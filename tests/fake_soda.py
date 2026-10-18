"""A stand-in for the Socrata API that answers the two query shapes the ingest uses, from a seeded model.

Volumes have a weekly rhythm (noise up at weekends), and one planted spike: heating complaints triple on
2026-10-16, which the "unusual" ranking must find.
"""

import random
import re
from datetime import date, datetime, timedelta

TYPES = {
    "Noise - Residential": 900,
    "HEAT/HOT WATER": 400,
    "Illegal Parking": 700,
    "Blocked Driveway": 350,
    "Street Condition": 200,
    "Rodent": 60,
    "Noise - Street/Sidewalk": 250,
    "Water System": 120,
    "Graffiti": 8,
}
BOROS = ["BROOKLYN", "QUEENS", "MANHATTAN", "BRONX", "STATEN ISLAND", None]
AGENCY = {
    "Noise - Residential": "NYPD",
    "HEAT/HOT WATER": "HPD",
    "Illegal Parking": "NYPD",
    "Blocked Driveway": "NYPD",
    "Street Condition": "DOT",
    "Rodent": "DOHMH",
    "Noise - Street/Sidewalk": "NYPD",
    "Water System": "DEP",
    "Graffiti": "DSNY",
}
SPIKE = date(2026, 10, 16)


def volume(t: str, d: date) -> int:
    rng = random.Random(f"{t}{d}")
    base = TYPES[t] * (1.35 if d.weekday() >= 5 and t.startswith("Noise") else 1.0)
    base *= 0.8 if d.weekday() == 6 and not t.startswith("Noise") else 1.0
    if t == "HEAT/HOT WATER" and d == SPIKE:
        base *= 3
    return max(0, int(rng.gauss(base, base**0.5)))


def _range(where: str) -> tuple[date, date]:
    a, b = re.findall(r"'(\d{4}-\d{2}-\d{2})T", where)
    return date.fromisoformat(a), date.fromisoformat(b)


def answer(params: dict) -> list[dict]:
    start, end = _range(params["$where"])
    if "$group" in params:
        rows = []
        d = start
        while d < end:
            for t in TYPES:
                n = volume(t, d)
                split = [n // 3, n // 3, n // 6, n // 10, n // 30]
                split.append(n - sum(split))
                for b, k in zip(BOROS, split, strict=True):
                    if k:
                        row = {"day": f"{d}T00:00:00.000", "complaint_type": t, "n": str(k)}
                        if b:
                            row["borough"] = b
                        rows.append(row)
            d += timedelta(days=1)
        return rows
    rows = []
    for t in TYPES:
        rng = random.Random(f"raw{t}{start}")
        for i in range(volume(t, start) // 10):  # a tenth of the volume keeps tests fast
            created = datetime.combine(start, datetime.min.time()) + timedelta(minutes=rng.randint(0, 1439))
            hrs = rng.expovariate(1 / (6 if AGENCY[t] == "NYPD" else 60))
            closed = None if rng.random() < 0.05 else created + timedelta(hours=hrs if rng.random() > 0.01 else -5)
            rows.append(
                {
                    "unique_key": f"{t[:3]}{start}{i}",
                    "created_date": created.isoformat(timespec="milliseconds"),
                    **({"closed_date": closed.isoformat(timespec="milliseconds")} if closed else {}),
                    "agency": AGENCY[t],
                    "complaint_type": t,
                    "borough": rng.choice(BOROS[:5]),
                    "status": "Closed",
                    "latitude": str(40.6 + rng.random() * 0.25),
                    "longitude": str(-74.0 + rng.random() * 0.2),
                }
            )
    return rows


def fake_get_all(params: dict, page: int = 50000) -> list[dict]:
    rows = answer(params)
    key = params.get("$order", "").split(",")[0].strip()
    return sorted(rows, key=lambda r: r.get(key, "")) if key else rows
