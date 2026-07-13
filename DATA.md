# Data reference

This is the operational index for the published tennis data: what each file
contains, where to obtain it, how to assess its health, and which limitations
matter when querying it. All structured data artifacts are Parquet.

## Health

[![Validate data](https://github.com/ryantjx/tennis-match-data/actions/workflows/ci.yml/badge.svg)](https://github.com/ryantjx/tennis-match-data/actions/workflows/ci.yml)

The live source of truth is
<https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/health/health.parquet>.
It contains one row per tour with the dataset `as_of` date, match and event
counts, earliest and latest event dates, latest ranking date, ranking row count,
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
| Tour freshness | [`health.parquet`](https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/health/health.parquet) | Separates dataset observation time from the latest event and ranking dates. |
| Historical completeness | [`coverage.parquet`](https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/coverage/coverage.parquet) | Reports rows, events, exact dates, scores, and statistics by tour, year, level, and draw. |
| Source reconciliation | [`source-audit.parquet`](https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/coverage/source-audit.parquet) | Enforces `source_rows = normalized_rows + quarantined_rows` for match inputs. |
| Rejected inputs | [`quarantine.parquet`](https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/quarantine/quarantine.parquet) | Every rejected source row has an explicit reason. |
| Ambiguous records | [`conflicts.parquet`](https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/conflicts/conflicts.parquet) | Records that cannot be reconciled unambiguously remain separate from canonical matches. |

An HTTP-successful download is not, by itself, proof that every source is fresh.
Check `health.parquet` and the relevant coverage rows before analysis.

## Match downloads

The `data-latest` release is the easiest entry point. Each file has a flat
superset schema containing canonical completed matches and the current
best-effort fixtures. Use `record_type = 'completed'` for results and
`record_type = 'fixture'` for scheduled or tentative matches.

| Dataset | URL | Health | Notes |
| --- | --- | --- | --- |
| Men's matches | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/mens.parquet> | Rolling, CI-validated release | Byte-identical alias of `atp.parquet`; ATP completed matches and fixtures. |
| Women's matches | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/womens.parquet> | Rolling, CI-validated release | Byte-identical alias of `wta.parquet`; WTA completed matches and fixtures. |
| ATP | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/atp.parquet> | Rolling, CI-validated release | Men's singles across tour, Challenger, qualifying, team, ITF, and Futures coverage available from approved sources. |
| WTA | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/wta.parquet> | Rolling, CI-validated release | Women's singles across tour, WTA 125, qualifying, team, and ITF coverage available from approved sources. |
| All matches | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/all-matches.parquet> | Rolling, CI-validated release | Combined ATP and WTA file; the most convenient source for cross-tour queries. |

The rolling assets are replaced only after schema, integrity, reconciliation,
compression, row-group, file-size, and query checks pass. Their stable URLs
always point to the latest published assets rather than an immutable snapshot.

## Future matches

The `future-latest` release uses the same schema and filenames as
`data-latest`, but contains fixtures only.

| Dataset | URL | Health | Notes |
| --- | --- | --- | --- |
| Men's future matches | <https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/mens.parquet> | Best effort; CI-validated | Byte-identical alias of `atp.parquet`; ATP fixtures only. |
| Women's future matches | <https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/womens.parquet> | Best effort; CI-validated | Byte-identical alias of `wta.parquet`; WTA fixtures only. |
| ATP future matches | <https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/atp.parquet> | Best effort; CI-validated | Dated current/future ATP fixtures plus undated future draw slots. |
| WTA future matches | <https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/wta.parquet> | Best effort; CI-validated | Dated current/future WTA fixtures plus undated future draw slots. |
| All future matches | <https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/all-matches.parquet> | Best effort; CI-validated | Combined ATP and WTA fixtures. Every row has `record_type = 'fixture'`. |

Dated rows are retained when `coalesce(CAST(scheduled_at AS DATE),
scheduled_on)` is on or after the catalog's `as_of` date. Undated draw slots
are retained because a known future pairing may not yet have an exact schedule.
Nullable participants, dates, and times are expected. Fixtures are not a
complete schedule service and may be revised or replaced by completed results.

```sql
SELECT tour, event_name, round, player1_name, player2_name,
       scheduled_on, scheduled_at
FROM read_parquet(
  'https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/all-matches.parquet'
)
ORDER BY scheduled_on NULLS LAST, scheduled_at NULLS LAST;
```

## Key repository files

Use the partitioned repository tables for projection and predicate pushdown,
detailed provenance, rankings, statistics, or quality analysis.

| File or directory | URL | Health role and notes |
| --- | --- | --- |
| `data/catalog/catalog.parquet` | <https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/catalog/catalog.parquet> | Entry point for file discovery, partition pruning, checksums, row counts, and source revision. |
| `data/matches/` | <https://github.com/ryantjx/tennis-match-data/tree/main/data/matches> | Canonical match facts partitioned by `tour` and `year`; duplicate IDs and broken player/event references fail validation. |
| `data/events/` | <https://github.com/ryantjx/tennis-match-data/tree/main/data/events> | Stable tournament-draw identities, dates, level, location, surface, draw size, and source identifiers. Repeated weekly ITF events remain distinct. |
| `data/players/` | <https://github.com/ryantjx/tennis-match-data/tree/main/data/players> | Canonical players, names, source IDs, country, birth date, hand, and physical attributes where available. |
| `data/match_stats/` | <https://github.com/ryantjx/tennis-match-data/tree/main/data/match_stats> | Duration, serve, return, and break-point facts where a source publishes them. Absence means unavailable, not zero. |
| `data/rankings/` | <https://github.com/ryantjx/tennis-match-data/tree/main/data/rankings> | Long-form ranking snapshots. Ranking freshness directly affects the tour health status. |
| `data/observations/` | <https://github.com/ryantjx/tennis-match-data/tree/main/data/observations> | Source-native observations, fingerprints, revisions, URLs, checksums, retrieval dates, and licences. Multiple observations may support one canonical match. |
| `data/fixtures/` | <https://github.com/ryantjx/tennis-match-data/tree/main/data/fixtures> | Current best-effort fixtures by tour. These are also folded into the rolling downloads and isolated in `future-latest`. |
| `data/identity/` | <https://github.com/ryantjx/tennis-match-data/tree/main/data/identity> | Persistent player, event, and match links. Published canonical IDs are not reused. |
| `data/coverage/coverage.parquet` | <https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/coverage/coverage.parquet> | Completeness by tour, year, level, draw, date, score, and statistics availability. |
| `data/coverage/source-audit.parquet` | <https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/coverage/source-audit.parquet> | Input file URL, revision, checksum, and row reconciliation. |
| `data/health/health.parquet` | <https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/health/health.parquet> | Current ATP/WTA freshness and high-level counts. Query this instead of relying on a copied status. |
| `data/conflicts/conflicts.parquet` | <https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/conflicts/conflicts.parquet> | Unresolved cross-source ambiguities; may be empty. |
| `data/quarantine/quarantine.parquet` | <https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/quarantine/quarantine.parquet> | Rejected source records and reasons; non-empty quarantine is visible rather than silently dropped. |
| `contributions/corrections.parquet` | <https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/contributions/corrections.parquet> | Sourced CC0 community correction proposals and their status. |

## Query live health

```sql
SELECT
  tour, status, as_of, latest_event_date, latest_ranking_date,
  match_count, event_count, quarantined_rows
FROM read_parquet(
  'https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/health/health.parquet'
)
ORDER BY tour;
```

To inspect a particular period or level, query coverage directly:

```sql
SELECT tour, year, level, draw, row_count, event_count,
       exact_date_count, score_count, statistics_count
FROM read_parquet(
  'https://raw.githubusercontent.com/ryantjx/tennis-match-data/main/data/coverage/coverage.parquet'
)
WHERE year >= 2020
ORDER BY tour, year, level, draw;
```

## Important semantics

- Coverage means complete ingestion of approved available sources, not proof
  that every tennis match ever played is present.
- `event_start_date` is the tournament start date. It is never exposed as an
  exact match date.
- `played_on` is populated only when a source provides the actual match day.
  `played_on_precision` is `day`, `event_only`, or `unknown`.
- Event and player names are display attributes, not identity keys. Use the
  canonical IDs for joins.
- Statistics, rankings, biographical attributes, schedule times, and some
  participants are nullable because source availability varies.
- Source and observation licences remain attached to provenance records. See
  [DATA_LICENSE.md](DATA_LICENSE.md) before redistribution or commercial use.

## Update cadence

- Hourly: current reusable Wikimedia results and fixtures.
- Daily: current source files, rankings, and affected partitions.
- Weekly: full historical source, checksum, reconciliation, and coverage audit.
- After validated updates: replace the rolling `data-latest` and
  `future-latest` release assets.

All builders write to an isolated location and validate before promotion.
Routine automation cannot publish duplicate canonical IDs, broken references,
schema drift, unreconciled source rows, oversized files, or missing required
ranking data.
