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
  if [ -f "$file" ]; then changed_bytes=$((changed_bytes + $(wc -c < "$file"))); fi
done < <(git diff --cached --name-only -z --diff-filter=ACM)
if [ "$changed_bytes" -gt 26214400 ]; then
  echo "Refusing routine data commit larger than 25 MB: $changed_bytes bytes" >&2
  exit 1
fi

git config user.name "open-tennis-data bot"
git config user.email "actions@users.noreply.github.com"
git commit -m "$message"

run_id=${GITHUB_RUN_ID:?GITHUB_RUN_ID is required for automated data updates}
run_attempt=${GITHUB_RUN_ATTEMPT:-1}
repository=${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required for automated data updates}
branch="automation/data-${run_id}-${run_attempt}"
validate_command=${OPEN_TENNIS_DATA_VALIDATE_COMMAND:-open-tennis-data validate}
historical_command=${OPEN_TENNIS_DATA_HISTORICAL_COMMAND:-python -m unittest tests.test_data_quality -v}

validate_head() {
  $validate_command
  $historical_command
}
publish_status() {
  head_sha=$(git rev-parse HEAD)
  gh api --method POST "repos/$repository/statuses/$head_sha" \
    -f state=success -f context=data-required \
    -f description="Automated refresh passed validation and historical tests"
}

git fetch origin main
git rebase origin/main
validate_head
git push origin "HEAD:refs/heads/$branch"
pr_url=$(gh pr create --repo "$repository" --base main --head "$branch" \
  --title "$message" \
  --body "Validated automated Parquet data refresh from GitHub Actions run $run_id (attempt $run_attempt).")
publish_status

if gh pr merge "$pr_url" --squash --delete-branch; then
  exit 0
fi

previous_main=$(git rev-parse origin/main)
git fetch origin main
current_main=$(git rev-parse origin/main)
if [ "$current_main" = "$previous_main" ]; then
  echo "Automated data pull request could not merge; main did not advance. Leaving it open: $pr_url" >&2
  exit 1
fi
if ! git rebase origin/main; then
  git rebase --abort || true
  echo "Automated data pull request conflicted after main advanced. Leaving it open: $pr_url" >&2
  exit 1
fi
validate_head
git push --force-with-lease origin "HEAD:refs/heads/$branch"
publish_status
if ! gh pr merge "$pr_url" --squash --delete-branch; then
  echo "Automated data pull request failed its one merge retry. Leaving it open: $pr_url" >&2
  exit 1
fi
