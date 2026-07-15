import sys

def main():
    test_content = """
def test_forecast_today_league_filter() -> None:
    conn = _conn()
    _insert_forecast_fixture(conn)
    
    # Add an EPL fixture
    conn.execute(
        '''
        INSERT INTO fixture (fixture_id, source, source_event_id, league_code, competition, stage, group_name, kickoff_utc, kickoff_local, status, ground, home_id, away_id, home_placeholder, away_placeholder, updated_utc)
        VALUES ('epl-forecast', 'odds_api_io', 'odds-epl-1', 'EPL', 'English Premier League', NULL, NULL, '2026-07-04T19:00:00Z', '2026-07-04T15:00:00', 'SCHEDULED', 'Emirates', 57, 58, NULL, NULL, '2026-07-01T00:00:00Z')
        '''
    )
    # Give it a primary forecast
    conn.execute(
        '''
        INSERT INTO prediction (fixture_id, market, selection, prob, fair_odds, created_utc, run_id)
        VALUES ('epl-forecast', 'OU2.5', 'over', 0.60, 1.66, '2026-07-03T10:00:00Z', 101)
        '''
    )
    
    # Give both fixtures a default competition
    conn.execute("UPDATE fixture SET league_code='WORLD_CUP_2026', competition='FIFA World Cup 2026' WHERE fixture_id='m-forecast'")

    # Test ALL
    data_all = load_forecast_data(
        conn, target_date="2026-07-04", now_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC), league_filter="ALL"
    )
    html_all = render_today_forecast(data_all, settings=Settings(db_path=Path(""), raw_dir=Path("")), refresh_seconds=60)
    assert len(data_all.fixtures) == 2
    assert "FIFA World Cup 2026" in html_all
    assert "English Premier League" in html_all

    # Test EPL
    data_epl = load_forecast_data(
        conn, target_date="2026-07-04", now_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC), league_filter="EPL"
    )
    html_epl = render_today_forecast(data_epl, settings=Settings(db_path=Path(""), raw_dir=Path("")), refresh_seconds=60)
    assert len(data_epl.fixtures) == 1
    assert data_epl.fixtures[0].fixture_id == "epl-forecast"
    assert "Premier League &mdash; READY_FOR_PILOT" in html_epl
    assert "Piloto EPL: cobertura de ratings 90%." in html_epl
    assert "FIFA World Cup 2026" not in html_epl
    assert "VALIDATED_RECOMMENDATION" not in html_epl
    assert "kelly" not in html_epl.lower()
    
    # Test empty state
    data_epl_empty = load_forecast_data(
        conn, target_date="2026-08-01", now_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC), league_filter="EPL"
    )
    html_empty = render_today_forecast(data_epl_empty, settings=Settings(db_path=Path(""), raw_dir=Path("")), refresh_seconds=60)
    assert len(data_epl_empty.fixtures) == 0
    assert "No hay partidos para esta liga en esta fecha." in html_empty
    assert "Premier League &mdash; READY_FOR_PILOT" in html_empty

def test_forecast_match_preserves_league() -> None:
    conn = _conn()
    _insert_forecast_fixture(conn)
    conn.execute("UPDATE fixture SET league_code='EPL', competition='English Premier League' WHERE fixture_id='m-forecast'")
    
    data = load_forecast_data(
        conn, fixture_id="m-forecast", now_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC), league_filter="EPL"
    )
    html = render_match_forecast(data, "m-forecast", settings=Settings(db_path=Path(""), raw_dir=Path("")))
    assert "English Premier League" in html
    assert "league=EPL" in html
"""
    with open("tests/test_gui.py", "a", encoding="utf-8") as f:
        f.write(test_content)

if __name__ == "__main__":
    main()
