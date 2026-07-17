# Open Tennis Data v3.2 migration

- Status: passed
- As of: 2026-07-17
- Match rows retained: 1742444
- Fixture rows migrated: 22
- Missing `best_of` values backfilled: 4538
- Ambiguous provenance rows quarantined: 120
- Retained-field differences: 0
- Completed match IDs preserved: 1742444
- Rewritten match partitions: 118
- Old catalog checksum: `1659f659b9948ee19cdc176be8d818cc9345ed7e8a228069bab3411dba46194d`
- New catalog checksum: `d6dfc35555a9d0bb58b65292c5c50b0e857da098c553ae5d3b74483049365cab`

## Schemas

- Old completed schema: `[('match_id', 'VARCHAR'), ('tournament_id', 'VARCHAR'), ('tour', 'VARCHAR'), ('year', 'SMALLINT'), ('draw', 'VARCHAR'), ('round', 'VARCHAR'), ('player1_id', 'VARCHAR'), ('player1_name', 'VARCHAR'), ('player1_country', 'VARCHAR'), ('player2_id', 'VARCHAR'), ('player2_name', 'VARCHAR'), ('player2_country', 'VARCHAR'), ('winner_id', 'VARCHAR'), ('loser_id', 'VARCHAR'), ('player1_seed', 'VARCHAR'), ('player2_seed', 'VARCHAR'), ('player1_entry', 'VARCHAR'), ('player2_entry', 'VARCHAR'), ('player1_rank', 'INTEGER'), ('player2_rank', 'INTEGER'), ('player1_rank_points', 'INTEGER'), ('player2_rank_points', 'INTEGER'), ('status', 'VARCHAR'), ('score', 'VARCHAR'), ('best_of', 'TINYINT')]`
- New completed schema: `[('date', 'DATE'), ('match_id', 'VARCHAR'), ('tournament_id', 'VARCHAR'), ('tournament_name', 'VARCHAR'), ('tour', 'VARCHAR'), ('year', 'SMALLINT'), ('draw', 'VARCHAR'), ('round', 'VARCHAR'), ('format', 'VARCHAR'), ('player1_id', 'VARCHAR[]'), ('player1_name', 'VARCHAR[]'), ('player1_seed', 'VARCHAR'), ('player2_id', 'VARCHAR[]'), ('player2_name', 'VARCHAR[]'), ('player2_seed', 'VARCHAR'), ('winner_id', 'VARCHAR[]'), ('status', 'VARCHAR'), ('score', 'VARCHAR'), ('best_of', 'TINYINT')]`
- Old fixture schema: `[('fixture_id', 'VARCHAR'), ('tournament_id', 'VARCHAR'), ('tour', 'VARCHAR'), ('year', 'SMALLINT'), ('draw', 'VARCHAR'), ('round', 'VARCHAR'), ('player1_id', 'VARCHAR'), ('player1_name', 'VARCHAR'), ('player2_id', 'VARCHAR'), ('player2_name', 'VARCHAR'), ('scheduled_on', 'DATE'), ('source_url', 'VARCHAR')]`
- New fixture schema: `[('date', 'DATE'), ('match_id', 'VARCHAR'), ('tournament_id', 'VARCHAR'), ('tournament_name', 'VARCHAR'), ('tour', 'VARCHAR'), ('year', 'SMALLINT'), ('draw', 'VARCHAR'), ('round', 'VARCHAR'), ('format', 'VARCHAR'), ('player1_id', 'VARCHAR[]'), ('player1_name', 'VARCHAR[]'), ('player1_seed', 'VARCHAR'), ('player2_id', 'VARCHAR[]'), ('player2_name', 'VARCHAR[]'), ('player2_seed', 'VARCHAR'), ('winner_id', 'VARCHAR[]'), ('status', 'VARCHAR'), ('score', 'VARCHAR'), ('best_of', 'TINYINT')]`
