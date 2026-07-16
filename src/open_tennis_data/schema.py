"""Shared constants for the Parquet data model."""

SCHEMA_VERSION = "3.2"
SCHEMA_METADATA_KEY = "open_tennis_data_schema_version"
TOURS = ("atp", "wta")
DRAWS = ("main", "qualifying")
FORMATS = ("singles", "doubles")
MATCH_STATUSES = (
    "fixture",
    "completed",
    "walkover",
    "retired",
    "defaulted",
    "abandoned",
    "cancelled",
)
MATCH_COLUMNS = (
    "date",
    "match_id",
    "tournament_id",
    "tournament_name",
    "tour",
    "year",
    "draw",
    "round",
    "format",
    "player1_id",
    "player1_name",
    "player1_seed",
    "player2_id",
    "player2_name",
    "player2_seed",
    "winner_id",
    "status",
    "score",
    "best_of",
)
MATCH_SCHEMA = (
    ("date", "DATE"),
    ("match_id", "VARCHAR"),
    ("tournament_id", "VARCHAR"),
    ("tournament_name", "VARCHAR"),
    ("tour", "VARCHAR"),
    ("year", "SMALLINT"),
    ("draw", "VARCHAR"),
    ("round", "VARCHAR"),
    ("format", "VARCHAR"),
    ("player1_id", "VARCHAR[]"),
    ("player1_name", "VARCHAR[]"),
    ("player1_seed", "VARCHAR"),
    ("player2_id", "VARCHAR[]"),
    ("player2_name", "VARCHAR[]"),
    ("player2_seed", "VARCHAR"),
    ("winner_id", "VARCHAR[]"),
    ("status", "VARCHAR"),
    ("score", "VARCHAR"),
    ("best_of", "TINYINT"),
)
SOURCE_LICENSES = {
    "sackmann": "CC-BY-NC-SA-4.0",
    "wikimedia": "CC-BY-SA-4.0",
    "community": "CC0-1.0",
}
