# Open Tennis Data v3 query cookbook

These examples provide quick checks for a local dataset, a published release,
and downloaded Parquet assets. Replace `TAG` with an immutable release tag
while v3 remains preview. Use `latest` only after the manifest reports
`release_status=stable`.

## Download and verify a release

Download the release contract first:

```bash
base=https://github.com/ryantjx/tennis-match-data/releases/download/TAG

curl -fL -O "$base/manifest.json"
curl -fL -O "$base/SHA256SUMS"
curl -fL -O "$base/matches.parquet"
curl -fL -O "$base/completed.parquet"
curl -fL -O "$base/fixtures.parquet"
```

Download every asset named by the manifest:

```bash
jq -r '.assets[].url' manifest.json |
  while IFS= read -r url; do curl -fLO "$url"; done
```

Verify all downloaded payloads:

```bash
shasum -a 256 --check SHA256SUMS
open-tennis-data verify-release --directory .
```

Inspect the release status, scope, and asset inventory:

```bash
jq '{release_status, release_tag, as_of, scope, preview_reasons}' manifest.json
jq -r '.assets[] | [.name, .rows, .bytes, .sha256] | @tsv' manifest.json
```

## CLI filters without SQL

Completed and scheduled ATP matches in a date range:

```bash
open-tennis-data matches \
  --release TAG \
  --tour atp \
  --from 2026-01-01 \
  --to 2026-12-31 \
  --status completed,fixture \
  --limit 200
```

Matches involving a player:

```bash
open-tennis-data matches \
  --release TAG \
  --player Sinner \
  --from 2025-01-01 \
  --format jsonl
```

WTA matches at a named tournament:

```bash
open-tennis-data matches \
  --release TAG \
  --tour wta \
  --tournament Wimbledon \
  --status completed \
  --format csv
```

The same commands work on a local build by replacing `--release TAG` with
`--data staged-data`.

## SQL through the CLI

`matches` is the completed projection, `fixtures` contains future slots, and
`all_matches` is their union.

### Counts by tour, year, and status

```bash
open-tennis-data query --release TAG --sql "
  SELECT tour, year, status, count(*) AS matches
  FROM all_matches
  GROUP BY tour, year, status
  ORDER BY year, tour, status
"
```

### Recent results

```bash
open-tennis-data query --release TAG --format json --sql "
  SELECT date, tour, tournament_name, round,
         player1_name, player2_name, score
  FROM matches
  ORDER BY date DESC, tournament_name, round
  LIMIT 50
"
```

### Upcoming dated fixtures

```bash
open-tennis-data query --release TAG --sql "
  SELECT date, tour, tournament_name, round,
         player1_name, player2_name
  FROM fixtures
  WHERE date BETWEEN current_date AND current_date + INTERVAL 14 DAY
  ORDER BY date, tour, tournament_name, round
"
```

### Fixtures awaiting a date or participant

```bash
open-tennis-data query --release TAG --sql "
  SELECT match_id, tour, tournament_name, round, date,
         player1_name, player2_name
  FROM fixtures
  WHERE date IS NULL
     OR player1_name IS NULL
     OR player2_name IS NULL
  ORDER BY tour, tournament_name, round, match_id
"
```

### Search for a player

Player names are lists so that team-event participants remain compatible with
the 19-column schema.

```bash
open-tennis-data query --release TAG --sql "
  SELECT date, tour, tournament_name, round,
         player1_name, player2_name, status, score
  FROM all_matches
  WHERE lower(array_to_string(player1_name, ' / ')) LIKE '%alcaraz%'
     OR lower(array_to_string(player2_name, ' / ')) LIKE '%alcaraz%'
  ORDER BY date DESC NULLS LAST
"
```

### Head-to-head results

```bash
open-tennis-data query --release TAG --sql "
  SELECT date, tournament_name, round,
         player1_name, player2_name, winner_id, score
  FROM matches
  WHERE (
    lower(array_to_string(player1_name, ' / ')) LIKE '%sinner%'
    AND lower(array_to_string(player2_name, ' / ')) LIKE '%alcaraz%'
  ) OR (
    lower(array_to_string(player1_name, ' / ')) LIKE '%alcaraz%'
    AND lower(array_to_string(player2_name, ' / ')) LIKE '%sinner%'
  )
  ORDER BY date
"
```

### Tournament summaries

```bash
open-tennis-data query --release TAG --sql "
  SELECT t.tour, t.year, t.level, t.tournament_name,
         min(m.date) AS first_match_date,
         max(m.date) AS last_match_date,
         count(*) AS matches
  FROM matches m
  JOIN tournaments t USING (tournament_id, tour, year)
  GROUP BY ALL
  ORDER BY t.year DESC, t.tour, first_match_date
"
```

### Match counts by round

```bash
open-tennis-data query --release TAG --sql "
  SELECT tour, tournament_name, round, count(*) AS matches
  FROM matches
  WHERE year = 2025
  GROUP BY tour, tournament_name, round
  ORDER BY tour, tournament_name, round
"
```

### Walkovers, retirements, and cancellations

```bash
open-tennis-data query --release TAG --sql "
  SELECT status, count(*) AS matches
  FROM matches
  WHERE status <> 'completed'
  GROUP BY status
  ORDER BY matches DESC, status
"
```

## Data-integrity checks

These queries should all return zero.

### Duplicate public match IDs

```bash
open-tennis-data query --release TAG --sql "
  SELECT count(*) - count(DISTINCT match_id) AS duplicate_match_ids
  FROM all_matches
"
```

### Terminal rows without a match date

```bash
open-tennis-data query --release TAG --sql "
  SELECT count(*) AS invalid_terminal_rows
  FROM matches
  WHERE date IS NULL OR status = 'fixture'
"
```

### Fixtures carrying result data

```bash
open-tennis-data query --release TAG --sql "
  SELECT count(*) AS invalid_fixture_rows
  FROM fixtures
  WHERE status <> 'fixture'
     OR winner_id IS NOT NULL
     OR score IS NOT NULL
"
```

### Out-of-scope public rows

```bash
open-tennis-data query --release TAG --sql "
  SELECT count(*) AS out_of_scope_rows
  FROM all_matches
  WHERE year < 2020
     OR draw <> 'main'
     OR format <> 'singles'
"
```

### Combined projection mismatch

```bash
open-tennis-data query --release TAG --sql "
  SELECT count(*) AS mismatched_rows
  FROM (
    (SELECT * FROM all_matches
     EXCEPT ALL
     (SELECT * FROM matches UNION ALL SELECT * FROM fixtures))
    UNION ALL
    ((SELECT * FROM matches UNION ALL SELECT * FROM fixtures)
     EXCEPT ALL
     SELECT * FROM all_matches)
  )
"
```

### Terminal rows missing exact-date provenance

```bash
open-tennis-data query --release TAG --sql "
  SELECT count(*) AS missing_date_evidence
  FROM matches m
  WHERE NOT EXISTS (
    SELECT 1
    FROM provenance p
    WHERE p.match_id = m.match_id
      AND p.tour = m.tour
      AND p.year = m.year
      AND p.observation_kind = 'match_date'
      AND p.played_on = m.date
      AND p.date_role = 'played'
      AND p.date_precision = 'day'
  )
"
```

## Coverage, provenance, and health

Coverage rows that prevent stable publication:

```bash
open-tennis-data query --release TAG --sql "
  SELECT *
  FROM coverage
  WHERE coverage_status <> 'complete'
     OR missing_tournament_rows > 0
     OR missing_match_rows > 0
     OR missing_date_rows > 0
     OR source_conflicts > 0
  ORDER BY year, tour, level, lifecycle
"
```

Release freshness:

```bash
open-tennis-data query --release TAG --sql "
  SELECT *, date_diff('hour', as_of, current_timestamp) AS age_hours
  FROM health
  ORDER BY tour
"
```

Rows lacking retrieval timestamps:

```bash
open-tennis-data query --release TAG --sql "
  SELECT tour, year, observation_kind, count(*) AS observations
  FROM provenance
  WHERE retrieved_at IS NULL
  GROUP BY tour, year, observation_kind
  ORDER BY year, tour, observation_kind
"
```

Source policies and attribution:

```bash
open-tennis-data query --release TAG --sql "
  SELECT DISTINCT policy_source, policy_state, terms_url,
         allowed_uses, attribution, reviewed_at, policy_revision
  FROM sources
  ORDER BY policy_source
"
```

Quarantine reasons:

```bash
open-tennis-data query --release TAG --sql "
  SELECT tour, year, reason, count(*) AS rows
  FROM quarantine
  GROUP BY tour, year, reason
  ORDER BY year DESC, tour, rows DESC, reason
"
```

## Query downloaded Parquet directly

DuckDB can query a local file:

```bash
duckdb -c "
  SELECT tour, year, count(*) AS matches
  FROM read_parquet('matches.parquet')
  GROUP BY tour, year
  ORDER BY year, tour
"
```

Or query a release asset through HTTP range reads:

```bash
duckdb -c "
  SELECT date, tournament_name, player1_name, player2_name, score
  FROM read_parquet(
    'https://github.com/ryantjx/tennis-match-data/releases/download/TAG/completed.parquet'
  )
  WHERE tour = 'atp' AND year = 2025
  ORDER BY date DESC
  LIMIT 25
"
```

Create a smaller Parquet extract:

```bash
open-tennis-data extract \
  --release TAG \
  --tour wta \
  --years 2024:2025 \
  --levels grand_slam,wta_1000 \
  --output wta-major-events.parquet
```

