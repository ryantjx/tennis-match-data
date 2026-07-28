# Sources and collection policy

Last reviewed: 2026-07-24.

The machine-readable registry is
[`src/open_tennis_data/sources.json`](../src/open_tennis_data/sources.json).
Release generation fails when
a referenced source is missing, blocked, lacks
`public_research_release` in `allowed_uses`, or lacks required attribution and
policy metadata.

## Tennis-Data.co.uk

Season XLS/XLSX files provide complete terminal observations for the 2020+
history. Each source file is content-hashed and each row receives a native ID,
row fingerprint, and exactly one accepted or quarantined migration
disposition. Late-December matches in the following season file are accepted
only within that bounded crossover; implausible future-year dates fail closed.

ATP files before 2003 use unsupported tournament-date semantics and are
quarantined as `tournament_date_not_match_date`. Malformed dates, participants,
and tournament names are also quarantined.

The registry describes these as research-use inputs under the publisher’s
source notice. No commercial redistribution grant is claimed.

## Wikimedia

Pinned MediaWiki page revisions supply tournament/draw identity, explicit
results, future draw slots, and match or schedule dates when the page actually
states them. Applicable page licences and revision attribution remain attached.

A tournament range or page publication timestamp is not match-day evidence.
Community-maintained future data may be incomplete or rescheduled.

## Sackmann / Tennis Abstract

The pinned bulk archive is retained only for migration identity/result
cross-checking under CC BY-NC-SA 4.0. V3.2 historical runtime ingestion and
new public projections use Tennis-Data observations after the retention gate
passes.

`tourney_date` is a tournament date and never qualifies as evidence for the
public match `date`. Qualifying, Challenger, WTA 125, ITF/Futures, doubles,
rankings, and statistics from the archive are outside the v3 release scope.

## ATP/WTA sites and order-of-play documents

Offline HTML and PDF parser coverage includes overview, draw, result, schedule,
player-list, order-of-play, combined-event classification, walkovers, ordering
labels, missing fields, and source changes. Automated collection and public
projection are blocked by the registry. Policy is evaluated before a browser
or HTTP client is created, so the default path makes zero ATP/WTA requests.

An adapter may be enabled only after separate written permission is recorded
with grant identity and digest, expiry, domains, routes, fields, uses,
retention, redistribution, and positive request limits in a reviewed policy
revision.

## Community corrections

Corrections require a public supporting URL and are dedicated by contributors
to CC0 1.0. A correction never overrides source policy or makes an unsupported
date exact.

## Failure behavior

The collector/release path quarantines or rejects:

- missing or invalid venue timezones for timestamps;
- tournament-only, imprecise, or malformed dates;
- ambiguous player/tournament identities;
- conflicting exact dates;
- unmatched source rows;
- duplicates and malformed lifecycle rows; and
- unregistered or policy-blocked sources.

The pipeline does not bypass authentication, paywalls, rate limits, robots
controls, or access restrictions. Public visibility is not a licence.
