#!/usr/bin/env bash
set -euo pipefail

release_directory=${1:?usage: publish-v3-release.sh RELEASE_DIRECTORY [TAG]}
repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
asset_manifest="$repository_root/scripts/v3-release-assets.txt"

test -d "$release_directory"
assets=()
while IFS= read -r filename; do
  assets+=("$filename")
done < "$asset_manifest"
for filename in "${assets[@]}"; do
  test -f "$release_directory/$filename" || {
    echo "missing release asset: $filename" >&2
    exit 1
  }
done

release_tag=${2:-$(python3 - "$release_directory/manifest.json" <<'PY'
import json
import sys
from pathlib import Path

print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["release_tag"])
PY
)}
release_status=$(python3 - "$release_directory/manifest.json" <<'PY'
import json
import sys
from pathlib import Path

print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["release_status"])
PY
)

open-tennis-data verify-release --directory "$release_directory"
if test "$release_status" = stable; then
  open-tennis-data verify-release \
    --directory "$release_directory" \
    --require-complete \
    --max-age-hours 30
elif test "$release_status" != preview; then
  echo "unsupported release status: $release_status" >&2
  exit 1
fi

created=false
download_directory=$(mktemp -d)
cleanup() {
  status=$?
  rm -rf "$download_directory"
  if test "$status" -ne 0 && test "$created" = true; then
    gh release delete "$release_tag" --yes --cleanup-tag >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT

notes="Open Tennis Data v3 ${release_status} dataset. See manifest.json for scope, coverage, provenance, and checksums."
create_options=(
  --draft
  --title "Open Tennis Data v3 — $release_tag"
  --notes "$notes"
)
if test "$release_status" = preview; then
  create_options+=(--prerelease --latest=false)
fi
gh release create "$release_tag" "${create_options[@]}"
created=true

upload_paths=()
for filename in "${assets[@]}"; do
  upload_paths+=("$release_directory/$filename")
done
gh release upload "$release_tag" "${upload_paths[@]}"
gh release download "$release_tag" --dir "$download_directory"

open-tennis-data verify-release --directory "$download_directory"
for filename in "${assets[@]}"; do
  cmp "$release_directory/$filename" "$download_directory/$filename"
done

if test "$release_status" = stable; then
  open-tennis-data verify-release \
    --directory "$download_directory" \
    --require-complete \
    --max-age-hours 30
  gh release edit "$release_tag" --draft=false --latest
else
  gh release edit "$release_tag" --draft=false
fi
created=false
