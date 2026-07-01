import sqlite3
from datetime import date

from scorerecord.db import init_db
from scorerecord.ingest.football_data import (
    build_source_event_id,
    football_data_coverage_report,
    football_data_season_range,
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
    assert football_data_season_range("2021-2022", "2023-2024") == [
        "2021-2022",
        "2022-2023",
        "2023-2024",
    ]
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


def test_parse_football_data_csv_extracts_avg_preclose_and_close_ou25() -> None:
    text = (
        "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,AvgH,AvgD,AvgA,AvgCH,AvgCD,AvgCA,"
        "Avg>2.5,Avg<2.5,AvgC>2.5,AvgC<2.5\n"
        "E0,11/08/2023,Burnley,Man City,0,3,A,8.00,5.00,1.36,9.00,5.50,1.33,"
        "1.90,1.95,1.85,2.00\n"
    )

    parsed = parse_football_data_csv(text, league_code="E0", season="2023-2024")

    assert parsed.rows_seen == 1
    assert parsed.missing_open_odds == 2
    assert parsed.missing_closing_odds == 0
    assert len(parsed.quotes) == 5
    keyed = {(quote.market, quote.selection): quote for quote in parsed.quotes}
    assert keyed[("OU2.5", "over")].line == 2.5
    assert keyed[("OU2.5", "over")].entry_odds == 1.90
    assert keyed[("OU2.5", "under")].closing_odds == 2.00
    assert keyed[("OU2.5", "over")].entry_odds_type == "avg_preclose"
    assert keyed[("OU2.5", "under")].closing_odds_type == "avg_close"


def test_parse_football_data_csv_extracts_bbav_ou25_aliases() -> None:
    text = (
        "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,AvgH,AvgD,AvgA,AvgCH,AvgCD,AvgCA,"
        "BbAv>2.5,BbAv<2.5,BbAvC>2.5,BbAvC<2.5\n"
        "E0,11/08/2023,Burnley,Man City,0,3,A,8.00,5.00,1.36,9.00,5.50,1.33,"
        "1.91,1.94,1.86,1.99\n"
    )

    parsed = parse_football_data_csv(text, league_code="E0", season="2023-2024")

    keyed = {(quote.market, quote.selection): quote for quote in parsed.quotes}
    assert keyed[("OU2.5", "over")].entry_odds == 1.91
    assert keyed[("OU2.5", "under")].entry_odds == 1.94
    assert keyed[("OU2.5", "over")].closing_odds == 1.86
    assert keyed[("OU2.5", "under")].closing_odds == 1.99


def test_football_data_coverage_report_counts_avg_open_and_close() -> None:
    conn = _conn()
    conn.execute(
        """
        INSERT INTO historical_match (
          source, source_event_id, league_code, season, match_date,
          home_team, away_team, home_goals, away_goals, result_1x2
        )
        VALUES (
          'football-data', 'football-data:E0:2023-2024:2024-01-01:a:b',
          'E0', '2023-2024', '2024-01-01', 'A', 'B', 1, 0, 'home'
        )
        """
    )
    for selection in ("home", "draw", "away"):
        conn.execute(
            """
            INSERT INTO historical_odds (
              source, source_event_id, bookmaker, market, selection,
              entry_odds, entry_odds_type, closing_odds, closing_odds_type
            )
            VALUES (
              'football-data', 'football-data:E0:2023-2024:2024-01-01:a:b',
              'Avg', '1X2', ?, 2.0, 'avg_open', 1.9, 'avg_close'
            )
            """,
            (selection,),
        )

    rows = football_data_coverage_report(conn, league="EPL")

    assert len(rows) == 1
    assert rows[0].matches == 1
    assert rows[0].avg_open_rows == 3
    assert rows[0].avg_close_rows == 3
    assert rows[0].avg_open_coverage == 1.0


def test_football_data_coverage_report_counts_ou25_avg_preclose_and_close() -> None:
    conn = _conn()
    conn.execute(
        """
        INSERT INTO historical_match (
          source, source_event_id, league_code, season, match_date,
          home_team, away_team, home_goals, away_goals, result_1x2
        )
        VALUES (
          'football-data', 'football-data:E0:2023-2024:2024-01-01:a:b',
          'E0', '2023-2024', '2024-01-01', 'A', 'B', 1, 0, 'home'
        )
        """
    )
    for selection in ("over", "under"):
        conn.execute(
            """
            INSERT INTO historical_odds (
              source, source_event_id, bookmaker, market, selection, line,
              entry_odds, entry_odds_type, closing_odds, closing_odds_type
            )
            VALUES (
              'football-data', 'football-data:E0:2023-2024:2024-01-01:a:b',
              'Avg', 'OU2.5', ?, 2.5, 2.0, 'avg_preclose', 1.9, 'avg_close'
            )
            """,
            (selection,),
        )

    rows = football_data_coverage_report(conn, league="EPL", market="OU2.5")

    assert len(rows) == 1
    assert rows[0].market == "OU2.5"
    assert rows[0].matches == 1
    assert rows[0].avg_open_rows == 2
    assert rows[0].avg_open_coverage == 1.0
    assert rows[0].avg_entry_odds_type == "avg_preclose"
    assert "avg_preclose_rows=2" in rows[0].as_row()
    assert "avg_close_rows=2" in rows[0].as_row()
