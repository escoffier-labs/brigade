#!/usr/bin/env bash
set -euo pipefail

crate="${1:?usage: verify-crates-version.sh <crate> <version> [attempts] [delay-seconds]}"
version="${2:?usage: verify-crates-version.sh <crate> <version> [attempts] [delay-seconds]}"
attempts="${3:-20}"
delay_seconds="${4:-15}"
url="https://crates.io/api/v1/crates/${crate}/${version}"

for (( attempt = 1; attempt <= attempts; attempt++ )); do
  if response="$(curl --silent --show-error --fail --retry 2 \
    -H "User-Agent: ${crate}-release-verifier/1.0 (${url})" \
    "$url" 2>/dev/null)" \
    && grep -Eq "\"num\"[[:space:]]*:[[:space:]]*\"${version}\"" <<<"$response"; then
    echo "registry exposes ${crate} ${version}"
    exit 0
  fi

  if (( attempt < attempts )); then
    sleep "$delay_seconds"
  fi
done

echo "registry did not expose ${crate} ${version} after ${attempts} attempts" >&2
exit 1
