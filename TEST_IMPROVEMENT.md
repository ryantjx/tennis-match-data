# Open Tennis Data v3.2 test plan

The v3.2 suite treats completed results and future fixtures as lifecycle views
of one 19-column match contract. Repository history is validated locally on
every pull request and every weekly audit. Daily refreshes may only
replace the current result year and current/next fixture horizon.

## Required commands

```bash
ruff check src tests
mypy src/open_tennis_data
coverage run -m unittest discover -s tests -v
coverage report --fail-under=90
open-tennis-data validate
python -m unittest tests.test_data_quality -v
python -m unittest tests.test_audit_workflow -v
python -m unittest tests.test_v32_contract -v
```

Integration CI additionally builds the current year from pinned sources,
validates and queries it, creates an extract, refreshes fixtures, runs the
retroactive audit, verifies both release families, promotes an unchanged copy,
and runs the browser suite.

## Contract tests

- Assert exact equality with the 19 names and physical types in
  `docs/SCHEMA.md` for every match partition, fixture partition, extract, and
  match release asset.
- Prove completed and future files support direct `UNION ALL` without casts or
  projections.
- Require `open_tennis_data_schema_version=3.2`, Parquet V2, Zstandard,
  65,536-row limits, stable sorting, and byte-identical deterministic writes.
- Reject rank, rank-point, country, entry, loser, fixture-ID, scheduled-date,
  and match-level source-URL columns while confirming the auxiliary rankings
  archive remains available.
- Require both release families to include byte-identical ATP/men's and
  WTA/women's aliases plus `tournaments.parquet`, `provenance.parquet`, and
  `sources.parquet`; every asset must remain below 75 MB.

## Participant, result, and identity tests

- Normalize singles scalars to one-element ID/name lists and validate synthetic
  doubles with two-element lists in stable source order.
- Reject invalid list lengths, empty lists, null or whitespace elements,
  placeholder players, duplicate teammates, opposing-team overlap, unequal
  ID/name lengths, and winner lists that do not exactly match either team.
- Allow unresolved future slots only when ID, name, and seed are consistently
  null. Fixtures require null winner and score.
- Validate the status domain, nullable exact dates, scalar seeds, `best_of`
  values 1/3/5, documented singles backfills, and the retained 303 completed
  rows with unavailable scores.
- Verify canonical player, tournament, and match IDs survive name, metadata,
  schedule, participant, and result changes. Test aliases, crosswalk precedence,
  source-slot fixture completion, and collision quarantine.
- Require copied tournament names to equal the authoritative tournament row and
  require approved name corrections to update staged affected partitions.
- Enforce one canonical match per source-file/source-match key and prevent one
  lifecycle ID from remaining in both completed and future releases.

## Migration tests and artifacts

`open-tennis-data migrate-v3-2` reads checked-in v3.1 data without network
access and writes only to an empty staging directory. It rewrites every match
and fixture partition, adds fixture provenance, rebuilds the catalog, validates
the assembled repository, and emits:

- `reports/v3.2/migration-v3.2.json`
- `reports/v3.2/migration-v3.2.md`

Acceptance requires identical old/new match counts, preserved established IDs,
zero retained-field differences, documented old/new checksums and schemas,
recorded `best_of` backfills, isolated ambiguous provenance, and no promotion
before complete validation. When a PR base is v3.1, the required CI contract
job checks this migration report instead of applying the old byte-immutability
gate. Once the base is v3.2, normal historical immutability is mandatory.

## Incremental and retroactive workflow tests

- Daily current-year refreshes cannot change any earlier
  year. Rollover initializes the new year and freezes the preceding one.
- Weekly audit first validates every local partition, probes revisions for the
  previous/current result years and current/next fixture/tournament sources,
  and rebuilds only changed inputs in isolation.
- Test no upstream change, revision-only/no-semantic change, previous-year
  correction, current-year correction, fixture/date correction, multiple
  changed sources, and invalid staged input.
- A no-change audit publishes JSON/Markdown artifacts only. A valid change opens
  a review PR without auto-merge. Failure preserves checked-in data and opens no
  PR.
- Reports include old/new revisions and checksums, entity and field deltas,
  quarantine/reconciliation changes, validation state, and partitions proven
  unchanged.

## Browser acceptance

The guided browser provides completed/future selection with identical visible
columns, renders a singles list as one name and doubles as joined names,
searches within list values, derives winner display from ID lists, and resolves
safe HTTP(S) source links through provenance. Explorer SQL remains read-only.

The change is accepted only when all checks pass, unit/integration coverage is
at least 90%, catalog rows match physical files, sources reconcile, aliases are
byte-identical, historical gates select the correct base-schema behavior, and
no failed build or audit mutates published data.
