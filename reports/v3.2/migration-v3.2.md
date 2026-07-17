# Open Tennis Data v3.2 migration

- Status: passed
- As of: 2026-07-17
- Match rows retained: 1742440
- Fixture rows migrated: 21
- Missing `best_of` values backfilled: 4534
- Ambiguous provenance rows quarantined: 120
- Retained-field differences: 0
- Completed match IDs preserved: 1742440
- Rewritten match partitions: 118
- Old catalog checksum: `4a42a15a00a38f190bfd7477f898ffbbf443648aeb8f201b46f525a2cf478909`
- New catalog checksum: `87891ae42889739cf8a10b64a82e06c222fda0e29c29b85f54b9e341a47dc6ff`

## Schemas

- Old completed schema: `[('match_id', 'VARCHAR'), ('tournament_id', 'VARCHAR'), ('tour', 'VARCHAR'), ('year', 'SMALLINT'), ('draw', 'VARCHAR'), ('round', 'VARCHAR'), ('player1_id', 'VARCHAR'), ('player1_name', 'VARCHAR'), ('player1_country', 'VARCHAR'), ('player2_id', 'VARCHAR'), ('player2_name', 'VARCHAR'), ('player2_country', 'VARCHAR'), ('winner_id', 'VARCHAR'), ('loser_id', 'VARCHAR'), ('player1_seed', 'VARCHAR'), ('player2_seed', 'VARCHAR'), ('player1_entry', 'VARCHAR'), ('player2_entry', 'VARCHAR'), ('player1_rank', 'INTEGER'), ('player2_rank', 'INTEGER'), ('player1_rank_points', 'INTEGER'), ('player2_rank_points', 'INTEGER'), ('status', 'VARCHAR'), ('score', 'VARCHAR'), ('best_of', 'TINYINT')]`
- New completed schema: `[('date', 'DATE'), ('match_id', 'VARCHAR'), ('tournament_id', 'VARCHAR'), ('tournament_name', 'VARCHAR'), ('tour', 'VARCHAR'), ('year', 'SMALLINT'), ('draw', 'VARCHAR'), ('round', 'VARCHAR'), ('format', 'VARCHAR'), ('player1_id', 'VARCHAR[]'), ('player1_name', 'VARCHAR[]'), ('player1_seed', 'VARCHAR'), ('player2_id', 'VARCHAR[]'), ('player2_name', 'VARCHAR[]'), ('player2_seed', 'VARCHAR'), ('winner_id', 'VARCHAR[]'), ('status', 'VARCHAR'), ('score', 'VARCHAR'), ('best_of', 'TINYINT')]`
- Old fixture schema: `[('fixture_id', 'VARCHAR'), ('tournament_id', 'VARCHAR'), ('tour', 'VARCHAR'), ('year', 'SMALLINT'), ('draw', 'VARCHAR'), ('round', 'VARCHAR'), ('player1_id', 'VARCHAR'), ('player1_name', 'VARCHAR'), ('player2_id', 'VARCHAR'), ('player2_name', 'VARCHAR'), ('scheduled_on', 'DATE'), ('source_url', 'VARCHAR')]`
- New fixture schema: `[('date', 'DATE'), ('match_id', 'VARCHAR'), ('tournament_id', 'VARCHAR'), ('tournament_name', 'VARCHAR'), ('tour', 'VARCHAR'), ('year', 'SMALLINT'), ('draw', 'VARCHAR'), ('round', 'VARCHAR'), ('format', 'VARCHAR'), ('player1_id', 'VARCHAR[]'), ('player1_name', 'VARCHAR[]'), ('player1_seed', 'VARCHAR'), ('player2_id', 'VARCHAR[]'), ('player2_name', 'VARCHAR[]'), ('player2_seed', 'VARCHAR'), ('winner_id', 'VARCHAR[]'), ('status', 'VARCHAR'), ('score', 'VARCHAR'), ('best_of', 'TINYINT')]`
