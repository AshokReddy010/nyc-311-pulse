"""pulse: refresh NYC 311 data and rebuild the dashboard files.

pulse update              pull the latest days (two years on the first run) and yesterday's map
pulse build               rebuild site/data/pulse.json from data/
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .analyze import build
from .ingest import today_ny, update


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pulse", description=__doc__.splitlines()[0])
    p.add_argument("--data", type=Path, default=Path("data"))
    p.add_argument("--site", type=Path, default=Path("site/data"))
    sub = p.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("update")
    u.add_argument("--backfill", type=int, default=0, help="re-pull this many days of counts")
    sub.add_parser("build")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.cmd == "update":
        print(update(args.data, args.site, args.backfill))
    doc = build(args.data, args.site, today_ny())
    pl = doc["pulse"]
    print(
        f"{pl['day']}: {pl['total']:,} requests (typical {pl['typical']:,}); "
        f"most unusual: {doc['unusual'][0]['type'] if doc['unusual'] else '-'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
