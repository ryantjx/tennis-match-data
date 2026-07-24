# Open Tennis Data v3 backend

Status: implemented as a fail-closed preview.

Last reviewed: 2026-07-24.

## Mission

Open Tennis Data v3 is a backend-only, self-updating research dataset for
ATP/WTA top-level main-draw singles from 2020 onward. It distributes
deterministic Parquet assets through GitHub Releases and supports local or
remote DuckDB queries through one small CLI.

The repository does not provide a website or hosted query API. Software is MIT
licensed; data retains its source terms and is not represented as commercially
reusable.

## Public scope

Included:

- ATP and WTA top-level main-draw singles from 2020 onward;
- Grand Slams, tour finals, Olympics, and team-event singles;
- terminal matches with accepted match-level day evidence; and
- future fixtures, including draw slots whose date or participants are not yet
  published.

Excluded:

- qualifying, Challenger, WTA 125, ITF/Futures, and doubles;
- rankings and match statistics; and
- any result whose only date evidence is a tournament week/start date.

The physical match schema is version 3.3. It appends `source VARCHAR[]` to the
existing column order so every public match and fixture carries direct,
deterministic attribution. V3 remains the product generation.

## Date and lifecycle rules

For a terminal match, `date` is the source-backed local calendar day at the
venue. A release row must join to accepted provenance with
`observation_kind=match_date`, `date_role=played`, and `date_precision=day`.
Conflicting dates, missing venue timezones, malformed rows, ambiguous
identities, and tournament-only dates are quarantined.

Fixtures use `status=fixture`. Their `date`, player IDs, and player names may be
null until an accepted schedule observation supplies them; `winner_id` and
`score` are always null. A fixture transitioning to a terminal result retains
its `match_id` and leaves the fixture projection in the same release.

## Release contract

One release contains:

```text
matches.parquet
completed.parquet
fixtures.parquet
tournaments.parquet
players.parquet
provenance.parquet
sources.parquet
coverage.parquet
health.parquet
quarantine.parquet
catalog.parquet
manifest.json
SHA256SUMS
```

`matches.parquet` is the byte-deterministic logical union of completed matches
and fixtures. The manifest records product/schema versions, scope, status,
timestamp, row counts, sizes, checksums, and stable asset URLs.

The publisher creates a timestamped draft, uploads every asset, redownloads
them, verifies byte equality, checks schemas and checksums, and only then
publishes. Preview builds become GitHub prereleases and cannot replace
`/releases/latest/`. A stable build must also pass the closed-event coverage
and 30-hour freshness gates.

## Collection and policy

The versioned registry is
[`src/open_tennis_data/sources.json`](src/open_tennis_data/sources.json). A source
without an entry, an allowed research-release use, attribution, or suitable
terms fails the release.

- Tennis-Data.co.uk season files provide historical match-day candidates.
  ATP files before 2003 are rejected because their date semantics are
  tournament-level.
- Wikimedia revisions provide draw identity, fixture slots, and explicit
  match/schedule dates where present.
- Sackmann/Tennis Abstract remains research-only identity/result
  cross-checking and enrichment. `tourney_date` is never date evidence.
- WTA and Tennis TV parser fixtures remain tested, but automated collection
  and publication are policy-blocked unless separate written permission is
  recorded. Their public terms prohibit automated harvesting/scraping.
- Community corrections are CC0 and require a public supporting URL.

Source rows are normalized, content-hashed, reconciled conservatively, and
linked through lifecycle-stable identities. Release provenance exposes native
source IDs, hashes, date role/precision, participants, round, score,
parser/policy versions, and reconciliation method.

## Automation

- Daily at 04:17 UTC: build a fresh staged dataset from 2020 through the
  current season, validate it, build deterministic release assets, and run the
  atomic publisher.
- Weekly: rebuild/audit every season from 2020 through the current season,
  retain reports as workflow artifacts, and never commit generated Parquet or
  open a data pull request.

Generated release Parquet belongs in GitHub Releases, not new Git history.
The old `data-latest` and `future-latest` assets are frozen and their date
semantics are superseded. Generated data has been removed from the current
tree; history and old tags are preserved, so shallow clones are recommended.

## Preview exit gate

V3 remains preview until all of the following are true:

- every closed in-scope event from 2020 through the previous season exists in
  an expected tournament/draw inventory;
- every expected main-draw slot is accepted or explicitly quarantined;
- missing tournaments, matches, dates, duplicates, conflicts, and unmatched
  observations are zero or explicitly resolved;
- every observation records its true retrieval timestamp;
- the current season is incomplete only for identified ongoing events or
  unpublished fixtures;
- identical pinned observations produce byte-identical assets;
- the latest release is no more than 30 hours old; and
- the first stable release passes redownload verification.

The current implementation intentionally fails `open-tennis-data
verify-release --require-complete` until the complete closed-event inventory
and historical retrieval timestamps are available. This prevents an
incomplete preview from silently becoming the stable `latest` release.
