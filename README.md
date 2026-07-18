# tennis-match-data

A self-updating, provenance-first collection of men's and women's singles data,
from tour level through Challenger, WTA 125, ITF, and Futures. Completed
matches and future fixtures share one 19-column Parquet contract and are
published as separate lifecycle views. Annual tournament editions and compact
provenance remain auxiliary tables so match rows stay lean.

Repository: https://github.com/ryantjx/tennis-match-data

Dataset inventory, health, file URLs, and notes: [DATA.md](DATA.md)

Query data: [https://ryantjx.github.io/tennis-match-data/](https://ryantjx.github.io/tennis-match-data/)

## Direct Parquet downloads

These rolling files contain the exact-dated subset of completed singles
matches. Canonical repository history remains complete when an exact day is
unavailable; those nullable rows are intentionally absent from downloads.
The files are regenerated after validated data updates.
`mens.parquet` is an alias of `atp.parquet`; `womens.parquet` is an alias of
`wta.parquet`.

| Dataset | Download URL | Contents |
| --- | --- | --- |
| Men's matches | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/mens.parquet> | Exact-dated ATP completed matches |
| Women's matches | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/womens.parquet> | Exact-dated WTA completed matches |
| ATP | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/atp.parquet> | Exact-dated ATP completed matches |
| WTA | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/wta.parquet> | Exact-dated WTA completed matches |
| All matches | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/all-matches.parquet> | Combined exact-dated ATP and WTA completed matches |
| Tournaments | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/tournaments.parquet> | Annual ATP/WTA tournament editions |
| Provenance | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/provenance.parquet> | Match-to-source-file mappings |
| Ambiguities | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/ambiguities.parquet> | Ambiguous source observations and candidate match IDs |
| Sources | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/sources.parquet> | Referenced source URLs, revisions, checksums, and licences |

### Future-only downloads

The future-only release uses the same filenames. Change `data-latest` to
`future-latest` in any download URL:

| Download | Future-only URL |
| --- | --- |
| [Men's future matches](https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/mens.parquet) | ATP fixtures only |
| [Women's future matches](https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/womens.parquet) | WTA fixtures only |
| [ATP future matches](https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/atp.parquet) | ATP fixtures only |
| [WTA future matches](https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/wta.parquet) | WTA fixtures only |
| [All future matches](https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/all-matches.parquet) | Combined ATP and WTA fixtures |
| [Tournaments](https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/tournaments.parquet) | Annual editions referenced by fixtures |
| [Provenance](https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/provenance.parquet) | Fixture-to-source-file mappings |
| [Ambiguities](https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/ambiguities.parquet) | Ambiguous source evidence; normally an empty typed asset |
| [Sources](https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/sources.parquet) | Referenced source-file records |

Future files have the exact same columns and types as completed files. Their
`date` and participant lists may be null; `winner_id` and `score` are always
null. Dated rows before the catalog's `as_of` date are excluded, while undated
draw slots remain:

```sql
SELECT
  f.date, f.tour, f.tournament_name, f.round, f.format,
  array_to_string(f.player1_name, ' / ') AS player_or_team_1,
  array_to_string(f.player2_name, ' / ') AS player_or_team_2,
  f.status, f.best_of
FROM read_parquet(
  'https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/all-matches.parquet'
) AS f
LEFT JOIN read_parquet(
  'https://github.com/ryantjx/tennis-match-data/releases/download/future-latest/tournaments.parquet'
) AS t USING (tournament_id)
ORDER BY f.date NULLS LAST, f.tour, f.tournament_name;
```

## Quick start

### Data you can query

- Rolling `data-latest` downloads: completed matches for ATP, WTA, or both.
- Equivalent aliases: `mens.parquet` is ATP and `womens.parquet` is WTA.
- Future-only downloads: the same filenames under the `future-latest` release.
- Repository tables: matches, fixtures, tournaments, players, rankings,
  statistics, compact provenance, coverage, and health data.

### Ways to query

| Method | Use case |
| --- | --- |
| Polars | Lazy DataFrame queries directly against a download URL |
| DuckDB | SQL against remote downloads or local Parquet partitions |
| `open-tennis-data` CLI | Catalog-pruned queries, an interactive shell, and Parquet extracts |
| pandas or R | Standard Parquet analysis after downloading a file |

### Query with Polars

Install Polars with `python -m pip install polars`, then query the combined
rolling dataset. Change the filename to `atp.parquet`, `wta.parquet`,
`mens.parquet`, or `womens.parquet` for a tour-specific subset.

```python
import polars as pl

url = "https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/all-matches.parquet"

matches = (
    pl.scan_parquet(url)
    .select("tour", "tournament_id", "round", "player1_name", "player2_name", "score")
    .head(10)
    .collect()
)
print(matches)
```

### Query with DuckDB

Query the same rolling download directly with SQL:

```sql
SELECT m.tour, t.level, count(*) AS matches
FROM read_parquet(
  'https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/all-matches.parquet'
) AS m
JOIN read_parquet(
  'https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/tournaments.parquet'
) AS t USING (tournament_id)
GROUP BY m.tour, t.level
ORDER BY matches DESC;
```

### Query with the CLI

Clone the repository and install the CLI with Python 3.11 or newer:

```bash
git clone --depth 1 https://github.com/ryantjx/tennis-match-data.git
cd tennis-match-data
python -m pip install .
```

Query selected local partitions without loading the entire corpus:

```bash
open-tennis-data query --tour atp --years 2020:2025 --sql \
  "SELECT t.level, t.surface, count(*) AS matches
   FROM matches m JOIN tournaments t USING (tournament_id, tour, year)
   GROUP BY t.level, t.surface ORDER BY matches DESC"
```

Open an interactive DuckDB session with all tables registered as views:

```bash
open-tennis-data shell
```

Create a smaller Parquet extract:

```bash
open-tennis-data extract --tour wta --years 2015:2025 \
  --levels wta_125,itf --output wta-lower-level.parquet
```

You can also query any local partition directly:

```sql
SELECT player1_name, count(*) AS wins
FROM read_parquet('data/matches/tour=atp/year=2025/matches.parquet')
WHERE winner_id = player1_id
GROUP BY player1_name
ORDER BY wins DESC;
```

## Data layout

```text
data/
  catalog/catalog.parquet
  coverage/{coverage,source-audit}.parquet
  health/health.parquet
  matches/tour=atp/year=2025/matches.parquet
  tournaments/tour=atp/year=2025/tournaments.parquet
  match_stats/tour=atp/year=2025/match-stats.parquet
  observations/tour=atp/year=2025/observations.parquet
  date_observations/tour=atp/year=2025/date-observations.parquet
  rankings/tour=atp/year=2025/rankings.parquet
  players/tour=atp/players.parquet
  fixtures/tour=atp/current.parquet
  identity/tournament-sources.parquet
  conflicts/conflicts.parquet
  quarantine/quarantine.parquet
contributions/corrections.parquet
```

`catalog.parquet` is the entry point. It lists each data file, logical table,
tour/year partition, row count, byte size, SHA-256 checksum, dataset as-of date,
and pinned source revision.

The `matches` and `fixtures` tables use the same v3.2 match schema. Tournament
classification, surface, location, and date range live once in the
`tournaments` table; its canonical name is deliberately copied into match rows.
`observations` is a compact match-to-source crosswalk. Internal
`date_observations` records each accepted day-precision assertion and the
reconciliation method; unresolved or conflicting assertions stay quarantined.
File-level revision, URL, checksum, licence, and reconciliation totals live in
`source-audit.parquet`.

## Coverage and semantics

- ATP and WTA singles from 1968 onward.
- Grand Slams, tour events, qualifying, team events, ATP Challenger, WTA 125,
  and every ITF/Futures file available from approved sources.
- Historical ATP rankings from 1973 and WTA rankings from 1984.
- Match statistics where the source publishes them.
- Reusable Wikimedia completed results and best-effort fixture draw slots.
- Tournament `start_date` and `end_date` provide edition context. Match `date`
  is nullable, requires accepted day-precision evidence, and is never inferred
  from the tournament window. Completed releases contain only that exact-dated
  subset; the canonical partitions retain unresolved historical rows.

Coverage means complete ingestion of the approved available source files, not
proof that every tennis match ever played is represented. Inspect
`data/coverage/coverage.parquet`, `source-audit.parquet`, and
`data/health/health.parquet` before relying on a time period.

## Rebuild and verify

```bash
# Empty checkout only: one-time complete historical download.
open-tennis-data bootstrap --as-of "$(date -u +%F)"

# Reproducible full builds may pin the archive revision explicitly.
open-tennis-data build --source-revision <40-character-git-sha>

# Routine updates never rebuild older history.
open-tennis-data refresh-current --as-of "$(date -u +%F)"
open-tennis-data refresh-fixtures --as-of "$(date -u +%F)"
open-tennis-data audit-retroactive --as-of "$(date -u +%F)"

# One-time offline migration from a checked-in v3.1 data directory.
open-tennis-data migrate-v3-2 --data data --output staged-v3.2 \
  --report reports/v3.2

open-tennis-data validate
python -m unittest discover -s tests -v
```

`build` remains a deprecated full-build alias for one release cycle.

Builds download source-native CSV only into a temporary directory. No CSV,
JSON, JSONL, gzip dataset, or alternate master export is stored in the
repository.

## Sources and licences

Code is MIT. Sackmann/Tennis Abstract-derived observations are CC BY-NC-SA 4.0
and therefore non-commercial. Wikimedia observations are CC BY-SA 4.0.
Community corrections are CC0. Licences remain attached at source-file level;
read [DATA_LICENSE.md](DATA_LICENSE.md) and [docs/SOURCES.md](docs/SOURCES.md).

## Contributing

Use `open-tennis-data add-correction --help` to append a sourced CC0 proposal to
`contributions/corrections.parquet`. Collector, identity, and schema changes
must include offline tests and documentation. See
[CONTRIBUTING.md](CONTRIBUTING.md).
