"""Pull 311 data into small monthly files under data/ that the repository keeps.

    data/counts/YYYY-MM.csv       requests per day x complaint type x borough (server-side aggregation)
    data/resolution/YYYY-MM.csv   for each creation day x agency x complaint type: how many requests, how many
                                  were closed within 1, 3 and 7 days, and the median and 90th-percentile hours
    site/data/points.json         every request of the latest complete day, for the map

Counts for the last 10 days are re-pulled on every run because requests keep arriving late. Resolution is
computed for cohorts at least 8 days old (so "closed within 7 days" is final) and refreshed for 14 days.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from . import api

log = logging.getLogger("pulse")
NY = ZoneInfo("America/New_York")
BOROUGHS = ["BRONX", "BROOKLYN", "MANHATTAN", "QUEENS", "STATEN ISLAND"]


def today_ny() -> date:
    return datetime.now(NY).date()


def _ts(d: date) -> str:
    return f"{d.isoformat()}T00:00:00"


def fetch_counts(start: date, end: date) -> pd.DataFrame:
    """Requests per day x complaint type x borough for start <= day < end."""
    rows = api.get_all(
        {
            "$select": "date_trunc_ymd(created_date) AS day, complaint_type, borough, count(*) AS n",
            "$where": f"created_date >= '{_ts(start)}' AND created_date < '{_ts(end)}'",
            "$group": "day, complaint_type, borough",
            "$order": "day, complaint_type, borough",
        }
    )
    return counts_frame(rows)


def counts_frame(rows: list[dict]) -> pd.DataFrame:  # type: ignore[type-arg]
    df = pd.DataFrame(rows, columns=["day", "complaint_type", "borough", "n"])
    if df.empty:
        return df
    df["day"] = pd.to_datetime(df["day"]).dt.date.astype(str)
    df["complaint_type"] = df["complaint_type"].fillna("(blank)").str.strip()
    df["borough"] = df["borough"].fillna("UNSPECIFIED").str.upper().where(lambda b: b.isin(BOROUGHS), "UNSPECIFIED")
    df["n"] = pd.to_numeric(df["n"]).astype(int)
    return df.groupby(["day", "complaint_type", "borough"])["n"].sum().reset_index()


def fetch_requests(day: date) -> pd.DataFrame:
    rows = api.get_all(
        {
            "$select": "unique_key, created_date, closed_date, agency, complaint_type, borough, status, latitude, longitude",
            "$where": f"created_date >= '{_ts(day)}' AND created_date < '{_ts(day + timedelta(days=1))}'",
            "$order": "unique_key",
        }
    )
    return requests_frame(rows)


def requests_frame(rows: list[dict]) -> pd.DataFrame:  # type: ignore[type-arg]
    cols = [
        "unique_key",
        "created_date",
        "closed_date",
        "agency",
        "complaint_type",
        "borough",
        "status",
        "latitude",
        "longitude",
    ]
    df = pd.DataFrame(rows)
    for c in cols:
        if c not in df.columns:
            df[c] = None
    df = df[cols].copy()
    df["created_date"] = pd.to_datetime(df["created_date"], errors="coerce")
    df["closed_date"] = pd.to_datetime(df["closed_date"], errors="coerce")
    for c in ("latitude", "longitude"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["complaint_type"] = df["complaint_type"].fillna("(blank)").str.strip()
    df["agency"] = df["agency"].fillna("(blank)")
    return df


def resolution(df: pd.DataFrame) -> pd.DataFrame:
    """Closure speed per creation day x agency x complaint type.

    Closed dates before the created date (a known data-entry artefact) are treated as unknown, not as instant.
    """
    d = df.copy()
    hours = (d["closed_date"] - d["created_date"]).dt.total_seconds() / 3600
    d["hours"] = hours.where(hours >= 0)
    d["day"] = d["created_date"].dt.date.astype(str)
    g = d.groupby(["day", "agency", "complaint_type"])
    out = g.agg(
        n=("unique_key", "size"),
        closed=("hours", "count"),
        closed_1d=("hours", lambda h: int((h <= 24).sum())),
        closed_3d=("hours", lambda h: int((h <= 72).sum())),
        closed_7d=("hours", lambda h: int((h <= 168).sum())),
        p50_hours=("hours", "median"),
        p90_hours=("hours", lambda h: h.quantile(0.9) if h.notna().any() else None),
    ).reset_index()
    out["p50_hours"] = out["p50_hours"].round(2)
    out["p90_hours"] = pd.to_numeric(out["p90_hours"]).round(2)
    return out


def _upsert(root: Path, df: pd.DataFrame, keys: list[str]) -> list[Path]:
    """Write rows into data/<root>/YYYY-MM.csv, replacing any existing rows for the same days."""
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if df.empty:
        return written
    df = df.assign(_month=df["day"].str[:7])
    for month, part in df.groupby("_month"):
        path = root / f"{month}.csv"
        part = part.drop(columns="_month")
        if path.exists():
            old = pd.read_csv(path, dtype={"day": str})
            old = old[~old["day"].isin(set(part["day"]))]
            part = pd.concat([old, part], ignore_index=True)
        part.sort_values(keys).to_csv(path, index=False)
        written.append(path)
    return written


def points(df: pd.DataFrame, types: list[str]) -> dict:  # type: ignore[type-arg]
    """Compact map data: [lat, lon, type index] rounded to ~10 m, for the most common types (rest = "Other")."""
    idx = {t: i for i, t in enumerate(types)}
    d = df.dropna(subset=["latitude", "longitude"])
    d = d[(d["latitude"].between(40.4, 41.0)) & (d["longitude"].between(-74.3, -73.6))]
    pts = [
        [round(float(a), 4), round(float(o), 4), idx.get(t, len(types))]
        for a, o, t in zip(d["latitude"], d["longitude"], d["complaint_type"], strict=True)
    ]
    return {"types": types + ["Other"], "points": pts}


def update(data: Path, site: Path, backfill_days: int = 0, today: date | None = None) -> dict[str, int]:
    today = today or today_ny()
    counts_dir, res_dir = data / "counts", data / "resolution"
    first_run = not any(counts_dir.glob("*.csv"))
    days = backfill_days or (730 if first_run else 10)
    stats = {"count_rows": 0, "resolution_rows": 0, "points": 0}
    # counts, in month-sized chunks to keep each query small
    start = today - timedelta(days=days)
    chunk = start
    while chunk < today:
        nxt = min(chunk + timedelta(days=31), today)
        c = fetch_counts(chunk, nxt)
        _upsert(counts_dir, c, ["day", "complaint_type", "borough"])
        stats["count_rows"] += len(c)
        log.info("counts %s..%s: %s rows", chunk, nxt, len(c))
        chunk = nxt
    # resolution cohorts at least 8 days old, so every request has had 7 full days to close
    oldest = 50 if first_run or backfill_days else 22
    for back in range(oldest, 7, -1):
        day = today - timedelta(days=back)
        r = resolution(fetch_requests(day))
        _upsert(res_dir, r, ["day", "agency", "complaint_type"])
        stats["resolution_rows"] += len(r)
    # the map: every request of the latest complete day
    from .analyze import latest_complete_day, load

    yday = latest_complete_day(load(counts_dir)) or today - timedelta(days=1)
    req = fetch_requests(yday)
    top = req["complaint_type"].value_counts().head(9).index.tolist()
    doc = points(req, top)
    doc["day"] = yday.isoformat()
    site.mkdir(parents=True, exist_ok=True)
    (site / "points.json").write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
    stats["points"] = len(doc["points"])
    return stats
