# Open Tennis Data v3

Open Tennis Data v3 is a self-updating research dataset of ATP/WTA top-level
main-draw singles matches from 2020 onward. Verified results and future
fixtures are distributed as deterministic Parquet files through GitHub
Releases and can be explored in the read-only browser interface or queried
with DuckDB, `curl`, and the included CLI.

V3 currently publishes preview releases. The stable `latest` channel remains
fail-closed until the closed-event completeness gate in
[`OBJECTIVE.md`](OBJECTIVE.md) passes.

## Explore in your browser

The [Open Tennis Data Explorer](https://ryantjx.github.io/tennis-match-data/)
lists the players, tournaments, seasons, levels, and surfaces available in the
newest published v3 release. Its guided filters and match results run locally
in the browser against a checksum-verified Pages snapshot; it is not a hosted
query API, and GitHub Releases remain the canonical distribution.

To preview the site locally, prepare a release snapshot and serve the
repository root:

```bash
python3 scripts/prepare-site-data.py --output site/data
python3 -m http.server 8000 --directory .
```

Then open <http://127.0.0.1:8000/site/>. Pin an immutable preview when needed
with `--tag data-v3-YYYYMMDDTHHMMSSZ`. Generated files under `site/data/` are
deployment artifacts and are never committed.

## Install

Python 3.11+ is required.

```bash
python -m pip install .
```

For a source checkout, use a shallow clone because preserved Git history still
contains legacy generated data:

```bash
git clone --depth 1 https://github.com/ryantjx/tennis-match-data.git
cd tennis-match-data
python -m pip install .
```

## Download with curl

Once the first stable release is available, these URLs remain stable:

```bash
curl -L -o matches.parquet \
  https://github.com/ryantjx/tennis-match-data/releases/latest/download/matches.parquet

curl -L -o manifest.json \
  https://github.com/ryantjx/tennis-match-data/releases/latest/download/manifest.json

curl -L -o SHA256SUMS \
  https://github.com/ryantjx/tennis-match-data/releases/latest/download/SHA256SUMS
```

For a preview or immutable release, replace `latest` with a tag:

```bash
curl -L -o completed.parquet \
  https://github.com/ryantjx/tennis-match-data/releases/download/TAG/completed.parquet
```

GitHub Releases are used for generated binaries; Parquet files are not added
to new Git history. See [GitHub’s release documentation](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases).

## Query from the CLI

Query a release without downloading the full file:

```bash
open-tennis-data query --release latest --sql \
  "SELECT tour, year, count(*) matches
   FROM matches
   GROUP BY tour, year
   ORDER BY year, tour"
```

`matches` is the completed projection. `fixtures` contains future slots and
`all_matches` is the combined view.

Use the convenience command when SQL is unnecessary:

```bash
open-tennis-data matches \
  --release latest \
  --tour atp \
  --from 2025-01-01 \
  --to 2025-12-31 \
  --player Sinner \
  --status completed \
  --format jsonl
```

Available output formats are `table`, `csv`, `json`, and `jsonl`. Release
selection is mutually exclusive with local `--data`:

```bash
open-tennis-data query \
  --release data-v3-20260724T041700Z \
  --sql "SELECT * FROM all_matches LIMIT 10"

open-tennis-data query \
  --data data \
  --tour wta \
  --years 2025 \
  --sql "SELECT * FROM matches LIMIT 10"
```

Open a local or remote interactive shell:

```bash
open-tennis-data shell --release latest
open-tennis-data shell --data data
```

Write a filtered local Parquet extract:

```bash
open-tennis-data extract \
  --release latest \
  --tour wta \
  --years 2024:2025 \
  --output wta-2024-2025.parquet
```

DuckDB’s `httpfs` extension uses Parquet metadata, filter/projection pushdown,
and HTTP range requests, so remote queries need not download every column or
row group. See [DuckDB HTTP(S) support](https://duckdb.org/docs/lts/core_extensions/httpfs/https).

More examples, including integrity, provenance, coverage, health, and
quarantine checks, are collected in
[`docs/QUERIES.md`](docs/QUERIES.md). A runnable Polars version is available
in [`notebooks/open_tennis_data_v3_polars.ipynb`](notebooks/open_tennis_data_v3_polars.ipynb).

## Direct DuckDB query

```sql
SELECT tour, tournament_name, date, round,
       player1_name, player2_name, score
FROM read_parquet(
  'https://github.com/ryantjx/tennis-match-data/releases/latest/download/completed.parquet'
)
WHERE date BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'
ORDER BY date, tournament_name;
```

## Release assets

| Asset | Contents |
| --- | --- |
| `matches.parquet` | Completed matches plus fixtures |
| `completed.parquet` | Terminal rows with accepted match-level day evidence |
| `fixtures.parquet` | Future slots; date/participants may be null |
| `tournaments.parquet` | Referenced annual tournament identities |
| `players.parquet` | Referenced player identities |
| `provenance.parquet` | Source-native observations, hashes, date evidence, parser/policy versions |
| `sources.parquet` | URLs, terms, attribution, policy, checksums, and reconciliation counts |
| `coverage.parquet` | Tour/year/level/lifecycle coverage and gate status |
| `health.parquet` | Release freshness and lifecycle totals |
| `quarantine.parquet` | Malformed, ambiguous, conflicting, or unsupported observations |
| `catalog.parquet` | Row counts, sizes, checksums, and release timestamp |
| `manifest.json` | Public release contract and stable asset URLs |
| `SHA256SUMS` | Checksums for every release payload |

Match-shaped assets use the v3.3 20-column schema. The final `source` column is
a sorted, non-empty list of canonical source labels contributing to the row.
See [`docs/SCHEMA.md`](docs/SCHEMA.md).

## Build and verify

Build an empty 2020+ staging dataset:

```bash
open-tennis-data bootstrap \
  --through-year 2026 \
  --as-of 2026-07-24 \
  --output staged-data
```

Build and verify release assets:

```bash
open-tennis-data release \
  --data staged-data \
  --output dist/v3-release \
  --as-of 2026-07-24T04:17:00Z \
  --tag data-v3-20260724T041700Z

open-tennis-data verify-release --directory dist/v3-release
```

`--require-complete` additionally enforces stable coverage, retrieval-time, and
freshness gates:

```bash
open-tennis-data verify-release \
  --directory dist/v3-release \
  --require-complete \
  --max-age-hours 30
```

## Scope and terms

V3 includes top-level ATP/WTA main-draw singles, Grand Slams, tour finals,
Olympics, and team-event singles from 2020 onward. It excludes qualifying,
Challenger, WTA 125, ITF/Futures, doubles, rankings, and match statistics.

Software is MIT licensed. Published data is a source-attributed research
dataset with source-specific obligations and must not be assumed commercially
reusable. Read [`DATA_LICENSE.md`](DATA_LICENSE.md) and
[`docs/SOURCES.md`](docs/SOURCES.md).

Corrections and collector changes are described in
[`CONTRIBUTING.md`](CONTRIBUTING.md). The companion frontend is read-only and
does not change the absence of a hosted query service.
