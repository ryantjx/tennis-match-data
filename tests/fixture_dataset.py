from __future__ import annotations

from pathlib import Path

import duckdb

from open_tennis_data.dataset import (
    MATCH_ROW_GROUP_SIZE,
    OBSERVATION_ROW_GROUP_SIZE,
    _copy_parquet,
)


def write_release_input(root: Path) -> None:
    """Write the smallest source-backed dataset accepted by the release builder."""
    connection = duckdb.connect()

    def write(
        relative: str,
        query: str,
        *,
        match_shaped: bool = False,
    ) -> None:
        _copy_parquet(
            connection,
            query,
            root / relative,
            row_group_size=(
                MATCH_ROW_GROUP_SIZE if match_shaped else OBSERVATION_ROW_GROUP_SIZE
            ),
            match_shaped=match_shaped,
        )

    write(
        "matches/tour=atp/year=2025/matches.parquet",
        """
        SELECT DATE '2025-07-13' AS date,
          'match:atp:test-completed'::VARCHAR AS match_id,
          'tournament_atp_2025_test'::VARCHAR AS tournament_id,
          'Test Open'::VARCHAR AS tournament_name,
          'atp'::VARCHAR AS tour,2025::SMALLINT AS year,
          'main'::VARCHAR AS draw,'F'::VARCHAR AS round,
          'singles'::VARCHAR AS format,
          ['player:atp:one']::VARCHAR[] AS player1_id,
          ['Jannik Sinner']::VARCHAR[] AS player1_name,
          '1'::VARCHAR AS player1_seed,
          ['player:atp:two']::VARCHAR[] AS player2_id,
          ['Carlos Alcaraz']::VARCHAR[] AS player2_name,
          '2'::VARCHAR AS player2_seed,
          ['player:atp:one']::VARCHAR[] AS winner_id,
          'completed'::VARCHAR AS status,'6-4 6-4'::VARCHAR AS score,
          3::TINYINT AS best_of,
          ['sackmann','tennis-data.co.uk']::VARCHAR[] AS source
        """,
        match_shaped=True,
    )
    write(
        "fixtures/tour=atp/current.parquet",
        """
        SELECT DATE '2026-07-25' AS date,
          'match:atp:test-fixture'::VARCHAR AS match_id,
          'tournament_atp_2026_test'::VARCHAR AS tournament_id,
          'Future Open'::VARCHAR AS tournament_name,
          'atp'::VARCHAR AS tour,2026::SMALLINT AS year,
          'main'::VARCHAR AS draw,'R32'::VARCHAR AS round,
          'singles'::VARCHAR AS format,
          ['player:atp:one']::VARCHAR[] AS player1_id,
          ['Jannik Sinner']::VARCHAR[] AS player1_name,
          NULL::VARCHAR AS player1_seed,
          NULL::VARCHAR[] AS player2_id,
          NULL::VARCHAR[] AS player2_name,
          NULL::VARCHAR AS player2_seed,
          NULL::VARCHAR[] AS winner_id,
          'fixture'::VARCHAR AS status,NULL::VARCHAR AS score,
          3::TINYINT AS best_of,
          ['wikimedia']::VARCHAR[] AS source
        """,
        match_shaped=True,
    )
    for year, tournament_id, name, start, end in (
        (2025, "tournament_atp_2025_test", "Test Open", "2025-07-07", "2025-07-13"),
        (2026, "tournament_atp_2026_test", "Future Open", "2026-07-25", "2026-07-31"),
    ):
        write(
            f"tournaments/tour=atp/year={year}/tournaments.parquet",
            f"""
            SELECT '{tournament_id}'::VARCHAR AS tournament_id,
              'atp'::VARCHAR AS tour,{year}::SMALLINT AS year,
              '{name}'::VARCHAR AS tournament_name,
              'other'::VARCHAR AS level,'hard'::VARCHAR AS surface,
              false AS indoor,DATE '{start}' AS start_date,DATE '{end}' AS end_date,
              'London'::VARCHAR AS city,'GBR'::VARCHAR AS country,
              'https://example.test/{year}'::VARCHAR AS source_url
            """,
        )
    write(
        "players/tour=atp/players.parquet",
        """
        SELECT * FROM (VALUES
          ('player:atp:one','atp','Jannik Sinner'),
          ('player:atp:two','atp','Carlos Alcaraz')
        ) players(player_id,tour,name)
        """,
    )
    write(
        "observations/tour=atp/year=2025/observations.parquet",
        """
        SELECT 'match:atp:test-completed'::VARCHAR AS match_id,
          'atp'::VARCHAR AS tour,2025::SMALLINT AS year,
          'source_file_sackmann'::VARCHAR AS source_file_id,
          'test-completed'::VARCHAR AS source_match_id
        """,
    )
    write(
        "observations/tour=atp/year=2026/observations.parquet",
        """
        SELECT 'match:atp:test-fixture'::VARCHAR AS match_id,
          'atp'::VARCHAR AS tour,2026::SMALLINT AS year,
          'source_file_wikimedia'::VARCHAR AS source_file_id,
          'test-fixture'::VARCHAR AS source_match_id
        """,
    )
    write(
        "date_observations/tour=atp/year=2025/date-observations.parquet",
        """
        SELECT 'match:atp:test-completed'::VARCHAR AS match_id,
          'atp'::VARCHAR AS tour,2025::SMALLINT AS year,
          DATE '2025-07-13' AS played_on,
          'source_file_tennis_data'::VARCHAR AS source_file_id,
          'test-date'::VARCHAR AS source_match_id,
          'day'::VARCHAR AS date_precision,
          'participants_round_score'::VARCHAR AS match_method,
          'fingerprint-date'::VARCHAR AS row_fingerprint
        """,
    )
    write(
        "coverage/source-audit.parquet",
        """
        SELECT * FROM (VALUES
          ('source_file_sackmann','matches','atp',2025,'tour',
           'atp/atp_matches_2025.csv','https://example.test/sackmann',
           'revision-sackmann','sha-sackmann','CC-BY-NC-SA-4.0',1,1,0),
          ('source_file_tennis_data','match_dates','atp',2025,'tennis-data.co.uk',
           '2025.xlsx','https://example.test/tennis-data',
           'revision-tennis-data','sha-tennis-data','source terms',1,1,0),
          ('source_file_wikimedia','fixtures','atp',2026,'wikimedia',
           'Future Open','https://example.test/wikimedia',
           'revision-wikimedia','sha-wikimedia','CC-BY-SA-4.0',1,1,0)
        ) audit(source_file_id,kind,tour,year,source_label,source_path,source_url,
          revision,sha256,license,source_rows,normalized_rows,quarantined_rows)
        """,
    )
    write(
        "quarantine/quarantine.parquet",
        """
        SELECT NULL::VARCHAR AS tour,NULL::SMALLINT AS year,
          NULL::VARCHAR AS source_label,NULL::VARCHAR AS source_path,
          NULL::VARCHAR AS source_file_id,NULL::VARCHAR AS source_match_id,
          NULL::VARCHAR AS row_fingerprint,
          NULL::VARCHAR[] AS candidate_match_ids,NULL::VARCHAR AS reason
        WHERE false
        """,
    )
    connection.close()
