# IMPROVEMENT v3.1: repository integrity and release-contract hardening

- Audit date: 2026-07-24
- Review incorporated: 2026-07-26
- Repository baseline: `1316c570`
- Published baseline: `data-v3-20260724T164801Z`
- Baseline manifest SHA-256:
  `d7258adf49d0ce156c72353b9b6b5d7227cc3d955f0c9e7ae082aaad1d566c6f`
- Authoritative target: [`OBJECTIVE.md`](OBJECTIVE.md)
- Match-shaped schema: v3.3
- Plan status: source-neutral integrity plan

## Executive outcome

The current repository has strong provenance and release foundations, but it
cannot yet prove that every published row is temporally valid, semantically
unique, lifecycle-correct, reproducible from a closed evidence set, and tied to
the exact release that advertises it.

This plan fixes those repository-level guarantees before any new acquisition
adapter is allowed to populate public fields. It deliberately does not select
data providers, define scraping routes, or change collection cadence. Those
decisions are owned by [`IMPROVEMENT_v3_2.md`](IMPROVEMENT_v3_2.md).

The target invariant is:

> Every public material field is projected from an eligible immutable
> observation available at one pinned `source_cutoff_at`; every observation,
> identity decision, revision, conflict, and quarantine outcome remains
> reproducible from the release and its verified parent.

The implementation order is:

1. contain known unsafe publication paths;
2. formalize time, lifecycle, identity, and evidence contracts;
3. introduce closed source bundles and parent-seeded append-only ledgers;
4. migrate baseline observations without invented timestamps;
5. activate semantic deduplication and fixture-to-result continuity;
6. compute stable-release gates from independent inventories; and
7. harden workflows, packaging, monitoring, and the explorer.

## 1. Scope and boundary

This plan covers:

- ATP/WTA top-level main-draw singles from 2020 onward;
- Grand Slams, tour finals, Olympics, and included team-event singles;
- the existing v3.3 20-column match-shaped schema;
- one atomic deterministic GitHub Release;
- a read-only Pages explorer;
- terminal and fixture date semantics;
- stable event, draw, slot, player, and match identity;
- source-neutral observations, artifacts, policy evaluations, and quarantine;
- deterministic migration, validation, and publication; and
- daily and weekly workflow correctness independent of provider choice.

This plan does not:

- select or rank data providers;
- implement provider-specific HTTP, browser, API, PDF, or spreadsheet adapters;
- define provider polling frequency or permission grants;
- expand to qualifying, Challenger, WTA 125, ITF/Futures, doubles, rankings, or
  point-by-point statistics;
- revive the retired v4 proposal;
- commit generated public release data to Git history; or
- downgrade the public schema from v3.3.

Provider migration, Wikipedia discovery, official-site scraping, historical
annual-file refresh, permissions, source canaries, and source-specific tests
belong exclusively to v3.2.

## 2. Current implementation assessment

### Strengths

- Match-shaped releases already separate completed rows, fixtures, tournaments,
  players, provenance, coverage, health, quarantine, and catalog assets.
- Release manifests and checksums support deterministic verification.
- Source policy is versioned and unknown or blocked labels fail closed.
- Exact date reconciliation already rejects ambiguous candidate matches.
- Fixtures support null dates rather than inventing tournament-window dates.
- The public explorer is read-only and queries release artifacts locally.
- Offline parser fixtures and workflow tests provide a useful regression base.

### Integrity gaps

The audited baseline exposes repository-level risks independent of the selected
provider:

- a terminal observation can carry a date years after its source season and
  release cutoff;
- participant uniqueness can reconcile a row without sufficient event, round,
  score, draw-slot, or date-window evidence;
- walkovers and pre-play defaults can be labelled as played;
- a dated fixture does not require exact schedule evidence;
- a still-active fixture can disappear merely because its scheduled day passed;
- clean daily rebuilds do not prove fixture-to-result `match_id` continuity;
- a clean rebuild can lose an older mutable-page revision;
- event aliases and draw topology can create completed, fixture, or
  cross-lifecycle duplicates;
- unknown draw slots can disappear from coverage accounting;
- source field permissions are recorded but are not enforced at every public
  projection;
- the publisher can be asked to create a tag that disagrees with its manifest;
- acquisition, cutoff, build, generation, and publication clocks are conflated;
- baseline observations lack truthful retrieval timestamps and accepted venue
  timezone evidence required by the objective; and
- the stable completeness label is not yet an independently recomputed proof.

The checked-in `data/` tree is not a valid release-shaped dataset: required
paths such as `catalog/catalog.parquet` are absent. Validation must use a
verified immutable release or an intentionally generated offline fixture.

## 3. Public semantics

### 3.1 Time model

Use distinct timestamps:

- `collection_started_at`: the first retrieval attempt in a collection run;
- `retrieved_at`: the actual instant an artifact or observation was obtained;
- `collection_closed_at`: the instant the immutable input bundle was closed;
- `source_cutoff_at`: equal to the closed bundle cutoff used for eligibility;
- `build_started_at`: the start of deterministic transformation;
- `generated_at`: asset-generation telemetry; and
- `published_at`: GitHub publication telemetry.

No build may resolve a moving `latest` reference after collection begins.
Every non-genesis build pins one parent tag and parent manifest hash.

Eligibility requires:

```text
retrieved_at <= source_cutoff_at
publisher_modified_at <= source_cutoff_at, when supplied
```

`published_at` is never evidence that a source fact was available earlier.

### 3.2 Date roles

- A played terminal row uses `date_role=played` and `date` equal to the
  venue-local day on which play first started.
- A retirement, abandonment, or post-start default retains the first-started
  day.
- A walkover, cancellation, or pre-play default has no played day and remains
  quarantined until the public contract adds a distinct decided-date role.
- An active fixture date is the current accepted publisher-announced
  venue-local scheduled day as of `source_cutoff_at`.
- An undated fixture is valid and explicit when exact eligible schedule
  evidence does not exist.
- Passing a scheduled day does not change lifecycle. The fixture remains
  active and overdue until an eligible terminal, cancellation, postponement, or
  date-withdrawal observation arrives.

### 3.3 Date acceptance

Every accepted material date must satisfy:

- declared source season;
- conservative global cutoff;
- event-edition window;
- accepted IANA venue timezone;
- eligible observation and policy evaluation;
- exact match-level identity evidence; and
- no unresolved independent-evidence conflict.

Participant uniqueness alone is insufficient. Reconciliation must include an
event or draw identity plus at least one of round, score, stable source match
identifier, or draw position.

### 3.4 Identity

Canonical identity is assigned independently from mutable evidence:

- `event_id`: stable event series;
- `event_edition_id`: event plus year/edition;
- `draw_id`: edition, tour, discipline, and draw type;
- `fixture_key`: canonical draw path and slot identity;
- `player_id`: canonical player with versioned source aliases; and
- `match_id`: stable lifecycle identity derived from canonical edition, draw,
  slot, and participant state.

Equivalent provider URLs, template locations, or native IDs are aliases, not
new matches. A fixture becoming terminal retains its `match_id`.

## 4. Supporting contracts

Keep the v3.3 match-shaped schema and introduce versioned supporting assets
before enabling new hard gates.

### 4.1 Evidence artifacts

`evidence_artifacts.parquet` records immutable retrieval facts:

```text
evidence_artifact_id
source_label
source_url
publisher_revision
retrieved_at
publisher_modified_at
media_type
byte_length
sha256
storage_mode
retention_basis
```

`evidence_artifact_evaluations.parquet` appends policy decisions without
rewriting artifact history:

```text
evaluation_id
evidence_artifact_id
policy_revision
evaluated_at
eligible_for_public_projection
allowed_fields
allowed_roles
reason
supersedes_evaluation_id
```

### 4.2 Venue evidence

`venue_evidence.parquet` records the accepted venue and timezone assertion:

```text
venue_evidence_id
event_edition_id
venue_name
city
country
iana_timezone
valid_from
valid_to
source_url
evidence_artifact_id
review_status
```

Multi-venue events and team ties use match/tie/session-scoped evidence rather
than a tournament-wide assumption.

### 4.3 Schedule observations

`schedule_observations.parquet` records what an eligible artifact asserted:

```text
observation_id
source_label
source_event_id
source_fixture_id
asserted_local_date
asserted_local_time
court
status
source_timezone
source_venue_text
evidence_artifact_id
observed_at
parser_version
policy_revision
```

`schedule_observation_links.parquet` resolves an immutable observation to
canonical identity:

```text
link_revision_id
observation_id
fixture_key
match_id
resolution_method
confidence_basis
linked_at
supersedes_link_revision_id
review_status
```

Reconciliation corrections append link revisions. They never mutate source
observations.

### 4.4 Migration dispositions

The first replacement release includes
`migration_dispositions.parquet`, mapping every baseline terminal match and
released provenance row to exactly one outcome:

```text
baseline_identity
disposition
replacement_observation_id
reason
reviewer
reviewed_at
```

Allowed dispositions are `retained`, `replaced`, `quarantined`, and `excluded`.
The asset is content-hashed in the manifest.

## 5. Policy and projection rules

The policy registry is the sole authority for:

- accepted source labels;
- material fields and semantic roles;
- policy and terms revisions;
- upstream lineage;
- raw-artifact retention and redistribution;
- public versus private ledger admission;
- permission expiry and revocation; and
- whether a transformation may affect a public projection.

Every source-backed public material field must join:

```text
public row
  -> field provenance
  -> immutable observation
  -> eligible current policy evaluation
  -> evidence artifact
```

Every derived material field records derivation name, input hashes, code
commit, parser version, and schema version.

Community corrections become observations with evidence and policy evaluation;
they do not directly mutate terminal or fixture dates.

Independent current observations that assert different material values produce
a conflict and a null/quarantined projection. Global “newest wins” and
unqualified source-priority voting are prohibited.

## 6. Delivery plan

### Phase 0 — Containment

1. Amend the objective and schema documentation for date roles, lifecycle,
   cutoffs, venue timezones, parent seeding, and supporting assets.
2. Add source-year, conservative cutoff, event-window, and minimum-evidence
   guards.
3. Add regressions for the audited far-future date and weak participant-only
   reconciliation.
4. Reject non-null fixture dates until exact schedule provenance is enforced.
5. Reject direct date mutation by corrections.
6. Quarantine no-play terminal outcomes under the current played-date contract.
7. Keep past-scheduled active fixtures in the fixture projection.
8. Reject tag/manifest mismatch.
9. Pause scheduled public releases, while allowing private validation artifacts,
   until migration and hard gates pass.

Exit criteria:

- no future/out-of-season terminal date can publish;
- no participant-only reconciliation can publish;
- no no-play outcome is labelled played;
- no active fixture disappears solely because time passed;
- no unsupported dated fixture publishes; and
- no manifest/tag mismatch path remains.

### Phase 1 — Identity, policy, and closed bundles

1. Add versioned event aliases, canonical draw paths, unknown slots, and stable
   fixture keys.
2. Pin and verify the parent release once per build.
3. Implement the collection/source-bundle/build clock contract.
4. Make field- and role-level policy authoritative.
5. Convert corrections into observations.
6. Add source-backed and derived-field lineage matrices.

Exit criteria:

- two clean builds retain fixture-to-result identity;
- template ordering and participant correction do not change stable IDs;
- every material field has eligible source or derivation lineage; and
- identical logical inputs create byte-identical assets.

### Phase 2 — Truthful migration

1. Build the venue and IANA timezone evidence inventory.
2. Re-fetch every legally and technically retrievable baseline input into a
   closed bundle with its real retrieval time.
3. Create new immutable observations; never synthesize historical retrieval
   times.
4. Produce and review complete migration dispositions.
5. Run cutoff, retrieval-time, timezone, policy, and retention gates in shadow.
6. Require at least 99.9% baseline terminal-match retention—at least 26,285 of
   26,311 rows—and no whole closed edition or season lost. A larger reduction
   requires an explicit owner decision and release notes; an empty projection
   is always blocked.

Exit criteria:

- every retained public observation has truthful retrieval and artifact
  evidence;
- every material date has accepted venue-timezone evidence;
- every baseline item has one reviewed disposition; and
- publication remains paused with a valid replacement candidate.

### Phase 3 — Durable lifecycle projection

1. Add supporting assets and manifest entries with explicit per-asset genesis
   markers.
2. Seed all later ledgers from the verified parent release.
3. Implement same-publisher revision chains and independent-evidence conflict
   projection.
4. Require accepted observation and artifact joins for every dated fixture.
5. Preserve postponements, retractions, withdrawn dates, and overwritten
   revisions.
6. Deduplicate only after canonical identity exists.
7. Reconcile fixture-to-terminal transitions without changing `match_id`.
8. Activate hard gates and publish the first replacement preview only when all
   migration and validation criteria pass.

### Phase 4 — Computed completeness and freshness

1. Populate a reviewed event/draw inventory from 2020 onward.
2. Model variable draw formats, byes, round robin, unknown slots, and team ties.
3. Reconcile every expected slot to accepted, fixture, or quarantine state.
4. Compute distinct-key coverage metrics rather than counting observations.
5. Make the verifier independently recompute every stable condition.
6. Compare weekly builds with the pinned prior immutable release.
7. Add an external monitor for the inclusive 30-hour source-cutoff and
   publication-envelope freshness predicates.

### Phase 5 — Repository hardening

- pass corrections explicitly in every scheduled build;
- minimize write-token exposure and separate validation from publication;
- add action and shell validation plus publisher failure injection;
- make browser tests use a local verified release fixture;
- ratchet Python coverage;
- build and install wheel and sdist artifacts;
- include code commit and dependency-lock identity in manifests;
- split the dataset monolith behind unchanged interfaces;
- bundle browser runtime dependencies deterministically; and
- update schema, contribution, citation, security, and operations docs.

Completion of Phase 5 leaves a source-neutral ingestion interface ready for the
v3.2 adapters. It does not enable any provider by itself.

## 7. Pull-request sequence

1. `contract-clarifications`: date roles, lifecycle, cutoffs, deterministic
   assets, supporting schemas, parent seeding, and freshness.
2. `emergency-integrity-guards`: future/out-of-season dates, reconciliation
   evidence, no-play quarantine, direct-date corrections, overdue fixtures,
   and publication pause.
3. `publisher-tag-integrity`: manifest/tag equality, verified parents, and
   failure-injection tests.
4. `canonical-event-draw-identity`: aliases, canonical draw paths, unknown
   slots, stable fixture keys, and parent identity seeds.
5. `policy-and-source-bundle-foundation`: authoritative field/role policy,
   immutable bundles, correction observations, and lineage.
6. `snapshot-timezone-migration`: truthful retrieval times, venue evidence,
   migration dispositions, retention decisions, and shadow gates.
7. `schedule-ledger-contract`: artifacts, evaluations, observations, links,
   chain validation, conflict projection, and fixture-date provenance.
8. `semantic-dedupe-lifecycle-gates`: deduplication, lifecycle transitions,
   overdue behavior, hard-gate activation, and replacement preview.
9. `computed-completeness-and-operations`: event/draw inventory, stable metrics,
   freshness monitoring, packaging, browser hermeticity, and documentation.

Each PR must leave releases fail-closed and must not combine generated dataset
artifacts with implementation changes.

## 8. Required tests

### Date integrity

- date after source season;
- date after conservative cutoff;
- exact venue-local cutoff boundary;
- event-window violation;
- missing or invalid IANA timezone;
- participant-only reconciliation;
- ambiguous round/score/draw identity;
- independent-evidence date conflict; and
- correction attempting direct date mutation.

### Terminal semantics

- completed match;
- retirement after play starts;
- abandonment and resumed play;
- post-start default;
- pre-play default;
- walkover; and
- cancellation.

### Fixture lifecycle

- dated and undated active fixtures;
- a past-dated active fixture remains public;
- postponed and rescheduled fixture;
- date withdrawal becomes an undated fixture;
- fixture becomes terminal across two clean builds;
- source-native identifier changes without changing `match_id`; and
- completed/fixture cross-status duplicate.

### Identity and topology

- event alias collision;
- combined event with separate tour draws;
- byes and unknown participants;
- round robin and team ties;
- template reordering;
- identical participants in different editions; and
- all-unknown slots remain in expected-slot accounting.

### Evidence and policy

- immutable artifact hash and retrieval time;
- artifact policy evaluation supersession;
- observation link revision without source mutation;
- unknown, blocked, expired, and changed policy;
- source allowed for participants but not dates;
- public versus private evidence eligibility;
- correction observation lineage; and
- derived-field lineage.

### Release and operations

- tag/manifest mismatch;
- stale or missing parent;
- moving `latest` is never resolved after collection starts;
- same closed bundle creates byte-identical output;
- missing mandatory supporting asset;
- malformed append-only chain;
- fresh publication wrapping a stale bundle;
- scheduled collection succeeds but validation blocks publication;
- external freshness monitor fires when the publisher never starts; and
- positive and negative stable-release fixtures.

## 9. Acceptance criteria and defaults

- The public match schema remains v3.3.
- Null fixture dates remain valid outside exact eligible schedule evidence.
- Every non-genesis build pins one exact parent tag and manifest hash.
- Every retained material date has accepted IANA venue-timezone evidence.
- Every public material source-backed field has an eligible observation,
  artifact, and current policy evaluation.
- No baseline timestamp is reconstructed or fabricated.
- Automatic migration retains at least 99.9% of baseline terminal matches.
- Freshness is inclusive at 30 hours for both source cutoff and publication
  envelope.
- Missing provider-specific evidence does not weaken a source-neutral stable
  gate.
- New acquisition adapters cannot bypass the contracts in this plan.
- All provider selection, scraping, historical refresh, permissions, and
  rollout work is implemented under `IMPROVEMENT_v3_2.md`.
