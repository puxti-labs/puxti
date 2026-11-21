#!/usr/bin/env bash
# Check every sdist in a dist folder: one per workspace package, and nothing inside
# beyond the package's own source and its approved root files.
# Usage: scripts/check_sdist.sh <dist dir>
set -euo pipefail

DIR="${1:?usage: scripts/check_sdist.sh <dist dir>}"
COMMON='pyproject\.toml|LICENSE|README\.md|PKG-INFO|\.gitignore'
EXPECTED=(puxti puxti_bi_core)

allowed_for() {
  case "$1" in
    puxti) echo "^src/puxti/|^$|^($COMMON|NOTICE\.md|SECURITY\.md|TELEMETRY\.md|\.env\.example)$" ;;
    puxti_bi_core) echo "^src/puxti_bi_core/|^$|^($COMMON)$" ;;
  esac
}

status=0
shopt -s nullglob
for sdist in "$DIR"/*.tar.gz; do
  base=$(basename "$sdist")
  known=0
  for name in "${EXPECTED[@]}"; do
    [[ "$base" =~ ^${name}-[0-9] ]] && known=1
  done
  if [ "$known" -eq 0 ]; then
    echo "ERROR: $base is not a workspace package sdist"
    status=1
  fi
done
shopt -u nullglob

for name in "${EXPECTED[@]}"; do
  shopt -s nullglob
  matches=("$DIR/$name"-[0-9]*.tar.gz)
  shopt -u nullglob
  if [ "${#matches[@]}" -ne 1 ]; then
    echo "ERROR: expected one $name sdist in $DIR, found ${#matches[@]}"
    status=1
    continue
  fi
  sdist="${matches[0]}"
  if ! listing=$(tar -tzf "$sdist"); then
    echo "ERROR: cannot read $(basename "$sdist")"
    status=1
    continue
  fi
  unexpected=$(echo "$listing" | sed 's|^[^/]*/||' | grep -Ev "$(allowed_for "$name")" || true)
  if [ -n "$unexpected" ]; then
    echo "ERROR: unexpected files in $(basename "$sdist"); add them to that package's sdist exclude list:"
    echo "$unexpected" | sed 's/^/    /'
    status=1
  else
    echo "  $(basename "$sdist") is clean"
  fi
done
exit "$status"
