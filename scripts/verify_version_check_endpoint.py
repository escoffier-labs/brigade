#!/usr/bin/env python3
"""Verify the public version-check endpoint reports the just-published release.

The publish workflow has no credentials to update the endpoint, so this check
is verify-only: a mismatch fails the job with instructions to update the
endpoint and rerun. Without it the endpoint drifts silently and update
discovery lags every release (issue #702; on 2026-08-04 it still reported
0.25.0 while a 0.25.1 version number existed in pyproject with no release).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

ENDPOINT = "https://check.brigade.tools/v1/version"


def fetch_latest(timeout: float = 10.0) -> str:
    # A bare urllib User-Agent gets a 403 from the endpoint's edge.
    request = urllib.request.Request(
        ENDPOINT,
        headers={"User-Agent": "brigade-publish-verify/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    latest = payload.get("latest") if isinstance(payload, dict) else None
    if not isinstance(latest, str) or not latest:
        raise ValueError(f"endpoint payload has no usable 'latest': {payload!r}")
    return latest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expected-version",
        required=True,
        help="version the endpoint must report (without the leading v)",
    )
    args = parser.parse_args(argv)

    try:
        latest = fetch_latest()
    except Exception as error:  # noqa: BLE001 - any fetch failure fails the gate loudly
        print(f"::error::version check endpoint fetch failed: {error}")
        return 1

    if latest == args.expected_version:
        print(f"version check endpoint reports {latest}: ok")
        return 0

    print(
        f"::error::{ENDPOINT} reports latest={latest!r} but this release is "
        f"{args.expected_version!r}. Update the endpoint to the new version, "
        "then rerun this job so update discovery does not lag the release."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
