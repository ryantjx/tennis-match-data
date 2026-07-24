# Objective: exact-date, daily open tennis data

Status: approved direction for v4. An interim v3.2 exact-date remediation
exists, but the v4 source-policy, licence tiers, collection architecture, and
atomic release contract are not implemented.

Last reviewed: 2026-07-24.

## Mission

Build a self-updating tennis match repository that uses no paid API, publishes
historical results and future fixtures every day, and never presents a
tournament date as the date on which an individual match was played.

The v4 build will cover the repository's existing men's and women's singles
scope from 1968 onward: ATP and WTA tour events, Grand Slams, qualifying,
team-event singles, ATP Challenger, WTA 125, ITF, and Futures where an approved
source supplies usable evidence.

One atomic daily run will produce:

- exact-dated completed-match assets;
- future-fixture assets, including undated draw slots;
- combined completed-and-future assets; and
- the provenance, source-policy, coverage, and health data needed to audit
  every published row.

## Why v4 is necessary

The pre-remediation v3.2 automation was operational, but it automated an
inadequate date contract. The interim remediation corrects the completed-date
fallback without completing the broader v4 objective.

- The primary Sackmann/Tennis Abstract inputs describe `tourney_date`, which
  is normally the tournament week or start date rather than the day of an
  individual match.
- The source-built canonical data reviewed before the v3.2 migration had no
  populated match-level `played_on` values.
- Pre-remediation v3.2 made terminal `date` non-null by using the exact match
  day when available and otherwise falling back to the tournament start date.
  Those approximately 1.74 million completed rows must not be described as
  uniformly exact-dated.
- The interim remediation leaves unresolved canonical dates null and publishes
  only its exact-dated subset. Its Tennis-Data.co.uk, WTA API, and Tennis TV
  evidence remains research-tier or internal-evaluation input until the v4
  policy registry records suitable permission.
- The current Wikimedia fixture collector discovers draw slots, but ordinarily
  has no published match day to assign. Null future dates are therefore
  expected.
- The repository now has interim exact-date parsers and observations, but no
  approved official order-of-play adapter or complete v4 policy-enforced
  observation pipeline.

Passing validation proves that files satisfy the remediated v3.2 contract. It
does not prove v4 source approval, open-tier compatibility, daily historical
and future atomic publication, or the v4 coverage gates.

## Goals

- Publish only terminal matches whose local calendar day is backed by approved
  match-level evidence.
- Build both historical and future data in the same staged, atomic daily run.
- Publish completed-only, future-only, and combined lifecycle views using one
  physical match schema.
- Update and publish by 06:00 UTC each day.
- Commit validated scheduled data-refresh outputs directly to `main`; routine
  workflow runs must not open pull requests for generated data changes.
- Preserve every accepted source observation, schedule revision, retrieval
  time, content hash, licence, and reconciliation decision.
- Retain unresolved evidence outside completed-match tables so later sources
  can promote it without refetching or inventing facts.
- Keep canonical match identities stable as schedules, participants, dates,
  scores, and statuses change.
- Make source coverage, data freshness, licence compatibility, ambiguity, and
  unresolved gaps visible to consumers.
- Offer a commercially reusable tier separately from a research tier that may
  contain non-commercial sources.

## Non-goals

- Do not promise that every historical professional match can be exact-dated
  from freely reusable sources.
- Do not infer a match day from a tournament start, tournament window, round
  order, draw position, article revision, publication time, or typical
  scheduling convention.
- Do not promise that a future schedule will remain unchanged after the
  release `as_of` timestamp.
- Do not fabricate a clock time for `Not Before`, `After`, or `Followed By`
  scheduling language.
- Do not scrape or redistribute a source merely because it is publicly
  viewable or has a free account or sandbox.
- Do not use or require a paid API, bookmaker feed, commercial livescore
  service, or unofficial odds site in v4.
- Do not add doubles until the exact-date singles pipeline meets its coverage
  and freshness gates. The schema may remain doubles-capable.

## Public match contract

Completed matches and fixtures retain the shared 19-column v3.2 physical
shape:

```text
date, match_id, tournament_id, tournament_name, tour, year, draw, round,
format, player1_id, player1_name, player1_seed,
player2_id, player2_name, player2_seed,
winner_id, status, score, best_of
```

The Arrow types and deterministic Parquet layout remain shared across
lifecycles. Status supplies the lifecycle semantics.

### Completed rows

- `status` must be terminal.
- `date` is required and means the verified local calendar day on which the
  match was played.
- The row must resolve to at least one accepted exact-date observation with
  day precision.
- All accepted exact-date observations for the match must agree. A conflict is
  quarantined rather than resolved by guessing or majority vote.
- Participants and result fields continue to follow the terminal status
  matrix.
- A completed row cannot also appear in the future release.

An unresolved historical result is not a v4 canonical or public completed
match. Its source observation and possible identity links remain in the
evidence backlog until exact evidence becomes available.

### Future rows

- `status` must be `fixture`.
- `winner_id` and `score` must be null.
- Participant slots and `date` may be null when a draw is known before the
  source publishes the players or order of play.
- A non-null `date` means the latest accepted scheduled local calendar day
  known at the release `as_of` timestamp.
- A non-null date must resolve to an accepted schedule observation. It cannot
  come from a tournament range or round inference.
- Revisions, postponements, cancellations, and moves append observations
  without changing `match_id` or erasing what an earlier release knew.

### Combined rows

The combined asset is a deterministic `UNION ALL` of the completed and future
assets for the same tier and `as_of` timestamp. Consumers distinguish the
lifecycle through `status`; no second record-type column is added.

## Release families

Every successful daily build publishes all three release families or none:

1. **Completed:** terminal, exact-dated matches only.
2. **Future:** fixtures, including legitimately undated draw slots.
3. **Combined:** the direct union of completed and future rows.

Each family provides ATP, WTA, and all-tour match assets plus the referenced
tournaments, provenance, source records, and a manifest. All manifests from
one build share the same UTC `as_of` timestamp and include:

- schema and collector versions;
- licence tier;
- asset paths, byte sizes, row counts, and SHA-256 checksums;
- source-policy revision;
- exact, scheduled, undated, unresolved, ambiguous, conflicted, and
  quarantined counts;
- source freshness and last successful observation time; and
- the build and source revisions needed for deterministic replay.

The build starts early enough to publish by 06:00 UTC. Health becomes stale and
alerts fire when the last successful build, or the last successful observation
check for an active approved source, is older than 30 hours.

## Licence tiers

Licences are enforced at observation and release time rather than summarized
only in prose.

### Open tier

The open tier contains only observations whose terms permit the intended
public redistribution and commercial reuse. Each asset's manifest lists all
applicable attribution and share-alike obligations.

### Research tier

The research tier may additionally include sources such as Sackmann/Tennis
Abstract under CC BY-NC-SA 4.0. It is clearly labelled non-commercial and
ShareAlike. It must never be presented as commercially reusable merely because
the repository's software is MIT licensed.

An open-tier row may also appear in the research tier. A research-only
observation must never leak into an open-tier asset, identifier lookup, or
derived fact without compatible evidence.

## Source policy

Every adapter has a versioned policy entry:

```text
source, legal_entity, policy_state, terms_url, reviewed_at,
allowed_fields, allowed_uses, raw_retention, redistribution,
attribution, rate_limit, authentication, expiry, policy_revision
```

The allowed `policy_state` values are:

- `approved`: may contribute to canonical and public data within its recorded
  licence tier;
- `metadata_only`: may provide discovery links and coverage metadata, but not
  copied match facts;
- `internal_evaluation`: may be measured locally, but derived rows cannot be
  committed or released; and
- `disabled`: cannot be called by automated workflows.

Release validation fails if an observation lacks a current policy entry, uses
an incompatible allowed-use class, omits required attribution, or comes from a
source whose approval has expired.

### Initial source roles

| Source | Initial role |
| --- | --- |
| [Wikimedia](https://www.wikimedia.org/) and Wikidata | Approved where the applicable Wikimedia licence covers the material. Use for tournament/draw identity and explicit match-level or schedule statements only. |
| [Sackmann / Tennis Abstract](https://github.com/JeffSackmann/tennis_atp) | Research-tier identity, result, ranking, and statistics evidence. Never treat `tourney_date` as the match day. |
| [tennis-data.co.uk](https://www.tennis-data.co.uk/data.php) | Permission-gated historical exact-date candidate. Keep disabled for public builds until normalized redistribution rights are confirmed. Its end-of-tournament update cycle does not satisfy the daily current-data objective. |
| [ATP](https://www.atptour.com/en/terms-and-conditions-app), [WTA](https://www.wtatennis.com/terms-and-conditions), ITF, and the four Grand Slams | Permission-gated official schedule/result candidates. Keep publishing adapters disabled until written no-fee automated-access and redistribution permission is recorded. |
| [WTA Data API](https://developers.wtatennis.com/) and [ATP/TDI platform](https://www.tennisdata.com/tennis-data-platform) | A free login, sandbox, or visible endpoint is not a redistribution grant. Enable only under an approved no-fee agreement. |
| Commercial livescore, bookmaker, paid API, and odds sites | Disabled and outside the v4 objective. Supporting one would require a separately approved objective change as well as written automated-access and redistribution rights. |

`robots.txt` controls crawler behavior but is not a data licence. Terms,
permissions, rate limits, caching rules, and redistribution rights are all
checked independently.

## Collection architecture

```text
Approved bulk/API/document/HTML source
                  |
                  v
Content-addressed raw snapshots
                  |
                  v
Append-only source observations
                  |
                  v
Identity reconciliation and quarantine
                  |
                  v
Completed matches + future fixtures
                  |
                  v
Open/research completed, future, and combined releases
```

### Retrieval

- Prefer permissioned bulk files, exports, APIs, and official documents over
  page-by-page HTML crawling.
- Fetch HTML only when the source policy permits it and no better transport
  exists.
- Use conditional requests, conservative concurrency, retries with backoff,
  and source-specific rate limits.
- Store raw responses by SHA-256 outside Git when redistribution or repository
  size makes committing them inappropriate.
- Never make canonical data depend on an uncataloged mutable page.

### Observations

Result-date and schedule observations retain at least:

```text
observation_id, source, source_record_id, source_url, observed_at,
source_timezone, venue_timezone, date_role, date_precision,
played_on, scheduled_on, scheduled_at, schedule_status,
tour, tournament_raw, round_raw, participants_raw, score_raw,
content_sha256, row_fingerprint, parser_version, policy_revision
```

`date_role` distinguishes an actual played date from a scheduled assertion.
`date_precision` prevents a tournament range or timestamp of publication from
becoming day-level match evidence.

### Reconciliation

Resolve observations in this order:

1. an existing mapping for the same source-native match ID;
2. an approved official crosswalk;
3. exactly one candidate using tour, tournament aliases, unordered
   participants, round, normalized score, and a date window that permits only
   a documented timezone boundary.

Zero candidates, multiple candidates, malformed input, conflicting dates, and
unapproved observations go to quarantine. Player names alone never merge
identities. Rematches at one tournament require round or stronger source
identity evidence.

### Daily collection window

Each run revisits:

- the previous seven days for late results, suspensions, and corrections;
- today for newly terminal matches and schedule changes; and
- the next fourteen days for draws, orders of play, postponements, and
  participant assignments.

Historical bulk and document backfills use the same observation and
reconciliation path as the daily pipeline. A second run over identical
snapshots must make no semantic or byte-level release changes.

## Transition from v3.2

The pre-remediation v3.2 event-date assets are frozen under an immutable legacy
label. Their documentation states that completed `date` may be the tournament
start date, so they are not represented as exact-date data.

The interim v3.2 remediation is identified separately. It removes the fallback,
keeps unresolved canonical dates null, and limits completed downloads to its
exact-dated subset, but remains a research-tier bridge rather than v4. It does
not satisfy the v4 policy registry, licence tiers, atomic output families,
daily SLA, or coverage gates.

v4 launches under separate release channels. The exact completed release may
initially contain few or no rows until approved match-date observations are
ingested. Coverage is never inflated with fallback dates to make the launch
look complete.

The legacy default is retired only after both gates pass:

- at least 95% exact-date coverage for ATP and WTA main-tour singles from 2000
  onward; and
- at least 90% exact-date coverage for the broader existing singles scope in
  every year that v4 claims as covered.

Coverage denominators, exclusions, source permissions, and unsupported years
are published so the gates are reproducible.

## Implementation milestones

### 0. Objective and repository hygiene

- Adopt this document as the authoritative direction.
- Remove superseded planning notes and duplicate local artifacts.
- Preserve the pre-remediation v3.2 event-date assets under an accurate legacy
  label and distinguish the interim exact-date remediation from v4.

### 1. Evidence contract

- Add the source-policy registry, exact-date observations, schedule
  observations, UTC `as_of` manifests, and licence-tier validation.
- Remove tournament-start fallback from the v4 projection.
- Separate unresolved historical evidence from completed matches.

### 2. Source permissions and pilots

- Confirm tennis-data.co.uk redistribution rights.
- Request no-fee automated-access and redistribution permission from ATP,
  WTA, ITF, TDI, and each Grand Slam.
- Pilot representative months from 2010, 2015, 2020, 2024, and 2026 before a
  full backfill.

### 3. Historical exact-date backfill

- Ingest approved bulk sources first.
- Add approved official orders of play, result documents, and match-level
  Wikimedia/Wikidata evidence.
- Publish exact-date coverage, reconciliation, conflict, and permission gaps
  by tour, level, draw, and year.

### 4. Atomic daily publisher

- Build completed, future, and combined assets for both licence tiers.
- Validate all lifecycle, evidence, licence, coverage, checksum, freshness,
  and deterministic-output rules before an atomic release.
- Let the scheduled workflow commit validated generated data and release
  metadata directly to `main` without opening a pull request. This exception
  does not bypass review for collector code, schema, source-policy, or
  hand-authored documentation changes.
- Publish by 06:00 UTC and alert on the 30-hour freshness threshold.

### 5. Default cutover

- Freeze and link the final v3.2 legacy snapshot.
- Demonstrate both coverage gates from published manifests.
- Promote v4 stable channels without removing the immutable legacy download.

## Acceptance criteria

The v4 objective is achieved when:

- every terminal row has a non-null exact date and accepted day-level
  match evidence;
- no completed date is supplied only by a tournament date or inference;
- every non-null fixture date resolves to an accepted schedule observation;
- undated fixtures remain valid and visibly counted;
- completed, future, and combined assets share one schema and one atomic
  `as_of` timestamp;
- combined assets equal the deterministic union of their lifecycle assets;
- open and research tiers contain no licence-incompatible observations;
- unresolved and conflicting historical evidence remains auditable outside
  completed-match tables;
- all source rows reconcile to accepted plus quarantined outcomes;
- match identities survive fixture changes and fixture-to-result conversion;
- routine scheduled data refreshes commit atomically to `main` without opening
  pull requests;
- daily publication meets the 06:00 UTC target and 30-hour freshness alert;
- identical snapshots reproduce byte-identical releases; and
- the published coverage manifests prove both legacy-retirement gates.

Until those conditions are implemented and measured, this document describes
the target. Current v3.2 behavior remains governed by `docs/SCHEMA.md` and must
not be represented as v4.
