"""Turn the monthly files into the dashboard's JSON: today's pulse, what is unusual, trends and closure speed.

"Unusual" means far from the same weekday's normal: 311 has a strong weekly rhythm (noise complaints peak on
weekend nights, most agencies are quieter on Sundays), so a Saturday is compared only with recent Saturdays.
The score is a robust z (median and MAD of the last 8 same weekdays), and small complaint types are left out
so that 3 requests instead of 1 does not headline the page.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

import pandas as pd

MIN_BASELINE = 15  # a complaint type needs a typical day of at least this many requests to be flagged
WEEKS = 8


def load(root: Path) -> pd.DataFrame:
    files = sorted(root.glob("*.csv"))
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_csv(f, dtype={"day": str}) for f in files], ignore_index=True)


def robust_z(x: float, base: list[float]) -> tuple[float, float]:
    m = median(base)
    mad = median(abs(b - m) for b in base) * 1.4826
    return (x - m) / max(mad, 0.1 * m, 2.0), m


def unusual(counts: pd.DataFrame, day: date) -> list[dict[str, Any]]:
    series: dict[str, dict[date, int]] = {}
    grouped = counts.groupby(["day", "complaint_type"])["n"].sum().reset_index()
    for d, t, n in grouped.itertuples(index=False):
        series.setdefault(str(t), {})[date.fromisoformat(str(d))] = int(n)
    if not any(day in v for v in series.values()):
        return []
    same = [day - timedelta(weeks=k) for k in range(1, WEEKS + 1)]
    window = [day - timedelta(days=k) for k in range(55, -1, -1)]
    out: list[dict[str, Any]] = []
    for t, by_day in series.items():
        base = [float(by_day.get(d, 0)) for d in same]
        x = float(by_day.get(day, 0))
        if median(base) < MIN_BASELINE and x < 3 * MIN_BASELINE:
            continue
        z, m = robust_z(x, base)
        out.append(
            {
                "type": t,
                "count": int(x),
                "typical": round(m, 1),
                "z": round(z, 2),
                "change": round(x / m - 1, 3) if m else None,
                "spark": [by_day.get(d, 0) for d in window],
            }
        )
    # Only rises are ranked: on recent days a drop is more often late-arriving records than a real lull.
    out = [r for r in out if r["z"] > 0]
    out.sort(key=lambda r: -r["z"])
    return out


def latest_complete_day(counts: pd.DataFrame) -> date | None:
    """The newest day whose volume is at least 60% of a typical same weekday (newer days are still filling in)."""
    if counts.empty:
        return None
    daily = counts.groupby("day")["n"].sum()
    daily.index = pd.to_datetime(daily.index)
    for ts in sorted(daily.index, reverse=True)[:7]:
        same = daily[(daily.index < ts) & (pd.DatetimeIndex(daily.index).dayofweek == ts.dayofweek)].tail(WEEKS)
        if len(same) < 3 or daily[ts] >= 0.6 * median(same.tolist()):
            return ts.date()
    return daily.index.max().date()


def build(data: Path, site: Path, today: date) -> dict[str, Any]:
    counts = load(data / "counts")
    res = load(data / "resolution")
    if counts.empty:
        raise RuntimeError("no count data yet; run the ingest first")
    counts["date"] = pd.to_datetime(counts["day"])
    latest = counts["date"].max().date()
    # the newest day can still be filling in; the pulse uses the latest day that looks complete
    day = latest_complete_day(counts) or latest
    daily = counts.groupby("date")["n"].sum()
    dts = pd.Timestamp(day)
    total = int(daily.loc[dts])
    same = [dts - pd.Timedelta(weeks=k) for k in range(1, WEEKS + 1)]
    base = [float(daily.get(d, 0)) for d in same]
    z, m = robust_z(total, base)
    by_type_day = counts[counts["date"] == dts].groupby("complaint_type")["n"].sum().sort_values(ascending=False)
    by_boro_day = counts[counts["date"] == dts].groupby("borough")["n"].sum()

    pulse = {
        "day": day.isoformat(),
        "weekday": dts.day_name(),
        "total": total,
        "typical": round(m),
        "z": round(z, 2),
        "top": [{"type": t, "n": int(n)} for t, n in by_type_day.head(12).items()],
        "boroughs": {b: int(n) for b, n in by_boro_day.items()},
    }

    weekly = (
        counts.assign(week=counts["date"] - pd.to_timedelta(counts["date"].dt.dayofweek, unit="D"))
        .groupby(["week", "borough"])["n"]
        .sum()
        .unstack(fill_value=0)
    )
    first = counts["date"].min()
    weekly = weekly[weekly.index >= first]  # drop a partial first week
    last_full = (
        dts - pd.Timedelta(days=dts.dayofweek) - (pd.Timedelta(0) if dts.dayofweek == 6 else pd.Timedelta(weeks=1))
    )
    weekly = weekly[weekly.index <= last_full]  # and a partial last week
    trend = {
        "weeks": [w.date().isoformat() for w in weekly.index],
        "boroughs": {b: weekly[b].astype(int).tolist() for b in weekly.columns},
    }

    top_types = counts.groupby("complaint_type")["n"].sum().sort_values(ascending=False).head(14).index.tolist()
    # average requests per day in each calendar month, so months covered in two different years and months
    # covered only partly are compared fairly
    per_day = (
        counts[counts["complaint_type"].isin(top_types)].groupby(["complaint_type", "date"])["n"].sum().reset_index()
    )
    per_day["month"] = per_day["date"].dt.month
    m_avg = per_day.groupby(["complaint_type", "month"])["n"].mean().unstack()
    season = {
        t: [
            round(float(v), 4) if pd.notna(v) else None
            for v in (m_avg.loc[t] / m_avg.loc[t].mean()).reindex(range(1, 13))
        ]
        for t in top_types
        if t in m_avg.index
    }

    closure: dict[str, Any] = {}
    if not res.empty:
        res["date"] = pd.to_datetime(res["day"])
        recent = res[res["date"] >= res["date"].max() - pd.Timedelta(days=27)]

        def summarise(g: pd.DataFrame) -> dict[str, Any]:
            n = int(g["n"].sum())
            w = g.dropna(subset=["p50_hours"])
            return {
                "n": n,
                "closed_1d": round(g["closed_1d"].sum() / n, 4),
                "closed_3d": round(g["closed_3d"].sum() / n, 4),
                "closed_7d": round(g["closed_7d"].sum() / n, 4),
                "median_hours": round(float((w["p50_hours"] * w["n"]).sum() / w["n"].sum()), 1) if len(w) else None,
            }

        agencies = recent.groupby("agency")["n"].sum().sort_values(ascending=False).head(10).index
        types = recent.groupby("complaint_type")["n"].sum().sort_values(ascending=False).head(20).index
        closure = {
            "window": [recent["day"].min(), recent["day"].max()],
            "overall": summarise(recent),
            "agencies": [{"agency": a, **summarise(recent[recent["agency"] == a])} for a in agencies],
            "types": [{"type": t, **summarise(recent[recent["complaint_type"] == t])} for t in types],
            "daily": [
                {"day": d, **summarise(g)}
                for d, g in res[res["date"] >= res["date"].max() - pd.Timedelta(days=41)].groupby("day")
            ],
        }
    doc = {
        "generated_at": datetime.now(UTC).isoformat(),
        "pulse": pulse,
        "unusual": unusual(counts, day)[:12],
        "trend": trend,
        "seasonality": season,
        "closure": closure,
        "coverage": {
            "first_day": counts["day"].min(),
            "last_day": counts["day"].max(),
            "requests": int(counts["n"].sum()),
        },
    }
    site.mkdir(parents=True, exist_ok=True)
    (site / "pulse.json").write_text(json.dumps(doc, separators=(",", ":"), default=str), encoding="utf-8")
    return doc
