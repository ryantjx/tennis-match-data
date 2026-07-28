# IMPROVEMENT v3.2: Wikipedia-led acquisition with official ATP/WTA detail

- Plan date: 2026-07-26
- Repository base: v3.1 integrity contracts
- Public match-shaped schema: v3.3
- Public data scope: 2020 onward
- Historical source capability: 2003 onward
- Status: decision-complete implementation plan

## Executive outcome

Replace the current acquisition strategy with a field- and lifecycle-specific
source hierarchy:

1. **Wikipedia is the primary discovery and identity source.** It defines the
   event edition, aliases, draw topology, player links, and the set of active or
   recently completed tournaments to inspect.
2. **Official ATP and WTA websites provide current match detail.** A
   Playwright-based scraper captures rendered tournament pages and linked
   order-of-play artifacts for schedules, participants, player IDs, rounds,
   scores, statuses, and results.
3. **Tennis-Data is the historical completed-match baseline from 2003 onward.**
   Its annual ATP/WTA spreadsheets are refreshed weekly, validated, reconciled,
   and used for closed-event history. The public product remains scoped to 2020
   onward unless the objective is separately expanded.

ATP and WTA currently restrict systematic automated retrieval without written
permission:

- [ATP terms](https://www.atptour.com/en/terms-and-conditions-app)
- [WTA terms](https://www.wtatennis.com/terms-and-conditions)

The official-site scrapers are therefore implemented and tested offline but
remain network-disabled until a versioned policy record references a current
written permission grant covering automation, retention, and normalized public
redistribution. This is a fail-closed technical gate, not a documentation
warning.

This plan depends on the source-neutral identity, evidence, lifecycle, and
release contracts in [`IMPROVEMENT_v3_1.md`](IMPROVEMENT_v3_1.md). A v3.2
adapter cannot bypass a v3.1 invariant.

## 1. Current and proposed implementations

| Area | Current implementation | New implementation |
| --- | --- | --- |
| Historical matches | Jeff Sackmann repositories supply primary records; Tennis-Data mainly supplies dates | Tennis-Data supplies validated completed-match records; Wikipedia supplies identity and event context |
| Active tournaments | Wikimedia pages provide uneven fixtures/results; official APIs exist but are blocked | Wikipedia discovers events; permission-gated ATP/WTA website scrapers add schedules, results, draws, and player details |
| Daily build | Fresh rebuild of all 2020+ data | Incremental active-event refresh seeded from the verified prior release |
| Weekly build | Fresh bootstrap followed by an audit of the same state | Re-fetch every 2020–current ATP/WTA annual Tennis-Data file, hash-diff it, rebuild changed seasons, and compare with the prior release |
| Reproducibility | Mutable upstream pages can disappear between fresh builds | Retain permitted HTML/PDF snapshots, hashes, retrieval times, revision chains, and source-bundle manifests |
| Failure behavior | Some source gaps or weak reconciliation can affect projection | Missing, loading, conflicting, expired-permission, or structurally changed inputs are quarantined |

### 1.1 Current implementation strengths

- Wikimedia collection already uses the MediaWiki API, immutable revision IDs,
  rate limiting, and offline parser fixtures.
- The existing model separates completed matches from fixtures.
- Tennis-Data annual-file parsing already validates spreadsheet signatures,
  computes hashes, normalizes scores, and quarantines invalid rows.
- Live official-source parsers are already blocked by source policy.
- The release contains provenance, source, coverage, health, and quarantine
  assets.
- The daily and weekly workflow entry points provide clear orchestration
  boundaries that can be replaced without changing the public schema.

### 1.2 Current implementation weaknesses

- The Jeff Sackmann historical repositories are not reliably available and
  should not remain a build dependency.
- Tennis-Data is used as secondary date evidence rather than as a validated
  complete historical record.
- The current daily workflow rebuilds every year instead of updating active
  events from a pinned parent release.
- The weekly audit rebuilds current upstream state and immediately audits that
  same state; it does not prove longitudinal stability against the last
  immutable release.
- Wikipedia tournament coverage and update latency are uneven.
- Current fixture parsing does not guarantee exact schedule dates.
- The blocked WTA API and Tennis TV code paths do not implement the requested
  official website scraping model.
- Mutable pages and order-of-play PDFs are not captured as durable,
  permission-eligible revisions.
- Combined events can expose both tours on one official schedule and create
  tour misclassification or duplicate slots.
- Tennis-Data is served over unauthenticated HTTP, so a content digest proves
  stability after retrieval but does not authenticate the publisher.

### 1.3 New implementation benefits

- Current schedules and results come from the governing tour websites.
- Wikipedia remains an open, revisioned primary discovery and identity layer.
- Historical collection becomes a predictable weekly annual-file workflow.
- Daily jobs touch only active and recently completed event editions.
- Official player IDs improve identity matching.
- Exact schedule date, displayed time/order, court, score, and status can be
  represented without inference.
- Closed source bundles preserve permitted mutable revisions.
- Authority and conflict rules are explicit by field and lifecycle.

### 1.4 New implementation costs and risks

- Written ATP/WTA permission is a deployment dependency.
- Chromium installation increases CI time and maintenance.
- DOM selectors and asynchronous loading can change without notice.
- Order-of-play PDF URLs may be mutable and overwritten.
- PDF extraction may fail on image-only or layout-heavy documents.
- A combined event may show WTA matches on an ATP schedule or vice versa.
- Wikipedia may lag the official draw or omit an edition.
- Tennis-Data updates weekly and may lag a completed tournament, with Grand
  Slams documented as updating later.
- Tennis-Data raw transport is HTTP-only.
- Browser polling and artifact retention must remain within the written grant.

## 2. Source authority

“Primary source” means primary for an explicit field and lifecycle state. It
does not mean one source automatically wins every conflict.

| Field or function | Active event authority | Closed historical authority |
| --- | --- | --- |
| Event discovery and aliases | Wikipedia | Wikipedia |
| Event edition and draw topology | Wikipedia, corroborated by official site | Wikipedia plus retained official evidence |
| Canonical player identity | Wikipedia with official source aliases | Wikipedia with retained aliases |
| Source-native player ID | ATP/WTA site | Retained ATP/WTA observation |
| Scheduled date/time/court | ATP/WTA site or linked official OOP | Retained observation; never reconstructed |
| Participants and round | ATP/WTA site during event | Tennis-Data after weekly validation, with retained official corroboration |
| Match status and result | ATP/WTA site during event | Tennis-Data after weekly validation |
| Score | ATP/WTA site during event | Tennis-Data after weekly validation |
| Historical exact match day | Retained official terminal observation | Tennis-Data after validation |

Safety invariants override authority:

- a date cannot be accepted outside its declared season and event window;
- a terminal result cannot be accepted after `source_cutoff_at`;
- participant uniqueness alone cannot reconcile a match;
- a no-play outcome cannot acquire a played date;
- an official or historical source cannot overwrite a material conflict
  silently; and
- an expired or insufficient permission grant makes an official-site
  observation ineligible for public projection.

When eligible independent observations disagree, retain both, emit a conflict,
and withhold the disputed public field until reconciliation succeeds. Do not
implement “newest wins” or unconditional source priority.

## 3. Mubadala DC Open reference model

The 2026 Mubadala DC Open is the first combined-event acceptance fixture:

- canonical edition: `mubadala-dc-open:2026`;
- ATP source edition: `atp:418:2026`;
- WTA source edition: `wta:1045:2026`;
- ATP overview:
  `https://www.atptour.com/en/tournaments/washington/418/overview`;
- WTA overview:
  `https://www.wtatennis.com/tournaments/1045/washington-dc/2026`;
- shared venue edition with one accepted IANA timezone;
- separate ATP and WTA singles draws; and
- source aliases retained without becoming canonical IDs.

The ATP daily schedule may display WTA matches at this combined event. Each
scraped match must carry an explicit source-displayed tour marker. A WTA match
found on the ATP page:

- is classified as WTA;
- uses WTA participant identity resolution;
- may corroborate the WTA schedule observation;
- does not create an ATP fixture; and
- is deduplicated against the corresponding WTA page/PDF observation.

Deduplication keys use tour, canonical edition, draw, round, participants,
source-native IDs, and canonical draw position. Page URL alone is not match
identity.

## 4. Acquisition architecture

### 4.1 Separation of concerns

Each adapter has three independent stages:

1. **Fetch:** retrieve bytes or rendered HTML and record retrieval metadata.
2. **Parse:** transform an immutable local artifact into source-native
   observations without network access.
3. **Reconcile:** map observations to canonical identity and project only
   eligible, conflict-free fields.

Fetchers never write canonical match rows. Parsers never resolve moving URLs.
Reconciliation never mutates an immutable source observation.

### 4.2 Source labels

Use these canonical labels:

- `wikimedia`: Wikipedia page revisions and linked identity evidence;
- `atp-site`: rendered ATP tournament pages;
- `wta-site`: rendered WTA tournament pages;
- `atp-oop`: ATP/ProTennisLive order-of-play artifacts when explicitly granted;
- `wta-oop`: WTA order-of-play artifacts when explicitly granted; and
- `tennis-data.co.uk`: annual historical spreadsheets.

Retire the `sackmann` source after its rows have migration dispositions and
replacement identity aliases. It must not remain a runtime dependency or
public field authority.

### 4.3 Browser scraper

Use Python Playwright with Chromium for official-site collection. Keep scraping
dependencies in an installable `scrape` extra and pin them in the build:

- Playwright for navigation and rendered DOM;
- a dedicated HTML parser for offline snapshot parsing; and
- a PDF text/layout parser for order-of-play artifacts.

The workflow installs the pinned Chromium revision. Default unit and parser
tests use a local fixture server and make no official-site requests.

For each route:

1. verify permission policy before launching a network request;
2. navigate to the allowlisted URL;
3. wait for a route-specific stable content marker;
4. reject login, CAPTCHA, consent-blocked, error, or indefinite loading states;
5. capture rendered HTML plus response URL and retrieval time;
6. hash and close the artifact before parsing;
7. discover linked official PDFs without following an unapproved domain;
8. retrieve a PDF only when its exact domain and route are grant-allowlisted;
9. enforce request frequency and retry limits; and
10. append structured fetch success or rejection telemetry.

Do not use search snippets, social posts, ticket sessions, tournament windows,
or inferred round order as match-day evidence.

### 4.4 ATP routes and fields

Allowlist by event slug, ATP event ID, year, and route:

- tournament overview;
- results;
- draws;
- daily schedule; and
- linked ProTennisLive OOP PDF when covered by the grant.

Extract:

- ATP event ID, year, name, category, dates, location, surface, prize money,
  singles/doubles draw sizes, and official website;
- source-native player ID, slug, displayed name, seed, entry marker, and
  nationality when present;
- draw type, round, participants, winner, score, and terminal status;
- displayed tour marker at combined events;
- schedule local date, court, “starts at,” “not before,” or “followed by”
  ordering label; and
- linked OOP URL and document digest.

The parser must distinguish schedule ordering labels from exact start times.
“Followed by” has no exact time.

### 4.5 WTA routes and fields

Allowlist by WTA tournament ID, slug, year, and route:

- tournament overview;
- scores;
- order of play;
- draws;
- player list; and
- linked WTA OOP PDF.

Extract:

- WTA tournament ID, year, name, level, dates, location, surface, prize
  commitment, draw sizes, and official website;
- numeric player ID, displayed name, seed, entry marker, and nationality;
- draw type, round, participants, winner, score, and terminal status;
- selected order-of-play day, schedule local date, court, and ordering/time
  label; and
- OOP URL and document digest.

WTA pages can remain in an asynchronous “Loading” state. The fetcher uses a
bounded route-specific wait. If the required match container never becomes
stable:

- record `rendered_content_unavailable`;
- attempt the OOP fallback only when allowlisted;
- preserve the prior accepted projection; and
- never publish an inferred replacement.

### 4.6 Order-of-play artifacts

Order-of-play PDFs are evidence objects, not convenient mutable URLs.

For each permitted PDF:

- capture real retrieval time, byte length, media type, URL, and SHA-256;
- reject non-PDF payloads and suspicious size changes;
- retain bytes only under the grant’s storage mode;
- parse event, publication date, play date, court, ordering/time label, round,
  and named participants;
- keep the raw extracted text/geometry privately when redistribution is not
  granted;
- quarantine OCR-only or ambiguous rows; and
- append a new artifact and observation when the mutable URL changes.

### 4.7 Tennis-Data historical ingestion

Source:

- [All annual data files](http://www.tennis-data.co.uk/alldata.php)
- [Data notes and cadence](http://www.tennis-data.co.uk/data.php)

Use annual ATP and WTA spreadsheet files as the historical completed-match
baseline. The parser accepts match-level dates from 2003 onward. It rejects
pre-2003 ATP dates because the provider documents them as tournament start
dates rather than exact match dates.

For every annual download:

- record the HTTP transport limitation explicitly;
- validate XLS/XLSX magic bytes before parsing;
- compute SHA-256 immediately;
- validate required columns, source year, row count, date distribution,
  tournament identity, participants, round, score, and status;
- compare with the previous accepted digest;
- treat expected current-year additions as candidates for automatic
  reconciliation;
- quarantine unexplained changes to a closed prior year pending review; and
- keep raw spreadsheets private while publishing normalized facts,
  attribution, URLs, hashes, and reconciliation outcomes.

Tennis-Data is authoritative only after these checks. “Historical source of
truth” does not bypass integrity validation.

## 5. Permission policy

### 5.1 Policy record

Extend source policy records for official sites with:

```text
source
policy_state
terms_url
grant_id
grant_sha256
grant_reviewed_at
grant_expires_at
network_collection_enabled
allowed_domains
allowed_routes
allowed_fields
allowed_uses
retention_mode
redistribution_mode
minimum_request_interval
maximum_requests_per_day
attribution
parser_version
policy_revision
```

The repository stores non-secret grant metadata and the digest of the reviewed
permission document. The actual legal document can remain in controlled
storage.

### 5.2 Enablement rule

Official-site network collection is enabled only when all are true:

- `policy_state` explicitly permits collection;
- `network_collection_enabled=true`;
- `grant_id` and `grant_sha256` are present;
- the grant review and expiry dates are valid at collection time;
- the requested domain and route are allowlisted;
- the requested fields and uses are allowlisted;
- retention permits the selected artifact storage mode;
- redistribution permits the intended normalized public fields; and
- the request stays within frequency limits.

An environment Boolean or workflow input cannot enable collection by itself.
Missing, expired, changed, or insufficient policy fails before any ATP/WTA
network request.

Without a valid grant:

- synthetic and local-fixture parser tests still run;
- official fetchers make zero network requests;
- daily Wikipedia discovery still runs;
- official adapters report `policy_blocked`; and
- no official-site field enters a public release.

If a grant permits internal evaluation but not public redistribution, the
adapter may run in a private shadow workflow but remains ineligible for public
projection.

## 6. Public interfaces

Add these CLI boundaries while preserving existing release commands.

### `collect-live`

```text
open-tennis-data collect-live
  --as-of TIMESTAMP
  --parent-release TAG
  --permission-policy PATH
  --output SOURCE_BUNDLE
  [--offline-fixture-root PATH]
```

Responsibilities:

- verify the parent release;
- discover active/recent Wikipedia event editions;
- evaluate source policy before official-site access;
- capture eligible rendered pages and PDFs;
- close and hash the immutable source bundle; and
- make no canonical projection.

### `refresh-history`

```text
open-tennis-data refresh-history
  --years START:END
  --parent-release TAG
  --output SOURCE_BUNDLE
  [--offline-fixture-root PATH]
```

Responsibilities:

- fetch or load annual Tennis-Data files;
- validate signatures and schemas;
- compare prior digests;
- close the source bundle; and
- report current-year additions and prior-year mutations separately.

### `reconcile`

```text
open-tennis-data reconcile
  --source-bundle PATH
  --parent-release TAG
  --output DATASET
```

Responsibilities:

- verify bundle and parent hashes;
- parse immutable local artifacts;
- append source observations and identity links;
- apply policy and conflict rules;
- retain prior valid projections when new inputs are unavailable; and
- produce deterministic accepted and quarantine tables.

### Existing commands

- `bootstrap` becomes explicit genesis orchestration only.
- `validate` checks dataset and append-only contracts.
- `release` packages only a reconciled closed dataset.
- `verify-release` independently recomputes manifest, evidence, identity,
  coverage, and freshness gates.

No command may resolve a moving input after the source bundle closes.

## 7. Collection cadence

### 7.1 Daily active-event refresh

Run at the existing `04:17 UTC` schedule:

1. Resolve one exact parent release tag and manifest hash.
2. Set `collection_started_at`.
3. Refresh Wikipedia revisions for active events and events completed within
   the configured reconciliation grace period.
4. Evaluate ATP/WTA permission policy.
5. Scrape only permission-enabled official routes for those editions.
6. Capture permitted HTML/PDF artifacts and fetch telemetry.
7. Close the bundle and set `source_cutoff_at`.
8. Reconcile from the bundle and pinned parent.
9. Validate the staged dataset and deterministic release assets.
10. Publish only when every active hard gate passes.

The daily job does not download every historical annual file. A failed or
blocked official fetch leaves an explicit status and preserves eligible parent
observations; it never invents a replacement.

### 7.2 Weekly historical refresh

Run Monday at the existing `07:41 UTC` schedule:

1. Resolve the exact parent release.
2. Download every ATP and WTA annual file from 2020 through the current year.
3. Validate and hash each file.
4. Reprocess only changed files.
5. Reconcile expected current-year additions.
6. Quarantine unexplained prior-year deletions or mutations.
7. Compare row identity, material fields, provenance, and coverage with the
   pinned parent release.
8. Run the full longitudinal and stable-release verifier.
9. Publish only when retention, identity, evidence, and regression gates pass.

The workflow records unchanged digests without creating duplicate
observations.

### 7.3 Retry and rate behavior

- Honor the stricter of repository policy and written-grant frequency.
- Use bounded exponential backoff only for transient network/server failures.
- Do not retry policy, permission, CAPTCHA, authentication, selector, or schema
  failures.
- One daily run may make at most one successful capture per route and event
  edition unless the grant explicitly permits amendment polling.
- Record every attempt without exposing credentials or private grant content.

## 8. Delivery plan

### Phase 0 — Document and policy boundary

1. Land v3.1/v3.2 plan separation.
2. Amend source documentation with the new hierarchy.
3. Add blocked `atp-site`, `wta-site`, `atp-oop`, and `wta-oop` policy entries.
4. Define the grant schema and fail-closed evaluator.
5. Mark the old historical bulk source as migration-only.

Exit criteria:

- no official route can be fetched without a valid grant;
- the old historical source is no longer selected for new builds; and
- source labels and allowed fields are centrally enforced.

### Phase 1 — Historical baseline replacement

1. Promote Tennis-Data parsing from date-only evidence to complete terminal
   match observations.
2. Use Wikipedia aliases and player pages for identity context.
3. Reconcile every 2020+ baseline terminal match to replacement observations.
4. Preserve stable `match_id` and source-native aliases.
5. Produce migration dispositions and the v3.1 retention report.
6. Remove the old source from runtime bootstrap after the retention gate passes.

Exit criteria:

- automatic migration retains at least 99.9% of baseline terminal matches;
- no whole closed event edition or season disappears;
- every excluded row has a reviewed reason; and
- builds work with the old source unavailable.

### Phase 2 — Offline official-site parsers

1. Add Playwright/HTML/PDF scraping dependencies behind the `scrape` extra.
2. Create synthetic ATP and WTA HTML/PDF fixtures.
3. Implement overview, schedule/OOP, results, draw, and player-list parsers.
4. Implement combined-event tour classification.
5. Emit source-native observations and structured rejections only.
6. Run browser tests against a local fixture server.

Exit criteria:

- default tests make no ATP/WTA network request;
- DC combined-event fixtures parse without cross-tour duplicates;
- loading and selector failures quarantine rather than infer; and
- offline artifacts produce deterministic observations.

### Phase 3 — Closed-bundle collectors

1. Implement `collect-live`, `refresh-history`, and `reconcile`.
2. Store captured HTML/PDF/spreadsheet artifacts according to policy.
3. Add retrieval manifests, digests, clocks, and append-only bundle chaining.
4. Make daily collection incremental from a verified parent.
5. Make weekly refresh hash-diff annual files and compare longitudinally.

Exit criteria:

- identical closed bundles produce byte-identical datasets;
- failed live collection preserves eligible parent observations;
- mutable URL changes append revisions; and
- no moving input is accessed during parse or reconcile.

### Phase 4 — Permissioned shadow run

Start only after written ATP/WTA permission metadata is reviewed and committed.

1. Enable the applicable adapter in a private shadow workflow.
2. Run for at least 14 consecutive days and 100 distinct named slots, or one
   complete edition for a smaller seasonal format.
3. Compare every parsed value with reviewed evidence.
4. Exercise at least 20 permitted controlled changes covering postponement,
   retraction, status change, parser drift, policy expiry, and rollback.
5. Measure capture coverage, identity match rate, false-day outcomes, and
   amendment latency.

Exit criteria:

- zero false-day projections;
- 100% policy eligibility for candidate public fields;
- at least 99% named-slot recall and identity match rate;
- 100% timezone completeness for dated fixtures; and
- no unresolved evidence-chain failure.

### Phase 5 — Public canary and rollout

1. Enable one granted event family.
2. Write observations into public ledgers while keeping projection behind a
   feature gate for one full revision cycle.
3. Enable projection only after canary gates pass.
4. Expand event families one at a time.
5. Automatically disable new projection on a false day, policy failure,
   evidence corruption, or identity conflict.

Rollback appends policy/quarantine decisions and returns affected fixture dates
to null. It never deletes lawfully retained observations.

## 9. Test plan

### 9.1 Permission and network isolation

- missing grant;
- malformed grant digest;
- expired grant;
- route or domain not allowlisted;
- field or use not allowlisted;
- internal-only grant cannot project publicly;
- environment flag without policy cannot enable collection;
- request-frequency limit;
- retryable server failure;
- CAPTCHA, login, or consent-blocked page; and
- default CI proves zero official-site network calls.

### 9.2 ATP scraper

- overview metadata;
- source-native player IDs;
- results and completed score;
- retirement and walkover markers;
- draw rounds, byes, and unknown opponents;
- schedule court and exact displayed local date;
- “starts at,” “not before,” and “followed by” distinctions;
- mutable OOP link;
- selector drift; and
- unexpected route redirect.

### 9.3 WTA scraper

- overview metadata and draw sizes;
- numeric player IDs and seeds;
- scores/results;
- order-of-play day selection;
- draws and player list;
- asynchronous match container success;
- indefinite loading fallback;
- missing or invalid OOP PDF;
- selector drift; and
- source-native status mapping.

### 9.4 Mubadala DC combined event

- canonical shared edition and venue;
- `atp:418:2026` and `wta:1045:2026` aliases;
- separate tour draws;
- WTA row on ATP schedule remains WTA;
- same WTA match from ATP and WTA pages deduplicates;
- official player ID aliases resolve to canonical players;
- shared court/date evidence remains source-specific; and
- ATP and WTA matches with similar participants cannot collide.

### 9.5 Historical annual files

- XLS and XLSX signatures;
- pre-2003 tournament-start date rejection;
- missing required columns;
- wrong source year;
- malformed date or score;
- retirement, walkover, and default status;
- current-year append;
- prior-year deletion or mutation;
- changed digest with identical normalized rows;
- HTTP transport risk recorded; and
- raw file absent from public release assets.

### 9.6 Reconciliation and lifecycle

- active official result later matched by weekly historical row;
- event alias and player identity mismatch;
- participants-only match rejected;
- round/score disagreement;
- independent material conflict;
- postponement and reschedule;
- date withdrawal;
- fixture becomes terminal across two clean builds;
- prior observation retained after fetch failure; and
- source-policy revocation disables projection without mutating history.

### 9.7 Local and release validation

Run:

```text
ruff check .
mypy src tests
coverage run -m unittest discover -s tests
coverage report
python -m build
install and smoke-test the wheel and sdist
npm test
npx playwright test
open-tennis-data validate --data <offline reconciled dataset>
open-tennis-data verify-release --directory <offline release>
```

Live ATP/WTA smoke tests are manual and permission-gated. They are never
required by default CI.

Before any source implementation PR is pushed, download the immutable baseline
release, verify its manifest and checksums, and compare the candidate dataset
against its row, identity, and coverage inventory.

## 10. Pull-request sequence

1. `source-policy-grants`: new source labels, grant metadata, and fail-closed
   policy evaluator.
2. `historical-baseline-replacement`: complete Tennis-Data observations,
   Wikipedia identity context, migration dispositions, and old-source removal.
3. `official-site-offline-parsers`: scraping dependencies, synthetic fixtures,
   ATP/WTA parsers, PDF parsing, and DC combined-event tests.
4. `closed-source-bundle-commands`: `collect-live`, `refresh-history`,
   `reconcile`, artifact manifests, and deterministic offline tests.
5. `incremental-daily-workflow`: pinned parent, active-event discovery,
   permission evaluation, closed bundle, validation, and fail-closed release.
6. `longitudinal-weekly-workflow`: annual hash diff, changed-season rebuild,
   prior-release comparison, and mutation quarantine.
7. `permissioned-official-shadow`: only after a written grant; private metrics
   and controlled revision exercises.
8. `official-site-public-canary`: only after shadow gates pass; one event
   family, projection gate, rollback, and staged expansion.

Do not combine generated public datasets with adapter code. Every PR must keep
network access blocked until its exact permission and rollout phase is met.

## 11. Acceptance criteria and defaults

- Wikipedia is the primary event-discovery, alias, identity, and topology
  source.
- Official ATP/WTA pages are the active-event detail source only when a valid
  written permission policy enables them.
- Playwright with Chromium is the official-site scraping mechanism.
- Official HTML and PDFs are parsed from immutable local captures.
- Tennis-Data is the validated completed-match baseline from 2003 onward.
- The public dataset remains scoped to 2020 onward.
- Pre-2003 annual dates are not exact match-day evidence.
- Raw Tennis-Data spreadsheets are private.
- HTTP transport risk is recorded and never described as authenticated.
- Daily collection is incremental and active-event-only.
- Weekly collection rechecks all annual files from 2020 through the current
  year and rebuilds changed seasons only.
- Combined ATP/WTA events retain separate tour/draw identities.
- Missing or blocked official data preserves prior eligible state and produces
  explicit telemetry.
- Material conflicts quarantine the field; they do not silently overwrite it.
- No official network request occurs in default CI.
- No adapter can bypass v3.1 identity, evidence, lifecycle, or release gates.
