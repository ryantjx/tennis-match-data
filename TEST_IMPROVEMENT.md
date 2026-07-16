# Data validation improvement plan

This repository validates three public contracts: completed matches, fixtures,
and annual tournaments. Historical data is checked on every pull request and
weekly; routine hourly and daily refreshes may only replace the current result
year and the current/next fixture horizon.

## Test tiers

1. **Unit tests** run without network access and cover normalization, score and
   date parsing, stable identifiers, source quarantine, CLI dispatch, release
   schema selection, and audit/PR scripts.
2. **Repository quality tests** run `open-tennis-data validate` and
   `tests.test_data_quality` against every checked-in ATP/WTA partition. They
   verify catalog checksums, exact schemas, IDs, participant/result invariants,
   tournament references, rankings, source reconciliation, and quarantine
   reasons.
3. **Pinned-source integration tests** build an isolated current-year dataset,
   validate it, query it, extract a level, and create both match and fixture
   releases. Network failures never promote partial output.
4. **Incremental tests** use a miniature two-year repository. They hash the old
   year, refresh the current year, and assert that older paths, row counts, and
   checksums remain identical. A rollover case creates year N+1 without
   rewriting years before N. A temporary remote also proves validated hourly
   and daily refreshes commit through an automatically merged data PR.
5. **Weekly retroactive audit** checks all local history, then rebuilds only the
   previous/current result years and current/next fixtures. It emits
   `retroactive-audit.json` and `retroactive-audit.md`; validated changes open a
   review-only PR, while a no-change run publishes artifacts only.

## Required schema assertions

- Matches contain exactly the 25 columns documented in `docs/SCHEMA.md` and
  always have a unique `match_id` and valid `tournament_id`.
- Fixtures contain exactly 12 columns, always have `fixture_id`, never expose
  `match_id`, and may have a null `scheduled_on`.
- Tournaments contain exactly 12 columns. IDs identify annual tour editions;
  `end_date` may be null but cannot precede `start_date`.
- Compact observations contain only `match_id`, `tour`, `year`,
  `source_file_id`, and `source_match_id`.
- Result and fixture releases have different schemas; both include a validated
  `tournaments.parquet` asset.

## Retroactive audit scenarios

The dedicated `tests.test_audit_workflow` suite covers changed revisions with
no semantic difference, previous/current result corrections, future fixture
dates, tournament-date corrections, multiple changed sources, and failed
rebuild isolation. `test_dataset` covers no-change artifacts, source revision
drift, stable tournament IDs, and old-partition checksum enforcement;
`test_scripts` proves review PR creation without auto-merge. Invalid staged
data must leave the checked-in dataset unchanged and fail the workflow after
writing its report.

## Test fixtures

- `tests/fixtures/` contains offline Wikimedia draw and tournament-page samples,
  including qualifying, Unicode players, walkovers, and nullable schedules.
- Temporary Parquet repositories model catalog corruption, immutable old-year
  changes, stable tournament-ID reuse, result/fixture release separation, and
  bootstrap refusal. Tests never mutate checked-in data.
- Mock upstream revision maps model unchanged, added, removed, and modified
  source pages without network access.
- Temporary bare Git repositories verify that routine refreshes create and
  auto-merge a validated data PR, while the weekly publisher creates a review
  PR and never invokes merge or auto-merge.

## Expected artifacts

- `data/catalog/catalog.parquet` is the checksum baseline and contains exactly
  the published Parquet inventory.
- `data/coverage/source-audit.parquet` stores one file/page revision, checksum,
  licence, and reconciliation record per source.
- Both release families contain ATP, WTA, men's, women's, combined, and
  `tournaments.parquet` assets; aliases must be byte-identical.
- Every weekly run produces `retroactive-audit.json` and
  `retroactive-audit.md`, including source revisions/checksums, entity and field
  changes, quarantine/reconciliation deltas, and old partitions proven
  unchanged.
- A no-change audit produces workflow artifacts only. A valid semantic change
  produces affected partitions/manifests plus a review-only PR. A failed audit
  produces reports and a failed workflow, with no data promotion or PR.

## Commands and acceptance criteria

```bash
ruff check src tests
mypy src/open_tennis_data
coverage run -m unittest discover -s tests -v
open-tennis-data validate
python -m unittest tests.test_data_quality -v
python -m unittest tests.test_audit_workflow -v
```

A change is accepted only when all commands pass, every catalog entry matches
its physical Parquet file, source rows reconcile to normalized plus quarantined
rows, release aliases are byte-identical, no file exceeds 75 MB, and protected
historical checksums remain unchanged. The one-time lean-schema migration must
also compare retained fields and per-tour/year/status counts before enabling
the historical immutability gate.
