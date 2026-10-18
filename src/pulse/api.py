"""NYC Open Data (Socrata SODA 2.1) client: SoQL queries with paging and retries.

An app token (SOCRATA_APP_TOKEN) raises rate limits but is optional; the daily volume here is small.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("pulse")
DATASET = "https://data.cityofnewyork.us/resource/erm2-nwe9.json"  # 311 Service Requests from 2020 to Present
PAGE = 50_000
UA = "nyc-311-pulse/1.0 (https://github.com/AshokReddy010/nyc-311-pulse)"


class ApiError(RuntimeError):
    pass


def get(params: dict[str, str], retries: int = 5, timeout: float = 180) -> list[dict[str, Any]]:
    url = DATASET + "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": UA, "Accept": "application/json"}
    token = os.environ.get("SOCRATA_APP_TOKEN")
    if token:
        headers["X-App-Token"] = token
    last = ""
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
                if not isinstance(data, list):
                    raise ApiError(f"unexpected response: {str(data)[:200]}")
                return data
        except urllib.error.HTTPError as error:
            last = f"HTTP {error.code}: {error.read().decode(errors='replace')[:200]}"
            if error.code < 500 and error.code != 429:
                raise ApiError(last) from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last = f"{type(error).__name__}: {error}"
        wait = 5 * 2**attempt
        log.warning("SODA request failed (%s); retrying in %ss", last, wait)
        time.sleep(wait)
    raise ApiError(last)


def get_all(params: dict[str, str], page: int = PAGE) -> list[dict[str, Any]]:
    """Every row of a query, page by page (the query must have a stable $order)."""
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        rows = get({**params, "$limit": str(page), "$offset": str(offset)})
        out += rows
        if len(rows) < page:
            return out
        offset += page
