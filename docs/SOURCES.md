# Sources and collection policy

Last reviewed: 2026-07-16.

## Sackmann / Tennis Abstract archive

- Primary origin: Jeff Sackmann's ATP and WTA repositories.
- Current transport fallback: the pinned `Aneeshers/tennis-sackmann-archive`
  Hugging Face revision because the original repositories were unavailable
  during the initial source rebuild.
- Coverage: tour singles, qualifying, Challenger, ATP Futures, WTA ITF,
  players, statistics, and historical rankings.
- Licence: CC BY-NC-SA 4.0.
- Every downloaded file is pinned to one repository revision, checksummed, and
  reconciled as `source rows = normalized observations + quarantined rows`.
- Source URLs, revisions, checksums, and licences are stored once in source-file
  records and linked to matches through compact provenance; match rows contain
  no source URL.
- Source-file identity includes provider label, URL, immutable revision,
  checksum, ingestion role, and tour. This distinguishes a shared page used for
  multiple roles or tour-specific records without duplicating an identity.

The direct origin should be restored automatically when it becomes reachable
and its content hashes reconcile with the fallback.

## Wikimedia

- Access: English Wikipedia MediaWiki API.
- Coverage: maintained current ATP/WTA main and qualifying singles draw pages.
- Uses: fresh completed results plus best-effort unplayed draw slots.
- Licence: CC BY-SA 4.0, with page URL, revision ID, and content checksum.
- Limitation: community maintained; fixtures may lack exact dates and are not a
  complete schedule service.
- Tournament metadata is filled only from recorded immutable page revisions;
  a tournament window is never substituted for an exact fixture date.

## Community corrections

Contributors dedicate factual corrections to CC0 and provide a public source.
Corrections must not be copied from a protected database or gathered in breach
of access terms. Proposals identify `entity_type` and `entity_id`; the
deprecated `--match-id` shorthand remains available for one release cycle.

## Excluded automation

ATP, WTA, ITF, Tennis TV, commercial livescore sites, odds feeds, and similar
APIs are not harvested without written automated-access and bulk-redistribution
permission. Public visibility or a free API tier is not a redistribution
licence.

The official ATP and WTA adapters therefore remain disabled. Enabling either
requires recording written permission in this policy before adding credentials
or automation. See the [ATP terms](https://www.atptour.com/en/terms-and-conditions)
and [WTA terms](https://www.wtatennis.com/terms-and-conditions).
