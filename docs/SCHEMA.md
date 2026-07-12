# Parquet schema v3

All published structured data is Parquet. Each file contains key-value metadata
`schema_version=3` and `dataset_version=YYYY.MM.DD`. The exact Arrow schema is
the contract; `open-tennis-data validate` checks types, metadata, checksums,
uniqueness, referential integrity, source reconciliation, and file limits.

## Match fact table

`matches` contains canonical match/event IDs; tour and season; denormalized
event name, level, detailed and source levels, surface and location; event and
nullable match dates; draw and canonical round; both players; winner/loser;
seeds, entries, event-time rankings; result status, termination, raw score and
best-of; observation dates and preferred-source summary.

`played_on_precision` is `day`, `event_only`, or `unknown`. `event_only` means
the tournament start date is known but `played_on` remains null.

## Dimensions and auxiliary facts

- `events`: source-stable tournament draws. Names never define identity.
- `players`: canonical player records and preferred source identifiers.
- `match_stats`: sparse duration, service, and break-point totals.
- `rankings`: long-form published ranking snapshots.
- `observations`: source-native IDs, fingerprints, revisions, checksums, URLs,
  retrieval dates, and licences for canonical matches.
- `fixtures`: separate scheduled or tentative source-observed slots.
- `identity`: persistent source-to-canonical mappings.
- `coverage`, `health`, `conflicts`, and `quarantine`: queryable quality state.
- `corrections`: proposed CC0 field-level patches.

## Identity

Source events use `(source, source_event_id, draw)`. Source matches use
`(source, source_match_id, row_fingerprint)`. Cross-source merging requires one
unambiguous event, round, and unordered canonical player pair. Ambiguities are
published as conflicts; no observation is silently discarded.

## Canonical levels

ATP values are `grand_slam`, `tour_finals`, `masters_1000`, `atp_500`,
`atp_250`, `challenger`, `itf`, `team`, `olympics`, or `other`. WTA values are
`grand_slam`, `tour_finals`, `wta_1000`, `wta_500`, `wta_250`, `wta_125`,
`itf`, `team`, `olympics`, or `other`. Where the source cannot distinguish a
modern category safely, v3 uses `other` and preserves the original code.
