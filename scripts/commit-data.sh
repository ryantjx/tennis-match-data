#!/usr/bin/env bash
set -euo pipefail

message=${1:?commit message required}
git add data contributions
if git diff --cached --quiet; then
  echo "No semantic data changes."
  exit 0
fi

changed_bytes=0
while IFS= read -r -d '' file; do
  if [ -f "$file" ]; then
    changed_bytes=$((changed_bytes + $(wc -c < "$file")))
  fi
done < <(git diff --cached --name-only -z --diff-filter=ACM)
if [ "$changed_bytes" -gt 26214400 ]; then
  echo "Refusing routine data commit larger than 25 MB: $changed_bytes bytes" >&2
  exit 1
fi

git config user.name "open-tennis-data bot"
git config user.email "actions@users.noreply.github.com"
git commit -m "$message"
git pull --rebase origin main
git push origin HEAD:main
