# NYC 311 Pulse

[![ci](https://github.com/AshokReddy010/nyc-311-pulse/actions/workflows/ci.yml/badge.svg)](https://github.com/AshokReddy010/nyc-311-pulse/actions/workflows/ci.yml)
[![daily](https://github.com/AshokReddy010/nyc-311-pulse/actions/workflows/daily.yml/badge.svg)](https://github.com/AshokReddy010/nyc-311-pulse/actions/workflows/daily.yml)

**▶ [Live dashboard](https://ashokreddy010.github.io/nyc-311-pulse/)**, refreshed every morning.

New York's 311 line takes about 3.5 million service requests a year: noise, heat and hot water, illegal parking, potholes, rodents. This project turns the city's open data into a daily briefing:

- **The pulse.** How many requests came in yesterday, against a typical day of the same weekday, by borough.
- **What was unusual.** The complaint types furthest from their normal level for that weekday (a cold snap shows up as a heat/hot water spike before anyone writes about it).
- **The map.** Every located request from the day, by type.
- **Week by week and the 311 calendar.** Two years of weekly volumes by borough, and each type's seasonal shape.
- **Closure speed.** For each agency and complaint type, the share of requests closed within a day, three days and a week, and the median time to close.

## How it works

```
NYC Open Data (Socrata SODA)
   │  server-side aggregation: count(*) per day × complaint type × borough
   │  raw rows only for single days (closure times, map)
   ▼
pulse update ── data/counts/YYYY-MM.csv ──┐
             ── data/resolution/YYYY-MM.csv ├─> pulse build ─> site/data/pulse.json ─> dashboard
             ── site/data/points.json ─────┘
```

A scheduled GitHub Actions workflow runs every morning and commits the small monthly CSVs, so the repository itself is the dataset's history and every change is reviewable in git.

**Design decisions**

- **Aggregate at the source.** Daily counts are computed by the Socrata API (`$group`), so two years of history arrive as about a quarter of a million small rows instead of seven million records.
- **Late data is expected.** Requests keep arriving for days, so every run re-pulls the last ten days and the headline skips a day that is still well below its usual volume.
- **Weekday-aware anomalies.** 311 has a strong weekly rhythm, so each day is compared with the same weekday over the previous eight weeks using a robust z-score (median and MAD), with a floor on small types so 3 requests instead of 1 never headlines.
- **Honest closure metrics.** Measured only on requests at least eight days old (so "within a week" is final); closed dates earlier than created dates, a known artefact in this data, are treated as unknown rather than instant. "Closed" is the agency's status, not proof the problem was fixed.
- **Monthly files, upserted by day.** Re-pulling a day replaces its rows, so runs are idempotent and a failed run never leaves duplicates.

## Run it

```bash
pip install -e ".[dev]"
pulse update        # first run pulls two years of daily counts (a few minutes)
pulse build
python -m http.server -d site     # open http://localhost:8000/?base=data/
```

An optional `SOCRATA_APP_TOKEN` environment variable raises the API's rate limits.

Tests (`pytest`) run the whole pipeline against a seeded fake of the Socrata API with a weekly rhythm and a planted heating spike, which the anomaly ranking has to find.

## Limits

311 measures what people report, not what happens: reporting rates differ by neighbourhood and by how easy an issue is to report. Complaint types are as categorised by the city. Data: NYC Open Data, *311 Service Requests from 2020 to Present*.

## License

MIT
