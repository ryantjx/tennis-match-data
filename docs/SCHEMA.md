# Open Tennis Data v3 schema

Every match-shaped Parquet file has
`open_tennis_data_schema_version=3.3` and these 20 columns in exact order:

```text
date, match_id, tournament_id, tournament_name, tour, year, draw, round,
format, player1_id, player1_name, player1_seed,
player2_id, player2_name, player2_seed,
winner_id, status, score, best_of, source
```

Physical DuckDB/Arrow types:

```text
DATE, VARCHAR, VARCHAR, VARCHAR, VARCHAR, SMALLINT, VARCHAR, VARCHAR,
VARCHAR, VARCHAR[], VARCHAR[], VARCHAR,
VARCHAR[], VARCHAR[], VARCHAR,
VARCHAR[], VARCHAR, VARCHAR, TINYINT, VARCHAR[]
```

## Source attribution

`source` is a sorted, duplicate-free, non-empty list of canonical labels. It
captures every source that materially contributes to the public row:

- `sackmann`: identity or result cross-check;
- `tennis-data.co.uk`: accepted exact match-day evidence;
- `wikimedia`: draw, schedule, fixture, or result observation; and
- `community`: an applied approved correction.

The list form preserves attribution when one row combines evidence from
several sources. Detailed native IDs, URLs, hashes, and policy information
remain in `provenance.parquet` and `sources.parquet`.

## Lifecycle semantics

`completed.parquet` contains terminal statuses: `completed`, `walkover`,
`retired`, `defaulted`, `abandoned`, or `cancelled`. Each row has a non-null
`date` equal to accepted match-level evidence with day precision. Tournament
dates never fill this field.

`fixtures.parquet` contains only `status=fixture`. `winner_id` and `score` are
null. The date and either participant list may also be null until a schedule or
draw observation supplies them.

`matches.parquet` is exactly:

```sql
SELECT * FROM completed
UNION ALL
SELECT * FROM fixtures
```

`match_id` is unique across the union and remains stable when a fixture becomes
a terminal result.

## Tournaments and players

`tournaments.parquet`:

```text
tournament_id, tour, year, tournament_name, level, surface, indoor,
start_date, end_date, city, country, source_url
```

`players.parquet` retains the compatible identity schema from the local
dataset, filtered to players referenced by the release.

## Provenance

`provenance.parquet` contains:

```text
match_id, tour, year, source_file_id, source_match_id,
observation_kind, retrieved_at, content_sha256,
played_on, date_role, date_precision,
source_timezone, venue_timezone,
participants_side_1, participants_side_2, round, score,
match_method, row_fingerprint, parser_version, policy_revision
```

Historical source rows can have `retrieved_at=null`; this is why the current
release is preview. A stable release rejects missing retrieval timestamps.
Every terminal match must have a `match_date` observation whose `played_on`
equals the public date, `date_role=played`, and `date_precision=day`.

`sources.parquet` adds policy fields to each source-file record:

```text
policy_source, policy_state, terms_url, allowed_uses, allowed_fields,
attribution, rate_limit, parser_version, reviewed_at, policy_revision
```

## Coverage and health

`coverage.parquet` groups row counts by tour, year, tournament level, and
lifecycle. `coverage_status=preview` fails the stable gate;
`coverage_status=complete` is allowed only after expected tournament and draw
slots reconcile.

`health.parquet` records release `as_of`, completed/fixture counts, latest
known dates, and status per tour.

`catalog.parquet` records asset path, table name, row count, byte size,
SHA-256, and `as_of`. `manifest.json` includes the catalog itself; the catalog
does not hash itself, avoiding a checksum cycle.

## Deterministic layout

Match assets use Parquet V2, Zstandard compression, 65,536-row groups, one
writer thread, fixed column order, stable null-last ordering, and schema
metadata. Identical pinned observations, release timestamp, repository, and
tag must produce byte-identical assets.
