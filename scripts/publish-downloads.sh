#!/usr/bin/env bash
set -euo pipefail

directory=${1:?download directory required}
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

gh release upload data-latest "${assets[@]}" --repo "$repository" --clobber
