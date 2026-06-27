import sqlite3
from datetime import date

from scorerecord.db import init_db
from scorerecord.ingest.football_data import (
    build_source_event_id,
    normalize_identifier,
    normalize_season,
    parse_football_data_csv,
    season_to_football_data_code,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_football_data_schema_is_initialized() -> None:
    conn = _conn()

    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'historical_%'"
        )
    }

    assert {"historical_match", "historical_odds"}.issubset(tables)


def test_football_data_season_and_event_id_are_stable() -> None:
    assert normalize_season("2023/24") == "2023-2024"
    assert normalize_season("2324") == "2023-2024"
    assert season_to_football_data_code("2023-2024") == "2324"
    assert normalize_identifier("Man United") == "man-united"

    event_id = build_source_event_id(
        league_code="E0",
        season="2023-2024",
        match_date=date(2024, 4, 21),
        home_team="Arsenal",
        away_team="Chelsea",
    )

    assert event_id == "football-data:E0:2023-2024:2024-04-21:arsenal:chelsea"


def test_parse_football_data_csv_extracts_avg_open_and_close_1x2() -> None:
    text = (
        "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,AvgH,AvgD,AvgA,AvgCH,AvgCD,AvgCA\n"
        "E0,11/08/2023,Burnley,Man City,0,3,A,8.00,5.00,1.36,9.00,5.50,1.33\n"
    )

    parsed = parse_football_data_csv(text, league_code="E0", season="2023-2024")

    assert parsed.rows_seen == 1
    assert parsed.rows_loaded == 1
    assert parsed.missing_results == 0
    assert parsed.missing_open_odds == 2
    assert parsed.missing_closing_odds == 0
    assert len(parsed.events) == 1
    assert len(parsed.quotes) == 3
    assert parsed.events[0].result_1x2 == "away"
    keyed = {quote.selection: quote for quote in parsed.quotes}
    assert keyed["home"].entry_odds == 8.00
    assert keyed["away"].closing_odds == 1.33
    assert keyed["draw"].entry_odds_type == "avg_open"
    assert keyed["draw"].closing_odds_type == "avg_close"
