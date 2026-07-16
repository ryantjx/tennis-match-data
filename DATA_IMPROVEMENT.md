# Data improvement plan

## Purpose

This document records the accepted v3.2 data-contract decisions and their
implementation. It builds on the lean v3.1 migration and is the rationale
behind the shared match schema in `docs/SCHEMA.md`.

The primary change is one match-row contract for completed matches and future
fixtures. Repository partitions, rolling releases, extracts, and website inputs
must expose the same columns in the same order and with the same Arrow types.
Lifecycle state determines which values may be null.

## Accepted feedback

| Date | Feedback | Decision | Status |
| --- | --- | --- | --- |
| 2026-07-16 | Completed and future match tables should have the same columns. | Adopt one shared match schema for repository partitions, all rolling match assets, extracts, and guided website tables. | Implemented in v3.2. |
| 2026-07-16 | Future matches may have unresolved participants; winner and score should be null. | Permit either participant slot to be unresolved and require `winner_id` and `score` to be null while `status = fixture`. | Implemented in v3.2. |
| 2026-07-16 | Include `tournament_name` in the common schema. | Store the canonical consumer-facing tournament name beside `tournament_id` in every match-shaped dataset. | Implemented in v3.2. |
| 2026-07-16 | Keep player seed and rank. | Superseded by the later decision to retain seed but remove rank. | Superseded. |
| 2026-07-16 | Rename `scheduled_on` to `date` and put it first. | Use one nullable, timezone-free `date` field as the first physical column. Never substitute the tournament start date. | Implemented in v3.2. |
| 2026-07-16 | Keep `best_of`. | Retain source values 1, 3, and 5 and backfill missing singles values from documented tour/draw rules. | Implemented in v3.2. |
| 2026-07-16 | Replace `winner_slot` with `winner_id`. | Store the canonical ID list of the winning participant or team directly. | Implemented in v3.2. |
| 2026-07-16 | Seed is more relevant than ranking. | Retain `player1_seed` and `player2_seed`; remove both rank columns. | Implemented in v3.2. |
| 2026-07-16 | Support different match types in the same structure. | Add `format` with `singles` and `doubles` values. Store participant IDs and names as lists so one schema represents both formats. Scalar API input for singles is normalized to a one-item list before storage. | Implemented in v3.2; ingestion remains singles-only. |
| 2026-07-16 | Establish canonical ID rules. | Persist source crosswalks for player, tournament, and match IDs; IDs are immutable and are never recomputed from mutable names or metadata. | Implemented in v3.2. |
| 2026-07-16 | Prevent duplicated tournament names from drifting. | Validate every row's `tournament_name` against its canonical `tournament_id` record and update affected rows atomically after a correction. | Implemented in v3.2. |
| 2026-07-16 | Formalize statuses, dates, and null values. | Adopt the domains and field matrices in this document; reject placeholders and invented values. | Implemented in v3.2. |
| 2026-07-16 | Remove `source_url`. | Keep source URLs and source relationships only in compact provenance and source-file tables. | Implemented in v3.2. |
| 2026-07-16 | Standardize physical Parquet layout. | Pin column order, types, sorting, compression, encoding, row-group policy, writer version, and schema metadata. | Implemented in v3.2. |

## Design principles

- Use one schema, not one physical file. Completed and future releases remain
  separate so consumers can download only the rows they need.
- Use `status` as the lifecycle discriminator. Do not restore `record_type`,
  `is_fixture`, or another duplicate type flag.
- Assign identity before participants are known. A fixture retains the same
  identity as it gains participants and becomes a completed result.
- Keep detailed tournament metadata in `tournaments.parquet`, but repeat the
  canonical `tournament_name` in match rows as a deliberate convenience.
- Store complete source relationships, URLs, revisions, and licences outside
  match rows in the compact provenance and source-file tables.
- Never invent dates, participants, IDs, seeds, scores, or match formats to
  satisfy a non-null constraint.
- Normalize flexible ingestion values into one strict storage schema. Parquet
  columns do not change type between singles, doubles, results, and fixtures.

## Implemented shared match schema

The recommended v3.2 standard contains 19 columns in this exact order:

```text
date, match_id, tournament_id, tournament_name, tour, year, draw, round,
format, player1_id, player1_name, player1_seed,
player2_id, player2_name, player2_seed,
winner_id, status, score, best_of
```

Recommended Arrow types are:

```text
date: date32
match_id, tournament_id, tournament_name, tour, draw, round, format: string
year: int16
player1_id, player1_name, player2_id, player2_name, winner_id: list<string>
player1_seed, player2_seed, status, score: string
best_of: int8
```

An ingestion API may accept a string or a list for a participant ID or name.
Before writing Parquet, a singles scalar must become a one-item list. Stored
values are therefore always `list<string>` or null, never a mixed Arrow type.
Seeds describe the participant slot or doubles team and remain scalar strings.

### Column rules

| Columns | Completed/result rows | Future rows (`status = fixture`) |
| --- | --- | --- |
| `date` | Nullable `date32`. Use the actual playing date when known. | Nullable `date32`. Use the latest published scheduled calendar date. |
| `match_id` | Required and globally unique. Existing canonical IDs remain stable. | Required and globally unique. Assigned to the source draw slot and retained after completion. |
| `tournament_id, tournament_name, tour, year, draw, round` | Required and validated against the tournament edition. | Required and validated against the tournament edition. |
| `format` | Required: `singles` or `doubles`. | Required when established by the draw or source; otherwise the fixture is quarantined because participant-list validation depends on it. |
| Player 1 ID/name | Required lists with one element for singles or two for doubles. | Both lists may be null while unresolved; otherwise their lengths must match `format`. |
| `player1_seed` | Nullable scalar seed for the player or team. | Nullable. |
| Player 2 ID/name | Required lists with one element for singles or two for doubles. | Both lists may be null while unresolved; otherwise their lengths must match `format`. |
| `player2_seed` | Nullable scalar seed for the player or team. | Nullable. |
| `winner_id` | Must exactly equal the Player 1 or Player 2 ID list when the terminal status declares a winner. Nullable only when no winner exists. | Must be null. |
| `status` | One of the terminal statuses defined below. | Must be `fixture`. |
| `score` | Nullable when the source does not provide one. In particular, preserve the 303 source-declared completed rows without scores. | Must be null. |
| `best_of` | Required; preserve source values `1`, `3`, and `5`, then use documented singles backfills for gaps. | Required after applying the same documented singles rules. |

### Match format and participant representation

- `format = singles` requires exactly one ID and one name in each resolved
  participant list.
- `format = doubles` requires exactly two IDs and two names in each resolved
  participant list.
- List order is stable and follows the canonical source or team registration
  order; it is not changed alphabetically during refreshes.
- A resolved participant's ID and name lists must have equal lengths.
- `winner_id` is a list because a doubles winner is a team. It must be an exact
  list match for one participant slot, including order.
- A scalar supplied by a singles adapter is accepted only at ingestion and is
  normalized to a one-item list before validation and persistence.

### Date semantics

`date` is a timezone-free calendar date stored as Arrow `date32`:

- It is never a timestamp and has no assumed time zone.
- It is never copied from `tournament.start_date` merely to fill a null.
- A completed row uses the actual playing date when it is known.
- A fixture uses the latest published scheduled date when it is known.
- A schedule correction updates `date` without changing `match_id`.
- Null is valid when a trustworthy match date is unavailable.

### Status and result rules

The complete status domain is:

```text
fixture, completed, walkover, retired, defaulted, abandoned, cancelled
```

| Status | Lifecycle | `winner_id` | `score` |
| --- | --- | --- | --- |
| `fixture` | Future | Null | Null |
| `completed` | Terminal | Required | Nullable only when provenance lacks a score; never invent one. |
| `walkover` | Terminal | Required | Null or normalized walkover notation |
| `retired` | Terminal | Required when a winner is declared | Nullable; validate if present |
| `defaulted` | Terminal | Required when a winner is declared | Nullable; validate if present |
| `abandoned` | Terminal | Null unless an official winner is declared | Nullable |
| `cancelled` | Terminal | Null | Null |

Outcome conditions are represented only by `status`; they are not repeated in
a separate `termination` field. Scores use one documented normalized grammar.

### Null and placeholder policy

Future rows may have zero, one, or two resolved participant slots. A completely
unresolved slot uses null for its ID list, name list, and seed. Empty lists are
not unresolved participants and are rejected.

- Reject empty or whitespace-only strings in every string and list element.
- Reject placeholder identities such as `TBD`, `Unknown`, `Qualifier`,
  `Lucky Loser`, `Winner of Match 12`, and equivalent source text.
- Keep source draw-slot descriptions in provenance or ingestion staging, not
  in canonical participant fields.
- If a participant ID list is null, its name list and seed must also be null.
- If a participant name list is null, its ID list and seed must also be null.
- Seeds may be null for resolved participants. A seed is stored as source
  notation without converting qualifiers or entry types into fake seeds.
- Null remains null in Parquet; it is never serialized as an empty string,
  sentinel number, or display label. User interfaces may render it as `TBD` or
  an em dash.

### Columns removed from the shared row

- `player1_rank` and `player2_rank` are removed; seeds are the retained
  tournament-context fields.
- Country, entry, and ranking-point fields remain available in their
  appropriate player, participant-detail, or ranking datasets.
- `winner_slot` is removed; `winner_id` stores the winning participant ID list.
- `loser_id` is derivable as the other participant slot when a winner exists.
- `fixture_id` is superseded by the lifecycle-stable `match_id`.
- `scheduled_on` is renamed to the leading `date` column.
- `source_url` is removed. URLs remain in source-file and provenance tables.

## Canonical identity rules

All public IDs are opaque, immutable strings backed by persistent crosswalks.
Once published, an ID is never recomputed merely because a name, date, draw,
source URL, or other metadata changes.

### Player IDs

- Maintain one canonical player registry and source crosswalk per upstream
  provider.
- Reuse an existing canonical ID whenever a source identifier is already
  mapped.
- For a previously unseen player, allocate `player_{stable_hash}` once and
  persist the source identifiers and reviewed identity evidence used to create
  it. Mutable display names are not part of future ID computation.
- A corrected or changed name updates player metadata but not the player ID.
- Player merges preserve the surviving canonical ID and keep retired IDs as
  aliases in the crosswalk. Published match rows are migrated atomically and a
  merge record is included in the audit report.
- Never assign an ID to placeholder draw-slot text. Historical participants
  without enough identity evidence are quarantined rather than guessed.

### Tournament IDs

- One ID represents one annual tour edition and is shared by its main and
  qualifying draws.
- Allocate `tournament_{tour}_{year}_{stable_hash}` once and persist every
  source-edition crosswalk.
- ATP and WTA editions remain distinct even when they share a public event
  name, site, or date range.
- Corrections to name, dates, level, surface, or location never change the ID.

### Match IDs

- `match_id` replaces `fixture_id` for the complete fixture-to-result lifecycle.
- Prefer a persistent source match ID or draw-slot key scoped by source and
  tournament edition. Persist the crosswalk before publishing the fixture.
- If no stable upstream key exists, allocate and persist an internal
  `match_{stable_hash}` from reviewed source-slot evidence. Do not regenerate it
  after participants or dates become known.
- Participant, seed, date, score, status, and tournament-metadata corrections
  never change `match_id`.
- Collision detection is mandatory. Conflicting source mappings are
  quarantined and cannot silently overwrite an existing crosswalk.
- A fixture conversion reuses its `match_id`, removes the future row
  atomically, and publishes exactly one completed row.

## Tournament-name consistency

- `tournaments.parquet` is authoritative for the canonical name of each
  `tournament_id`.
- Every match and fixture row must have a `tournament_name` exactly equal to
  the canonical value after the repository's documented Unicode and
  whitespace normalization.
- A tournament-name correction rebuilds all affected current and historical
  partitions in staging as one reviewed semantic change.
- The retroactive audit reports the old and new names, affected IDs, row
  counts, partitions, and checksums.
- Validation rejects orphan tournament IDs and conflicting names before data
  is promoted from staging.

## Provenance after removing `source_url`

Match rows contain no URL. The compact provenance table maps each match to its
source observation:

```text
match_id, tour, year, source_file_id, source_match_id
```

The source-file table stores the URL, provider, revision, retrieval date,
checksum, licence, and reconciliation totals once per source file. Future
fixtures must have at least one valid provenance mapping before publication.
The website resolves an optional source link through these tables rather than
reading a repeated URL from every match row.

## Releases and repository layout

- Completed match partitions and fixture partitions use the exact same
  19-column Arrow schema.
- Every ATP, WTA, men's, women's, combined, singles, doubles, extract, and
  rolling match asset uses the same names, order, types, and lifecycle rules.
- `data-latest` contains terminal result rows only.
- `future-latest` contains `status = fixture` rows only.
- Both release families continue to include `tournaments.parquet` and the
  provenance data needed to resolve sources.
- The catalog records one shared schema version for both logical match tables.
- A direct `UNION ALL` of completed and future match assets must work without
  casts, aliases, or missing-column projections.
- Auxiliary tournament, player, ranking, provenance, source-file, and catalog
  datasets retain schemas appropriate to their entities.

## Deterministic physical layout

All match-shaped Parquet outputs must use:

- The exact 19-column order and Arrow types specified above.
- Stable row order: `date` nulls last, then `tournament_id`, `draw`, `round`,
  and `match_id` as the final tie-breaker.
- Zstandard compression and dictionary encoding for eligible string columns.
- One repository-wide row-group target configured in code rather than chosen
  independently by adapters.
- Pinned DuckDB 1.5.4 with Parquet V2 and identical writer settings in local
  builds and CI.
- Deterministic schema metadata containing the contract version, while
  excluding volatile build timestamps and machine-specific values.
- Normalized Unicode, line endings, list ordering, and null representation
  before serialization.

The implementation uses Zstandard level 19, 65,536-row groups, a 1 MiB
dictionary-page limit, one writer thread, and schema version 3.2. A golden-file
test proves identical input produces byte-identical Parquet files.

## Website tables

Completed and future views use the same visible columns and labels:

```text
Date, Tournament, Round, Format,
Player/Team 1, Seed 1, Player/Team 2, Seed 2,
Winner, Score, Best of, Status, Level, Surface, Source
```

- Render a singles list as one name and a doubles list as two joined names.
- Derive the winner display by matching `winner_id` to a participant ID list.
- Resolve the optional Source link through provenance; do not restore
  `source_url` to match data.
- Render null participants, dates, winners, scores, and `best_of` values as
  `TBD` or an em dash according to context.
- Never label `tournament.start_date` as a match `Date`.
- Hide internal IDs from guided tables but retain them in the data explorer.

## Validation changes

### Shared schema and layout

- Assert byte-for-byte-equivalent 19-column Arrow schemas for every match
  partition, release asset, extract, and website input.
- Assert exact column order, list types, schema metadata, deterministic sort,
  writer settings, and a byte-identical clean rebuild.
- Prove direct `UNION ALL` compatibility across completed/future, ATP/WTA, and
  singles/doubles assets.
- Reject removed columns including ranks, `fixture_id`, `source_url`,
  `record_type`, and `is_fixture`.

### Participants, formats, and IDs

- Validate one list element per resolved singles slot and two per resolved
  doubles slot.
- Validate equal ID/name list lengths, unique teammates, distinct opposing
  teams, stable list order, and exact winning-team membership.
- Validate every ID through its canonical registry and source crosswalk.
- Test name corrections, player merges, tournament corrections, source-key
  collisions, and fixture conversion without unintended ID changes.
- Reject placeholders, empty strings, empty lists, guessed IDs, orphan IDs,
  and mismatched tournament names.

### Future rows

- Require `status = fixture`; require `winner_id` and `score` to be null.
- Permit zero, one, or two resolved slots with internally consistent nulls.
- Permit null `date`; apply documented singles rules when `best_of` is missing.
- Require a valid tournament reference and at least one provenance mapping.
- Reject duplicate source slots and duplicate `match_id` values.
- Validate known dates against corrected tournament windows.

### Completed rows

- Reject `status = fixture` and require two resolved, distinct participants.
- Apply the status matrix to `winner_id` and `score`.
- Require `best_of` and validate its supported domain.
- Ensure the completed `match_id` no longer exists in the future release.
- Permit `date` to be null; never substitute a tournament date.

### Incremental and retroactive workflows

- Date, participant, seed, score, status, format, and tournament corrections do
  not change established IDs.
- Weekly audits report provenance and canonical-name changes even though URLs
  are no longer stored in match rows.
- Hourly and daily workflows cannot modify frozen historical partitions.
- A reviewed player merge or tournament-name correction may change older
  partitions only through the staged weekly retroactive path.
- Failed validation leaves published data and checksum baselines untouched.

## Migration and rollout

1. Add canonical player, tournament, and match crosswalks with collision tests.
2. Add the shared 19-column schema and status, format, list, date, null, and
   tournament-name validators.
3. Migrate `scheduled_on` to leading `date`; remove ranks and `source_url`.
4. Add `format`; normalize participant IDs and names to lists; convert
   `winner_id` to the winning participant ID list.
5. Preserve seeds and source `best_of` values 1, 3, and 5; backfill missing
   singles values with documented WTA/ATP tournament-draw rules.
6. Move all URLs and source metadata into provenance/source-file tables and
   verify every fixture retains a source mapping.
7. Correct canonical tournament metadata and rebuild every affected row in an
   isolated staging directory.
8. Pin deterministic Parquet settings and establish golden output fixtures.
9. Regenerate the coordinated breaking release and compare retained values and
   row counts with v3.1.
10. Update `docs/SCHEMA.md`, `README.md`, `TEST_IMPROVEMENT.md`, and
    `tests/README.md` with the same contract and regression suite.
11. Establish the new checksum baseline, then enable routine refresh and
    retroactive-audit automation.

## Acceptance criteria

The change is complete when:

- Every match-shaped dataset has the exact 19-column schema, order, and types.
- Singles and doubles use the same physical schema and pass participant-list
  validation.
- No public match row contains rank fields or `source_url`.
- Future rows allow unresolved participants but never expose a winner or score.
- Results and published fixtures contain a valid source or rule-derived
  `best_of` value.
- Every ID follows the canonical crosswalk rules and remains stable across the
  complete fixture-to-result lifecycle and metadata corrections.
- Every repeated tournament name equals its authoritative tournament record.
- Every row satisfies the date, status/result, null, placeholder, and format
  rules.
- Every fixture has compact provenance capable of resolving its source.
- Identical clean builds produce byte-identical, deterministically sorted
  Parquet outputs.
- Full historical validation, incremental immutability, rollover, migration,
  release, website, and retroactive-audit tests pass.

## Feedback workflow

Add each new request to **Accepted feedback** with its decision and status.
If feedback conflicts with an earlier contract, mark the superseded decision
and describe the migration explicitly rather than silently changing it.

## Open questions

None. The decisions above form the implemented v3.2 contract.
