import io
import json
import urllib.error
from datetime import date

import pandas as pd
import pytest

from pulse import analyze, api, ingest
from pulse.cli import main
from tests.fake_soda import SPIKE, fake_get_all

TODAY = date(2026, 10, 17)


@pytest.fixture
def soda(monkeypatch):
    monkeypatch.setattr(api, "get_all", fake_get_all)


@pytest.fixture(scope="module")
def populated(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setattr(api, "get_all", fake_get_all)
    root = tmp_path_factory.mktemp("pulse")
    stats = ingest.update(root / "data", root / "site", backfill_days=120, today=TODAY)
    mp.undo()
    return root, stats


def test_counts_frame_cleans_values():
    df = ingest.counts_frame(
        [
            {"day": "2026-10-01T00:00:00.000", "complaint_type": " Rodent ", "borough": "Unspecified", "n": "3"},
            {"day": "2026-10-01T00:00:00.000", "complaint_type": "Rodent", "n": "2"},
        ]
    )
    assert df.to_dict("records") == [
        {"day": "2026-10-01", "complaint_type": "Rodent", "borough": "UNSPECIFIED", "n": 5}
    ]
    assert ingest.counts_frame([]).empty


def test_resolution_ignores_negative_durations():
    df = ingest.requests_frame(
        [
            {
                "unique_key": "1",
                "created_date": "2026-10-01T10:00:00",
                "closed_date": "2026-10-01T12:00:00",
                "agency": "NYPD",
                "complaint_type": "Noise",
            },
            {
                "unique_key": "2",
                "created_date": "2026-10-01T10:00:00",
                "closed_date": "2026-10-03T22:00:00",
                "agency": "NYPD",
                "complaint_type": "Noise",
            },
            {
                "unique_key": "3",
                "created_date": "2026-10-01T10:00:00",
                "closed_date": "2026-10-01T09:00:00",
                "agency": "NYPD",
                "complaint_type": "Noise",
            },
            {"unique_key": "4", "created_date": "2026-10-01T10:00:00", "agency": "NYPD", "complaint_type": "Noise"},
        ]
    )
    r = ingest.resolution(df).iloc[0]
    assert (r["n"], r["closed"], r["closed_1d"], r["closed_3d"], r["closed_7d"]) == (4, 2, 1, 2, 2)
    assert r["p50_hours"] == 31.0  # median of 2 h and 60 h


def test_upsert_replaces_days(tmp_path):
    a = pd.DataFrame(
        {"day": ["2026-10-01", "2026-10-02"], "complaint_type": ["x", "x"], "borough": ["B", "B"], "n": [1, 2]}
    )
    ingest._upsert(tmp_path, a, ["day", "complaint_type", "borough"])
    b = pd.DataFrame(
        {"day": ["2026-10-02", "2026-11-01"], "complaint_type": ["x", "x"], "borough": ["B", "B"], "n": [5, 7]}
    )
    paths = ingest._upsert(tmp_path, b, ["day", "complaint_type", "borough"])
    assert sorted(p.name for p in paths) == ["2026-10.csv", "2026-11.csv"]
    assert pd.read_csv(tmp_path / "2026-10.csv")["n"].tolist() == [1, 5]


def test_points_drop_out_of_city_coordinates():
    df = pd.DataFrame(
        {"latitude": [40.7, 0.0, None], "longitude": [-73.9, 0.0, -73.9], "complaint_type": ["A", "B", "A"]}
    )
    doc = ingest.points(df, ["A"])
    assert doc == {"types": ["A", "Other"], "points": [[40.7, -73.9, 0]]}


def test_update_writes_monthly_files(populated):
    root, stats = populated
    assert stats["count_rows"] > 1000 and stats["resolution_rows"] > 0 and stats["points"] > 100
    months = sorted(p.name for p in (root / "data" / "counts").glob("*.csv"))
    assert months[0] == "2026-06.csv" and months[-1] == "2026-10.csv"
    res = analyze.load(root / "data" / "resolution")
    assert res["day"].max() == "2026-10-09"  # newest cohort is 8 days old


def test_build_finds_the_planted_spike(populated):
    root, _ = populated
    doc = analyze.build(root / "data", root / "site", TODAY)
    assert doc["pulse"]["day"] == "2026-10-16" and doc["pulse"]["total"] > 0
    assert doc["unusual"][0]["type"] == "HEAT/HOT WATER" and doc["unusual"][0]["z"] > 4
    assert len(doc["unusual"][0]["spark"]) == 56
    assert all(r["type"] != "Graffiti" for r in doc["unusual"])  # too small to flag
    c = doc["closure"]
    assert c["agencies"][0]["agency"] == "NYPD" and c["agencies"][0]["closed_1d"] > c["overall"]["closed_1d"]
    assert set(doc["trend"]["boroughs"]) >= {"BROOKLYN", "QUEENS"}
    assert (root / "site" / "pulse.json").exists() and json.loads((root / "site" / "points.json").read_text())[
        "day"
    ] == "2026-10-16"


def test_incomplete_latest_day_falls_back(populated, tmp_path):
    root, _ = populated
    data = tmp_path / "data"
    (data / "counts").mkdir(parents=True)
    for f in (root / "data" / "counts").glob("*.csv"):
        df = pd.read_csv(f, dtype={"day": str})
        df.loc[df["day"] == str(SPIKE), "n"] = 1  # the newest day has barely started
        df.to_csv(data / "counts" / f.name, index=False)
    doc = analyze.build(data, tmp_path / "site", TODAY)
    assert doc["pulse"]["day"] == "2026-10-15"


def test_api_retries_then_raises(monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    calls = []

    def urlopen(req, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, io.BytesIO(b"busy"))
        return io.BytesIO(b'[{"a": 1}]')

    monkeypatch.setattr(api.urllib.request, "urlopen", urlopen)
    assert api.get({"$limit": "1"}) == [{"a": 1}] and len(calls) == 2
    monkeypatch.setattr(
        api.urllib.request,
        "urlopen",
        lambda req, timeout: (_ for _ in ()).throw(
            urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b"bad query"))
        ),
    )
    with pytest.raises(api.ApiError, match="400"):
        api.get({})


def test_get_all_pages(monkeypatch):
    pages = [[{"i": i} for i in range(3)], [{"i": 3}]]
    monkeypatch.setattr(api, "get", lambda params: pages.pop(0))
    assert len(api.get_all({"$order": "i"}, page=3)) == 4


def test_cli_build(populated, capsys):
    root, _ = populated
    assert main(["--data", str(root / "data"), "--site", str(root / "site"), "build"]) == 0
    assert "HEAT/HOT WATER" in capsys.readouterr().out
