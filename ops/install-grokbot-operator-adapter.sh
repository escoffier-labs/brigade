#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ID="grokbot-operator-adapter"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SOURCE_DIR="${REPO_ROOT}/obsidian-plugin/${PLUGIN_ID}"
SUMS_FILE="${SOURCE_DIR}/SHA256SUMS"

usage() {
  echo "usage: $0 [--rollback] /absolute/vault-root" >&2
  exit 2
}

is_owner_dir() {
  local path="$1"
  [[ -d "$path" && ! -L "$path" ]] || return 1
  local owner
  owner="$(stat -c '%u' "$path")"
  [[ "$owner" == "$(id -u)" ]]
}

reject_symlink() {
  local path="$1"
  if [[ -L "$path" ]]; then
    echo "refusing symlink path" >&2
    exit 1
  fi
}

verify_sources() {
  [[ -f "${SOURCE_DIR}/main.js" && -f "${SOURCE_DIR}/manifest.json" && -f "${SUMS_FILE}" ]] || {
    echo "tracked adapter sources are missing" >&2
    exit 1
  }
  (
    cd "${SOURCE_DIR}"
    sha256sum --strict -c SHA256SUMS >/dev/null
  ) || {
    echo "tracked adapter source hash mismatch" >&2
    exit 1
  }
}

install_adapter() {
  local vault="$1"
  local obsidian="${vault}/.obsidian"
  local plugins="${obsidian}/plugins"
  local dest="${plugins}/${PLUGIN_ID}"

  if [[ -e "$obsidian" ]]; then
    reject_symlink "$obsidian"
    is_owner_dir "$obsidian" || {
      echo "refusing non-owner .obsidian" >&2
      exit 1
    }
  else
    mkdir -m 0755 "$obsidian"
  fi
  if [[ -e "$plugins" ]]; then
    reject_symlink "$plugins"
    is_owner_dir "$plugins" || {
      echo "refusing non-owner plugins directory" >&2
      exit 1
    }
  else
    mkdir -m 0755 "$plugins"
  fi
  if [[ -e "$dest" ]]; then
    reject_symlink "$dest"
    is_owner_dir "$dest" || {
      echo "refusing non-owner adapter directory" >&2
      exit 1
    }
  else
    mkdir -m 0755 "$dest"
  fi
  install -m 0644 "${SOURCE_DIR}/main.js" "${dest}/main.js"
  install -m 0644 "${SOURCE_DIR}/manifest.json" "${dest}/manifest.json"
  echo "copied ${PLUGIN_ID} into the vault plugin directory"
  echo "this script does not load or activate the adapter"
}

rollback_adapter() {
  local vault="$1"
  local dest="${vault}/.obsidian/plugins/${PLUGIN_ID}"
  if [[ ! -e "$dest" ]]; then
    echo "adapter directory already absent"
    return 0
  fi
  reject_symlink "$dest"
  is_owner_dir "$dest" || {
    echo "refusing non-owner adapter directory" >&2
    exit 1
  }
  local name
  for name in "$dest"/* "$dest"/.[!.]*; do
    [[ -e "$name" || -L "$name" ]] || continue
    case "$(basename "$name")" in
      main.js | manifest.json | data.json) ;;
      *)
        echo "refusing rollback of unexpected adapter contents" >&2
        exit 1
        ;;
    esac
    if [[ -L "$name" || -d "$name" ]]; then
      echo "refusing rollback of unexpected adapter contents" >&2
      exit 1
    fi
  done
  rm -f "${dest}/main.js" "${dest}/manifest.json" "${dest}/data.json"
  rmdir "$dest"
  echo "removed ${PLUGIN_ID}; the operator must disable and reload it first"
}

ROLLBACK=0
VAULT=""
for arg in "$@"; do
  if [[ "$arg" == "--rollback" ]]; then
    ROLLBACK=1
    continue
  fi
  if [[ -n "$VAULT" ]]; then
    usage
  fi
  VAULT="$arg"
done

if [[ -z "$VAULT" || "$VAULT" != /* || "$VAULT" == *"/./"* || "$VAULT" == *"/../"* ]]; then
  usage
fi
if [[ "$VAULT" == *..* ]]; then
  echo "refusing vault path with dot segments" >&2
  exit 1
fi

reject_symlink "$VAULT"
if ! is_owner_dir "$VAULT"; then
  echo "refusing non-owner or non-directory vault" >&2
  exit 1
fi

verify_sources
if [[ "$ROLLBACK" -eq 1 ]]; then
  rollback_adapter "$VAULT"
else
  install_adapter "$VAULT"
fi
