# Data reference

This is the operational index for the published tennis data: what each file
contains, where to obtain it, how to assess its health, and which limitations
matter when querying it. All structured data artifacts are Parquet.

## Health

[![Validate data](https://github.com/ryantjx/tennis-match-data/actions/workflows/ci.yml/badge.svg)](https://github.com/ryantjx/tennis-match-data/actions/workflows/ci.yml)

The live source of truth is
<https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/health/health.parquet>.
It contains one row per tour with the dataset `as_of` date, match and tournament
counts, earliest and latest tournament dates, latest ranking date, ranking row count,
quarantined row count, and overall status.

| Status | Meaning |
| --- | --- |
| `healthy` | Rankings exist and the latest ranking date is no more than 14 days behind `as_of`. |
| `stale` | Rankings exist, but the latest ranking date is more than 14 days behind `as_of`. Match results may still be current. |
| `unhealthy` | A required ranking partition is missing. |

Health has several independent dimensions:

| Check | Authoritative file | Notes |
| --- | --- | --- |
| File inventory and integrity | [`catalog.parquet`](https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/catalog/catalog.parquet) | Lists every published data path, row count, byte size, SHA-256 checksum, `as_of` date, and pinned source revision. |
| Tour freshness | [`health.parquet`](https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/health/health.parquet) | Separates dataset `as_of` time from the latest tournament and ranking dates. |
| Historical completeness | [`coverage.parquet`](https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/coverage/coverage.parquet) | Reports rows, tournaments, scores, and statistics by tour, year, level, and draw. |
| Source reconciliation | [`source-audit.parquet`](https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/coverage/source-audit.parquet) | Enforces `source_rows = normalized_rows + quarantined_rows` for match inputs. |
| Rejected inputs | [`quarantine.parquet`](https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/quarantine/quarantine.parquet) | Every rejected source row has an explicit reason. |
| Ambiguous records | [`conflicts.parquet`](https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/conflicts/conflicts.parquet) | Records that cannot be reconciled unambiguously remain separate from canonical matches. |

An HTTP-successful download is not, by itself, proof that every source is fresh.
Check `health.parquet` and the relevant coverage rows before analysis.

## Match downloads

The `data-latest` release is the easiest result entry point. Match files contain
completed matches only; `tournaments.parquet` contains the annual editions they
reference.

| Dataset | URL | Health | Notes |
| --- | --- | --- | --- |
| Men's matches | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/mens.parquet> | Rolling, CI-validated release | Byte-identical alias of `atp.parquet`; ATP completed matches. |
| Women's matches | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/womens.parquet> | Rolling, CI-validated release | Byte-identical alias of `wta.parquet`; WTA completed matches. |
| ATP | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/atp.parquet> | Rolling, CI-validated release | Men's singles across tour, Challenger, qualifying, team, ITF, and Futures coverage available from approved sources. |
| WTA | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/wta.parquet> | Rolling, CI-validated release | Women's singles across tour, WTA 125, qualifying, team, and ITF coverage available from approved sources. |
| All matches | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/all-matches.parquet> | Rolling, CI-validated release | Combined ATP and WTA file; the most convenient source for cross-tour queries. |
| Tournaments | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/tournaments.parquet> | Rolling, CI-validated release | Annual tournament editions referenced by the match files. |

The rolling assets are replaced only after schema, integrity, reconciliation,
compression, row-group, file-size, and query checks pass. Their stable URLs
always point to the latest published assets rather than an immutable snapshot.

## Future matches

The `future-latest` release uses fixture-specific files. Fixtures deliberately
have no `match_id`; they join to the included `tournaments.parquet` by
`tournament_id`.

| Dataset | URL | Health | Notes |
| --- | --- | --- | --- |
| Men's future matches | <https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/mens.parquet> | Best effort; CI-validated | Byte-identical alias of `atp.parquet`; ATP fixtures only. |
| Women's future matches | <https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/womens.parquet> | Best effort; CI-validated | Byte-identical alias of `wta.parquet`; WTA fixtures only. |
| ATP future matches | <https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/atp.parquet> | Best effort; CI-validated | Current/future ATP fixtures, including undated draw slots. |
| WTA future matches | <https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/wta.parquet> | Best effort; CI-validated | Current/future WTA fixtures, including undated draw slots. |
| All future matches | <https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/all-matches.parquet> | Best effort; CI-validated | Combined ATP and WTA fixtures. |
| Tournaments | <https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/tournaments.parquet> | Best effort; CI-validated | Annual editions referenced by fixtures. |

Rows with a known `scheduled_on` are retained when that date is on or after the
catalog's `as_of` date. Undated tentative draw slots are also retained.
Participants and dates may be null. Fixtures are not a complete schedule
service and may be revised or replaced by completed results.

```sql
SELECT f.tour, t.tournament_name, f.round,
       f.player1_name, f.player2_name, f.scheduled_on
FROM read_parquet(
  'https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/all-matches.parquet'
) AS f
LEFT JOIN read_parquet(
  'https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/tournaments.parquet'
) AS t USING (tournament_id)
ORDER BY f.scheduled_on NULLS LAST, t.tournament_name;
```

## Key repository files

Use the partitioned repository tables for projection and predicate pushdown,
detailed provenance, rankings, statistics, or quality analysis.

| File or directory | URL | Health role and notes |
| --- | --- | --- |
| `data/catalog/catalog.parquet` | <https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/catalog/catalog.parquet> | Entry point for file discovery, partition pruning, checksums, row counts, and source revision. |
| `data/matches/` | <https://github.com/ryantjx/tennis-match-data/tree/main/data/matches> | Canonical completed-match facts partitioned by `tour` and `year`; duplicate IDs and broken tournament references fail validation. |
| `data/tournaments/` | <https://github.com/ryantjx/tennis-match-data/tree/main/data/tournaments> | Annual tournament editions shared by main and qualifying draws, with classification, surface, location, dates, and source URL. |
| `data/players/` | <https://github.com/ryantjx/tennis-match-data/tree/main/data/players> | Canonical players, names, source IDs, country, birth date, hand, and physical attributes where available. |
| `data/match_stats/` | <https://github.com/ryantjx/tennis-match-data/tree/main/data/match_stats> | Duration, serve, return, and break-point facts where a source publishes them. Absence means unavailable, not zero. |
| `data/rankings/` | <https://github.com/ryantjx/tennis-match-data/tree/main/data/rankings> | Long-form ranking snapshots. Ranking freshness directly affects the tour health status. |
| `data/observations/` | <https://github.com/ryantjx/tennis-match-data/tree/main/data/observations> | Compact `match_id` to source-file/source-match crosswalk. File metadata is stored once in `source-audit.parquet`. |
| `data/fixtures/` | <https://github.com/ryantjx/tennis-match-data/tree/main/data/fixtures> | Current/next-year best-effort fixtures by tour, published separately in `future-latest`. |
| `data/identity/tournament-sources.parquet` | <https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/identity/tournament-sources.parquet> | Persistent source crosswalks that keep annual tournament IDs stable after metadata corrections. |
| `data/coverage/coverage.parquet` | <https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/coverage/coverage.parquet> | Completeness by tour, year, level, draw, date, score, and statistics availability. |
| `data/coverage/source-audit.parquet` | <https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/coverage/source-audit.parquet> | Input file URL, revision, checksum, and row reconciliation. |
| `data/health/health.parquet` | <https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/health/health.parquet> | Current ATP/WTA freshness and high-level counts. Query this instead of relying on a copied status. |
| `data/conflicts/conflicts.parquet` | <https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/conflicts/conflicts.parquet> | Unresolved cross-source ambiguities; may be empty. |
| `data/quarantine/quarantine.parquet` | <https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/quarantine/quarantine.parquet> | Rejected source records and reasons; non-empty quarantine is visible rather than silently dropped. |
| `contributions/corrections.parquet` | <https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/contributions/corrections.parquet> | Sourced CC0 community correction proposals and their status. |

## Query live health

```sql
SELECT
  tour, status, as_of, latest_tournament_date, latest_ranking_date,
  match_count, tournament_count, quarantined_rows
FROM read_parquet(
  'https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/health/health.parquet'
)
ORDER BY tour;
```

To inspect a particular period or level, query coverage directly:

```sql
SELECT tour, year, level, draw, row_count, tournament_count,
       score_count, statistics_count
FROM read_parquet(
  'https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/coverage/coverage.parquet'
)
WHERE year >= 2020
ORDER BY tour, year, level, draw;
```

## Important semantics

- Coverage means complete ingestion of approved available sources, not proof
  that every tennis match ever played is present.
- Tournament dates describe the edition, not the exact match day.
- Tournament and player names are display attributes, not identity keys. Use the
  canonical IDs for joins.
- Statistics, rankings, biographical attributes, schedule dates, and some
  participants are nullable because source availability varies.
- Source licences remain attached to file-level provenance records. See
  [DATA_LICENSE.md](DATA_LICENSE.md) before redistribution or commercial use.

## Update cadence

- Hourly: current reusable Wikimedia results and fixtures.
- Daily: current source files, rankings, and affected partitions.
- Weekly: exhaustive local validation of all history, plus upstream revision
  review for the previous/current result years and current/next-year fixtures.
- After validated updates: replace the rolling `data-latest` and
  `future-latest` release assets.

All builders write to an isolated location and validate before promotion.
Routine automation cannot publish duplicate canonical IDs, broken references,
schema drift, unreconciled source rows, oversized files, or missing required
ranking data.
