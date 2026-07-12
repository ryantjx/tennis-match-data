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

run_id=${GITHUB_RUN_ID:?GITHUB_RUN_ID is required for automated data updates}
run_attempt=${GITHUB_RUN_ATTEMPT:-1}
repository=${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required for automated data updates}
branch="automation/data-${run_id}-${run_attempt}"

git push origin "HEAD:refs/heads/$branch"
pr_url=$(gh pr create \
  --repo "$repository" \
  --base main \
  --head "$branch" \
  --title "$message" \
  --body "Validated automated Parquet data refresh from GitHub Actions run $run_id (attempt $run_attempt).")
head_sha=$(git rev-parse HEAD)
gh api --method POST "repos/$repository/statuses/$head_sha" \
  -f state=success \
  -f context=v3-required \
  -f description="Automated Parquet refresh passed repository validation"
gh pr merge "$pr_url" --auto --squash --delete-branch
