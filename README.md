# tennis-match-data

A self-updating, provenance-first collection of men's and women's singles data,
from tour level through Challenger, WTA 125, ITF, and Futures. Every published
structured data artifact is Parquet. Files carry no repository-wide identity
marker; relevant tables expose ordinary as-of, source, retrieval, and
observation dates instead.

Repository: https://github.com/ryantjx/tennis-match-data

## Direct Parquet downloads

These rolling files contain completed singles matches **and** the current
best-effort future fixtures. They are regenerated after validated data updates.
`mens.parquet` is an alias of `atp.parquet`; `womens.parquet` is an alias of
`wta.parquet`.

| Dataset | Download URL | Contents |
| --- | --- | --- |
| Men's matches | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/mens.parquet> | All ATP completed matches and future fixtures |
| Women's matches | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/womens.parquet> | All WTA completed matches and future fixtures |
| ATP | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/atp.parquet> | All ATP completed matches and future fixtures |
| WTA | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/wta.parquet> | All WTA completed matches and future fixtures |
| All matches | <https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/all-matches.parquet> | Combined ATP and WTA completed matches and future fixtures |

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

Every future-only row has `record_type = 'fixture'`. Dated fixtures are on or
after the catalog's `as_of` date; undated future draw slots are retained until
their source publishes an exact schedule or completed result.

Completed rows have `record_type = 'completed'`; scheduled rows have
`record_type = 'fixture'`. Query known future matches while retaining undated
future draw slots:

```sql
SELECT
  tour, event_name, round, player1_name, player2_name,
  scheduled_on, scheduled_at, schedule_date_source
FROM read_parquet(
  'https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/all-matches.parquet'
)
WHERE record_type = 'fixture'
  AND (
    coalesce(CAST(scheduled_at AS DATE), scheduled_on) >= current_date
    OR (scheduled_at IS NULL AND scheduled_on IS NULL)
  )
ORDER BY scheduled_on NULLS LAST, scheduled_at NULLS LAST, tour, event_name;
```

## Quick start

### Data you can query

- Rolling downloads: completed matches plus current fixtures for ATP, WTA, or
  both tours. Use `atp.parquet`, `wta.parquet`, or `all-matches.parquet`.
- Equivalent aliases: `mens.parquet` is ATP and `womens.parquet` is WTA.
- Future-only downloads: the same filenames under the `future-latest` release.
- Repository tables: matches, fixtures, events, players, rankings, statistics,
  observations, identity mappings, coverage, and health data.

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
    .filter(pl.col("record_type") == "completed")
    .select("tour", "event_name", "round", "player1_name", "player2_name", "score")
    .head(10)
    .collect()
)
print(matches)
```

### Query with DuckDB

Query the same rolling download directly with SQL:

```sql
SELECT tour, level, count(*) AS matches
FROM read_parquet(
  'https://github.com/ryantjx/tennis-match-data/releases/download/data-latest/all-matches.parquet'
)
WHERE record_type = 'completed'
GROUP BY tour, level
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
  "SELECT level, surface, count(*) AS matches
   FROM matches GROUP BY level, surface ORDER BY matches DESC"
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
  events/tour=atp/year=2025/events.parquet
  match_stats/tour=atp/year=2025/match-stats.parquet
  observations/tour=atp/year=2025/observations.parquet
  rankings/tour=atp/year=2025/rankings.parquet
  players/tour=atp/players.parquet
  fixtures/tour=atp/current.parquet
  identity/{events,players,matches}/...
  conflicts/conflicts.parquet
  quarantine/quarantine.parquet
contributions/corrections.parquet
```

`catalog.parquet` is the entry point. It lists each data file, logical table,
tour/year partition, row count, byte size, SHA-256 checksum, dataset as-of date,
and pinned source revision.

The `matches` fact table repeats commonly queried event and player attributes.
Parquet dictionary encoding keeps this compact while avoiding joins for routine
analysis. Dimension, statistics, ranking, identity, and observation tables are
available when deeper provenance or biographical detail is needed.

## Coverage and semantics

- ATP and WTA singles from 1968 onward.
- Grand Slams, tour events, qualifying, team events, ATP Challenger, WTA 125,
  and every ITF/Futures file available from approved sources.
- Historical ATP rankings from 1973 and WTA rankings from 1984.
- Match statistics where the source publishes them.
- Reusable Wikimedia completed results and best-effort future draw slots.
- `event_start_date` is never presented as an exact match date. `played_on` is
  null unless a source provides the day, and `played_on_precision` explains the
  distinction.

Coverage means complete ingestion of the approved available source files, not
proof that every tennis match ever played is represented. Inspect
`data/coverage/coverage.parquet`, `source-audit.parquet`, and
`data/health/health.parquet` before relying on a time period.

## Rebuild and verify

```bash
open-tennis-data build --years 1968:$(date -u +%Y) --as-of "$(date -u +%F)"
open-tennis-data validate
python -m unittest discover -s tests -v
```

Builds download source-native CSV only into a temporary directory. No CSV,
JSON, JSONL, gzip dataset, or alternate master export is stored in the
repository.

## Sources and licences

Code is MIT. Sackmann/Tennis Abstract-derived observations are CC BY-NC-SA 4.0
and therefore non-commercial. Wikimedia observations are CC BY-SA 4.0.
Community corrections are CC0. Licences remain attached at observation level;
read [DATA_LICENSE.md](DATA_LICENSE.md) and [docs/SOURCES.md](docs/SOURCES.md).

## Contributing

Use `open-tennis-data add-correction --help` to append a sourced CC0 proposal to
`contributions/corrections.parquet`. Collector, identity, and schema changes
must include offline tests and documentation. See
[CONTRIBUTING.md](CONTRIBUTING.md).
