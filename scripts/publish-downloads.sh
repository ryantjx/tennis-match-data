#!/usr/bin/env bash
set -euo pipefail

directory=${1:?download directory required}
release=${2:-data-latest}
repository=${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}

assets=(
  "$directory/mens.parquet"
  "$directory/womens.parquet"
  "$directory/atp.parquet"
  "$directory/wta.parquet"
  "$directory/all-matches.parquet"
)
for asset in "${assets[@]}"; do
  test -f "$asset"
done

gh release upload "$release" "${assets[@]}" --repo "$repository" --clobber
