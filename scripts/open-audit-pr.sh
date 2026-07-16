#!/usr/bin/env bash
set -euo pipefail

report=${1:?retroactive audit report required}
git add data
if git diff --cached --quiet; then
  echo "No semantic retroactive data changes."
  exit 0
fi

git config user.name "open-tennis-data audit bot"
git config user.email "actions@users.noreply.github.com"
message="data: review weekly retroactive audit"
git commit -m "$message"
git pull --rebase origin main

run_id=${GITHUB_RUN_ID:?GITHUB_RUN_ID is required}
run_attempt=${GITHUB_RUN_ATTEMPT:-1}
repository=${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}
branch="automation/retroactive-audit-${run_id}-${run_attempt}"
git push origin "HEAD:refs/heads/$branch"
gh pr create \
  --repo "$repository" \
  --base main \
  --head "$branch" \
  --title "$message" \
  --body-file "$report"
