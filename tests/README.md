# Repository tests

This directory contains the deterministic unit, integration, data-quality, script, and
browser tests for Open Tennis Data. The pull-request workflow also exercises the live
pinned-source build and enforces at least 90% combined Python coverage.

## Run the tests

Install the project and Python test tools:

```bash
python -m pip install '.[dev]'
```

Run the complete Python suite and repository dataset validation:

```bash
python -m unittest discover -s tests -v
open-tennis-data validate
ruff check src tests
mypy src/open_tennis_data
```

Run Python coverage:

```bash
coverage erase
coverage run -m unittest discover -s tests -v
coverage report --show-missing
```

Run one module or test method:

```bash
python -m unittest tests.test_data_quality -v
python -m unittest tests.test_audit_workflow -v
python -m unittest \
  tests.test_dataset.DatasetTests.test_direct_downloads_include_matches_and_fixtures -v
```

The yearly suite can validate another generated dataset without changing the checked-in
files:

```bash
OPEN_TENNIS_DATA_ROOT=/path/to/generated/data \
  python -m unittest tests.test_data_quality -v
```

Run the browser smoke test:

```bash
npm install
npx playwright install chromium
npm run test:browser
```

The browser test starts a local server for `site/` and uses the published Parquet files,
so it requires internet access. The ordinary Python suite is deterministic and does not
perform a live source rebuild.

## Test suites

| Suite | Coverage |
| --- | --- |
| `test_model_scores.py` | Text normalization, slugs, canonical player/match identifiers, round aliases, semantic matching, Sackmann scores, bracket scores, tiebreaks, and retirements. |
| `test_wikimedia.py` | Men's and women's result parsing, Unicode names, qualifying draws, tour-specific page discovery, tentative future draw slots, and unsupported future-page titles. All parser inputs come from `tests/fixtures/`. |
| `test_dataset.py` | Year parsing, ingestion classification, quarantine precedence, complete repository validation, deliberately corrupted datasets, catalog accounting, partition-pruned queries, repeated tournament identity, statistics/date semantics, extracts, release downloads, strict future filtering, alias equality, metadata removal, and deterministic corrections. |
| `test_data_quality.py` | The checked-in or `OPEN_TENNIS_DATA_ROOT` dataset. It requires every ATP and WTA year from 1968 through catalog `as_of`, checks required match, tournament, and observation partitions, enforces match cleanliness, verifies documented ranking coverage, and restricts quarantine reasons. These tests never skip when the repository dataset is missing. |
| `test_cli.py` | Successful and failing CLI behavior for `build`, `bootstrap`, `validate`, `query`, `extract`, `add-correction`, `refresh-wikimedia`, `refresh-current`, `refresh-fixtures`, `audit-retroactive`, `promote`, both `downloads` modes, and the interactive `shell`. Network-heavy handlers are mocked here and exercised live in CI. |
| `test_audit_workflow.py` | Revision-gated weekly audit orchestration: upstream changes with no semantic delta, match/fixture/tournament field corrections, multiple source changes, isolated staging, promotion exactly once after validation, JSON/Markdown artifacts, and failed rebuilds that never promote. |
| `test_scripts.py` | Release-asset preconditions, GitHub upload arguments through a stubbed `gh`, no-change data commits, auto-merged routine data PRs, and review-only weekly audit PRs. |
| `browser/site.spec.js` | Chromium smoke coverage for DuckDB-Wasm initialization, guided search, filters, pagination, tab navigation, table/schema loading, read-only SQL execution, and rejection of mutating SQL. |

## Dataset cleanliness contract

The validation and yearly tests jointly enforce these rules:

- Catalog paths are unique and exactly match the published Parquet inventory. Recorded
  byte sizes, row counts, checksums, `as_of` dates, and source revisions must reconcile.
- Partitioned rows must contain the tour and year named by their paths. Match years must
  have corresponding tournament, observation, coverage, and source-audit data.
- ATP and WTA match coverage begins in 1968. Ranking coverage begins in 1973 for ATP and
  1984 for WTA. Match-statistics partitions may be sparse.
- Match, tournament, player, observation, statistic, ranking, fixture, and crosswalk identifiers
  must be unique where required and must not leave broken references.
- Canonical matches cannot contain identical participants or invalid winner/loser
  relationships. Required domains and tournament references must be valid.
- Published statistics cannot be negative or internally impossible, such as first serves
  won exceeding first serves in or break points saved exceeding break points faced.
- Every source match row is classified exactly once. Rejected rows use
  `duplicate_source_row`, `invalid_participants`, or `invalid_statistics`, and source
  reconciliation must satisfy `source_rows = normalized_rows + quarantined_rows`.
- Tournament dates may be null. Optional statistics, fixture participants and schedule
  dates, and player biography values may remain null.

## Download expectations

Normal rolling downloads use the lean completed-match schema. Future-only downloads use
the separate lean fixture schema. Known dates must be on or after the catalog `as_of`
date; undated tentative slots are retained. Both release families include
`tournaments.parquet`.

For both release families, tests require the five match/fixture files plus the tournament
file, identical ATP/men's and WTA/women's aliases, consistent schemas, no repository
metadata, Zstandard Parquet output, and the 75 MB file limit.
`scripts/verify-downloads.sh` repeats these checks before and after scheduled publication.

## Live and scheduled coverage

The pull-request integration job performs a live current-year build, validation, query,
extract, Wikimedia refresh, revision-gated audit, normal and future download generation,
release verification, and a no-change promotion. Coverage from that sequence is appended
to the deterministic tests and must remain at or above 90%.

Daily and hourly workflows rebuild only the current result year and fixture horizon in
isolation, prove older checksums unchanged, and commit validated changes through an
automatically squash-merged data PR using scoped `contents`, `pull-requests`, and
`statuses` permissions. The weekly audit validates all local history, checks upstream
revisions for the previous and current result years plus current/next fixtures, and
opens a review-only PR only for validated semantic changes.

## Adding tests

- Keep parser and normalization tests offline by adding minimal source examples under
  `tests/fixtures/`.
- Use temporary directories for generated Parquet files, extracts, corrections, and Git
  repositories. Tests must not modify checked-in data.
- Add a failure-path test whenever a new validator rule or CLI error is introduced.
- Label data-quality failures with the affected table and `tour/year` whenever those values
  are available.
- Update this README when a new suite, public command, data contract, or CI test stage is
  added.
