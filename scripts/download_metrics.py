#!/usr/bin/env python3
"""Build the public combined downloads badge. Failed refreshes leave output intact."""

import argparse
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

# brigadestack is the real package. brigade-cli is the same code under the old name,
# which existing installs, pipx apps and scripts still use, so both count as PyPI downloads.
PYPI_PACKAGES = ("brigadestack", "brigade-cli")
PYPI_URLS = {name: f"https://pypistats.org/api/packages/{name}/overall?mirrors=false" for name in PYPI_PACKAGES}
# The same split on npm: brigadestack is the real package and @escoffier-labs/brigade is
# the compatibility package that existing global installs keep updating.
NPM_PACKAGES = ("brigadestack", "@escoffier-labs/brigade")
NPM_URLS = {name: f"https://api.npmjs.org/downloads/range/last-year/{name}" for name in NPM_PACKAGES}
MAX_BYTES = 2 * 1024 * 1024
# Keep public JSON counts within JavaScript's exactly representable integer range.
MAX_SAFE_INTEGER = 2**53 - 1


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def reject_constant(_value):
    raise ValueError("non-finite JSON number")


def fetch_json(url):
    request = Request(url, headers={"User-Agent": "brigade-download-metrics/1", "Accept": "application/json"})
    with urlopen(request, timeout=30) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("source response too large")
    try:
        return json.loads(body, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except RecursionError:
        raise ValueError("source JSON nesting is too deep") from None


def parse_day(value):
    if not isinstance(value, str):
        raise ValueError("missing or invalid date")
    day = date.fromisoformat(value)
    if day.isoformat() != value:
        raise ValueError("date must use YYYY-MM-DD")
    return day


def daily_counts(payload, *, pypi, today, package):
    if not isinstance(payload, dict) or payload.get("package") != package:
        raise ValueError("unexpected source package")
    if pypi and payload.get("type") != "overall_downloads":
        raise ValueError("unexpected PyPI series")
    rows = payload.get("data" if pypi else "downloads")
    if not isinstance(rows, list) or not rows:
        raise ValueError("missing daily data")
    counts = {}
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid daily row")
        day = parse_day(row.get("date" if pypi else "day"))
        count = row.get("downloads")
        if type(count) is not int or not 0 <= count <= MAX_SAFE_INTEGER:
            raise ValueError("downloads must be nonnegative safe integers")
        if day >= today:
            raise ValueError("source contains an incomplete UTC day")
        category = row.get("category") if pypi else "npm"
        if pypi and category not in ("without_mirrors", "with_mirrors"):
            raise ValueError("unexpected PyPI category")
        key = (day, category)
        if key in seen:
            raise ValueError("duplicate daily data")
        seen.add(key)
        if not pypi or category == "without_mirrors":
            counts[day] = count
    if not counts:
        raise ValueError("missing daily data without mirrors")
    if not pypi and (parse_day(payload.get("start")) != min(counts) or parse_day(payload.get("end")) != max(counts)):
        raise ValueError("npm range disagrees with daily data")
    if (today - max(counts)).days > 3:
        raise ValueError("stale daily data (more than three UTC days old)")
    return counts


def aggregate(pypi, npm, *, now):
    if now.tzinfo is None:
        raise ValueError("updated time must include a timezone")
    now = now.astimezone(timezone.utc)
    # ``pypi`` and ``npm`` map each package name to its source payload.
    if not isinstance(pypi, dict) or set(pypi) != set(PYPI_PACKAGES):
        raise ValueError("unexpected PyPI package set")
    if not isinstance(npm, dict) or set(npm) != set(NPM_PACKAGES):
        raise ValueError("unexpected npm package set")
    pypi_series = {
        name: daily_counts(pypi[name], pypi=True, today=now.date(), package=name) for name in PYPI_PACKAGES
    }
    npm_series = {
        name: daily_counts(npm[name], pypi=False, today=now.date(), package=name) for name in NPM_PACKAGES
    }
    end = min(max(days) for days in (*pypi_series.values(), *npm_series.values()))
    start = end - timedelta(days=29)
    days = [start + timedelta(days=n) for n in range(30)]
    if any(day not in series for series in (*pypi_series.values(), *npm_series.values()) for day in days):
        raise ValueError("missing day in the shared 30-day window")
    pypi_packages = {name: sum(series[day] for day in days) for name, series in pypi_series.items()}
    pypi_total = sum(pypi_packages.values())
    npm_packages = {name: sum(series[day] for day in days) for name, series in npm_series.items()}
    npm_total = sum(npm_packages.values())
    total = pypi_total + npm_total
    # Nonnegative counts mean this bound also bounds each source subtotal.
    if total > MAX_SAFE_INTEGER:
        raise ValueError("combined downloads exceed the safe integer limit")
    return {
        "schemaVersion": 1,
        "label": "downloads/month",
        "message": f"{total:,}",
        "color": "blue",
        "total": total,
        "sources": {
            "pypi": {
                "package": PYPI_PACKAGES[0],
                "downloads": pypi_total,
                "mirrors": False,
                "packages": pypi_packages,
            },
            "npm": {"package": NPM_PACKAGES[0], "downloads": npm_total, "packages": npm_packages},
        },
        "period": {"start": start.isoformat(), "end": end.isoformat(), "days": 30, "timezone": "UTC"},
        "updatedAt": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="public badge JSON destination")
    args = parser.parse_args(argv)
    temporary = None
    try:
        pypi = {name: fetch_json(url) for name, url in PYPI_URLS.items()}
        npm = {name: fetch_json(url) for name, url in NPM_URLS.items()}
        badge = aggregate(pypi, npm, now=datetime.now(timezone.utc))
        # Replace only after both sources validate and the entire file is written.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.output.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(badge, stream, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, args.output)
    except (ValueError, OSError, URLError, HTTPException):
        # Do not print remote response bodies or environment-specific exception details.
        print("Downloads refresh failed. Previous metric retained.", file=sys.stderr)
        return 1
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
