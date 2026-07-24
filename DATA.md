# Data distribution

Open Tennis Data v3 publishes one atomic release asset set. Generated Parquet
is distributed through GitHub Releases rather than committed to `main`.

## Status

The current release status is `preview`. Preview tags are GitHub prereleases
and do not move `/releases/latest/`. A release becomes `stable` only after the
closed-event inventory, observation retrieval timestamps, source policy,
exact-date, checksum, schema, and freshness gates all pass.

Legacy `data-latest` and `future-latest` releases are frozen. Their earlier
date semantics are superseded; new integrations should use v3 assets.

## Match assets

All match-shaped files share the 19-column schema in
[`docs/SCHEMA.md`](docs/SCHEMA.md).

- `completed.parquet`: terminal matches with verified venue-local match days.
- `fixtures.parquet`: future slots. Dates and participants may be null;
  winners and scores are always null.
- `matches.parquet`: deterministic union of the two projections.

The CLI registers `matches`, `fixtures`, and `all_matches` views for these
files respectively.

## Audit assets

- `tournaments.parquet` and `players.parquet` contain only referenced
  identities.
- `provenance.parquet` links every released match to normalized source
  observations and accepted date evidence.
- `sources.parquet` records source URLs, immutable revisions, content hashes,
  source terms, attribution, rate policy, parser version, review date, and
  reconciliation totals.
- `coverage.parquet` records tour/year/level/lifecycle totals and the coverage
  gate state.
- `health.parquet` records `as_of`, latest match/fixture dates, and row totals.
- `quarantine.parquet` retains rejected evidence and explicit reasons.
- `catalog.parquet`, `manifest.json`, and `SHA256SUMS` make the release
  independently verifiable.

## Dates

Terminal `date` means an accepted match-level local calendar day. Tournament
start dates, tournament ranges, article publication dates, and unsupported
source date semantics do not qualify.

A timestamp source must identify the venue timezone. Missing or invalid
timezones are quarantined. Accepted sources that disagree on an exact date
cause all conflicting observations to be quarantined.

Fixture dates are schedule observations, not guarantees. Undated draw slots
remain visible with `date=null`.

## Local bridge data

The tracked `data/` tree is the legacy bootstrap bridge and contains tables
outside the v3 public scope. It is not the v3 release contract. It will be
removed from the repository only after the first verified stable v3 release;
preserved Git history will remain large.

Use the release assets for consumer applications and use `--data` only for
development, validation, or historical compatibility.
