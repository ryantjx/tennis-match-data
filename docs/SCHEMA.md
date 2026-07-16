# Parquet schemas

All published structured data is Parquet. The exact Arrow schema is the public
contract, and `open-tennis-data validate` checks schemas, checksums, uniqueness,
references, source reconciliation, and file limits.

## Completed matches

Match partitions and the `data-latest` release contain only completed result
records. Their columns, in order, are:

```text
match_id, tournament_id, tour, year, draw, round,
player1_id, player1_name, player1_country,
player2_id, player2_name, player2_country,
winner_id, loser_id,
player1_seed, player2_seed, player1_entry, player2_entry,
player1_rank, player2_rank, player1_rank_points, player2_rank_points,
status, score, best_of
```

`match_id` is a stable canonical identifier. Result `status` is one of
`completed`, `walkover`, `retired`, `defaulted`, or `abandoned`.

## Fixtures

Fixture partitions and the `future-latest` release use a separate schema:

```text
fixture_id, tournament_id, tour, year, draw, round,
player1_id, player1_name, player2_id, player2_name,
scheduled_on, source_url
```

Fixtures never have a `match_id`. `scheduled_on` is nullable because a draw can
be known before the order of play. Dated rows older than the dataset `as_of`
date are excluded from `future-latest`; undated draw slots remain.

## Annual tournaments

Tournament partitions and each rolling release include `tournaments.parquet`:

```text
tournament_id, tour, year, tournament_name, level, surface, indoor,
start_date, end_date, city, country, source_url
```

One ID represents one annual tour edition and is shared by its main and
qualifying draws. IDs use `tournament_{tour}_{year}_{stable_hash}`. ATP and WTA
editions remain separate even when they share a brand or venue. Start and end
dates describe the tournament window, not an individual match date.

## Auxiliary facts and provenance

- `players`: canonical player records and source identifiers.
- `match_stats`: sparse duration, service, and break-point totals.
- `rankings`: long-form ranking snapshots.
- `observations`: compact match-to-source rows containing only `match_id`,
  `tour`, `year`, `source_file_id`, and `source_match_id`.
- `source-audit`: one row per source file with URL, revision, checksum, licence,
  and source/normalized/quarantined counts.
- `tournament-sources` and `player-links`: persistent source crosswalks.
- `coverage`, `health`, `conflicts`, `quarantine`, and `corrections`: queryable
  quality and contribution state.

## Canonical levels

ATP values are `grand_slam`, `tour_finals`, `masters_1000`, `atp_500`,
`atp_250`, `challenger`, `itf`, `team`, `olympics`, or `other`. WTA values are
`grand_slam`, `tour_finals`, `wta_1000`, `wta_500`, `wta_250`, `wta_125`,
`itf`, `team`, `olympics`, or `other`.
