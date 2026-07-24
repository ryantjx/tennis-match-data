# Parquet schemas

This document describes the implemented legacy v3.2 contract. In v3.2,
completed `date` falls back to the source tournament start date when an exact
played day is unavailable. The exact-evidence v4 target is documented in
[`OBJECTIVE.md`](../OBJECTIVE.md) and is not implemented yet.

Open Tennis Data v3.2 publishes one match contract for completed results,
future fixtures, extracts, and rolling release assets. Every match-shaped file
has metadata `open_tennis_data_schema_version=3.2` and these 19 columns in this
exact order:

```text
date, match_id, tournament_id, tournament_name, tour, year, draw, round,
format, player1_id, player1_name, player1_seed,
player2_id, player2_name, player2_seed,
winner_id, status, score, best_of
```

The physical DuckDB/Arrow types are:

```text
DATE, VARCHAR, VARCHAR, VARCHAR, VARCHAR, SMALLINT, VARCHAR, VARCHAR,
VARCHAR, VARCHAR[], VARCHAR[], VARCHAR,
VARCHAR[], VARCHAR[], VARCHAR,
VARCHAR[], VARCHAR, VARCHAR, TINYINT
```

Participant IDs and names and `winner_id` are always lists in Parquet. Singles
use one-element lists; doubles use two-element lists. Current ingestion remains
singles-only, while validators and synthetic tests support doubles.

Completed rows require both participant slots. A terminal winner must exactly
equal one participant ID list. The 303 source-declared completed results whose
provenance has no score remain `status=completed, score=null`; validation never
invents a score. Completed rows prefer an exact match date and otherwise use
the source tournament start date; terminal rows cannot be published undated.

Fixtures use `status=fixture`, keep `winner_id` and `score` null, and may have a
null `date` or unresolved participant slot. Their lifecycle-stable `match_id`
survives conversion to a completed result. A match cannot be published in both
completed and future data.

The status domain is `fixture`, `completed`, `walkover`, `retired`, `defaulted`,
`abandoned`, and `cancelled`. `best_of` accepts source values 1, 3, and 5.
Missing singles values are backfilled as WTA 3, ATP Grand Slam main draw 5, and
other ATP draws 3.

## Annual tournaments

Tournament partitions and both rolling release families include:

```text
tournament_id, tour, year, tournament_name, level, surface, indoor,
start_date, end_date, city, country, source_url
```

One immutable ID represents an annual tour edition and is shared by main and
qualifying draws. ATP and WTA editions remain distinct. `tournaments.parquet`
is authoritative for `tournament_name`; copied names in every match row must
match it exactly.

## Provenance and auxiliary data

`observations` and release `provenance.parquet` contain only:

```text
match_id, tour, year, source_file_id, source_match_id
```

Release `ambiguities.parquet` contains source observations that cannot be
truthfully assigned to one canonical match:

```text
tour, year, source_file_id, source_match_id, candidate_match_ids, reason
```

Every released match has either direct provenance or appears in an ambiguity
candidate list. Ambiguity rows use `reason=ambiguous_source_mapping` and never
select a candidate on the source's behalf.

`source-audit.parquet` and release `sources.parquet` store URLs, revisions,
checksums, licences, and reconciliation totals once per referenced source file.
`source_file_id` hashes source label, URL, revision, content checksum, role, and
tour, so each source record is unique even when one page serves multiple roles
or tours.
Match-shaped rows never contain `source_url`.

`quarantine.parquet` contains:

```text
tour, year, source_label, source_path, source_file_id, source_match_id,
row_fingerprint, candidate_match_ids, reason
```

`candidate_match_ids` is nullable and is populated only for
`ambiguous_source_mapping` evidence. Those rows preserve every ambiguous source
observation without selecting a canonical identity that the source does not prove.

`identity/match-aliases.parquet` resolves retired exact-duplicate IDs through:

```text
retired_match_id, canonical_match_id, reason, changed_on
```

Alias targets are live canonical matches. Retired IDs are absent from match tables;
aliases are unique and cannot form chains or cycles.

Players, rankings, match statistics, tournament/player source crosswalks,
coverage, health, conflicts, quarantine, and corrections keep entity-specific
schemas. Rankings remain available as an auxiliary archive even though public
rank and rank-point columns were removed from match rows.

## Physical layout

Match-shaped files use DuckDB 1.5.4, Parquet V2, Zstandard level 19, 65,536-row
groups, a 1 MiB dictionary page limit, one writer thread, stable ordering, and
schema-version metadata. `open-tennis-data validate` checks the contract,
metadata, checksums, row groups, identities, references, lifecycle rules,
provenance, and the 75 MB file limit.
