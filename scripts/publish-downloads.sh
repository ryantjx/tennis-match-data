#!/usr/bin/env bash
set -euo pipefail

directory=$(cd "${1:?download directory required}" && pwd)
release=${2:-data-latest}
repository=${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}
script_directory=$(cd "$(dirname "$0")" && pwd)
filenames=()
while IFS= read -r filename; do filenames+=("$filename"); done < "$script_directory/release-assets.txt"
test "${#filenames[@]}" -eq 8

assets=()
for filename in "${filenames[@]}"; do
  test -f "$directory/$filename"
  assets+=("$directory/$filename")
done

snapshot=$(mktemp -d)
verification=$(mktemp -d)
snapshot_ready=false
cleanup() { rm -rf "$snapshot" "$verification"; }
rollback() {
  status=$?
  trap - ERR
  if [ "$snapshot_ready" = false ]; then cleanup; exit "$status"; fi
  snapshot_names=()
  while IFS= read -r -d '' path; do snapshot_names+=("$(basename "$path")"); done < <(find "$snapshot" -type f -print0)
  for filename in "${filenames[@]}"; do
    present=false
    for old_name in "${snapshot_names[@]}"; do [ "$old_name" = "$filename" ] && present=true; done
    if [ "$present" = false ]; then
      gh release delete-asset "$release" "$filename" --repo "$repository" --yes >/dev/null 2>&1 || true
    fi
  done
  if [ "${#snapshot_names[@]}" -gt 0 ]; then
    gh release upload "$release" "$snapshot"/* --repo "$repository" --clobber
  fi
  cleanup
  exit "$status"
}
trap rollback ERR
trap cleanup EXIT

gh release download "$release" --repo "$repository" --dir "$snapshot"
snapshot_ready=true
gh release upload "$release" "${assets[@]}" --repo "$repository" --clobber
gh release download "$release" --repo "$repository" --dir "$verification"
for filename in "${filenames[@]}"; do
  cmp "$directory/$filename" "$verification/$filename"
done
trap - ERR
