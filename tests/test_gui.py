import sqlite3
from datetime import UTC, datetime

from scorerecord.db import init_db
from scorerecord.gui import (
    load_dashboard_data,
    load_forecast_data,
    render_dashboard,
    render_match_forecast,
    render_today_forecast,
)
from scorerecord.models.manifest import MODEL_VERSION


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _run(conn: sqlite3.Connection, job_name: str) -> int:
    cursor = conn.execute(
        "INSERT INTO ingest_run (job_name, status) VALUES (?, 'succeeded')",
        (job_name,),
    )
    return int(cursor.lastrowid)


def _insert_forecast_fixture(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT INTO team (team_id, canonical, canonical_key) VALUES (1, 'Mexico', 'mexico')"
    )
    conn.execute(
        "INSERT INTO team (team_id, canonical, canonical_key) VALUES (2, 'Canada', 'canada')"
    )
    conn.execute(
        """
        INSERT INTO fixture (
          fixture_id, kickoff_local, kickoff_utc, stage, group_name, home_id, away_id, ground
        )
        VALUES (
          'm-forecast', '2026-07-04 14:00 UTC-6', '2026-07-04T20:00:00+00:00',
          'Quarter-finals', NULL, 1, 2, 'Azteca'
        )
        """
    )
    run_id = _run(conn, "predict-fixtures")
    conn.execute(
        """
        INSERT INTO model_artifact (
          artifact_id, model_version, market_validation_status, trained_until,
          training_rows, params_json
        )
        VALUES ('artifact-forecast-1', ?, 'NOT_TESTED', '2026-06-11', 5730, '{}')
        """,
        (MODEL_VERSION,),
    )
    for market, selection, prob in [
        ("OU1.5", "over", 0.74),
        ("OU1.5", "under", 0.26),
        ("OU2.5", "over", 0.49),
        ("OU2.5", "under", 0.51),
        ("OU3.5", "over", 0.22),
        ("OU3.5", "under", 0.78),
        ("BTTS", "yes", 0.53),
        ("BTTS", "no", 0.47),
    ]:
        conn.execute(
            """
            INSERT INTO prediction (
              run_id, fixture_id, artifact_id, model, market, selection, prob,
              fair_odds, generated_utc, model_version
            )
            VALUES (
              ?, 'm-forecast', 'artifact-forecast-1', 'dixon_coles',
              ?, ?, ?, ?, '2026-07-04T12:00:00+00:00', ?
            )
            """,
            (run_id, market, selection, prob, 1 / prob, MODEL_VERSION),
        )
    return run_id


def test_dashboard_uses_latest_post_guardrail_recommendation_run() -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO team (team_id, canonical, canonical_key) VALUES (1, 'Colombia', 'colombia')"
    )
    conn.execute(
        "INSERT INTO team (team_id, canonical, canonical_key) VALUES (2, 'Portugal', 'portugal')"
    )
    conn.execute(
        """
        INSERT INTO fixture (fixture_id, kickoff_local, home_id, away_id)
        VALUES ('m1', '2026-06-18 13:00 UTC-6', 1, 2)
        """
    )
    edge_run_id = _run(conn, "compute-edge")
    recommend_run_id = _run(conn, "recommend")
    conn.execute(
        """
        INSERT INTO edge_analysis (
          fixture_id, run_id, model_version, market, selection, prob_model,
          odds_decimal, raw_implied_prob, prob_market_devig, market_overround,
          edge_ev, edge_prob, bookmaker, captured_utc, generated_utc,
          odds_books, median_odds, odds_ratio_to_median, odds_age_minutes,
          odds_quality_flags, high_edge_reason, recommendation_score
        )
        VALUES (
          'm1', ?, ?, '1X2', 'home', 0.3403, 3.50, 0.2857, 0.2855, 0.055,
          0.1910, 0.0548, 'Bet365 (no latency)',
          '2026-06-10T01:00:00+00:00', '2026-06-10T02:00:00+00:00',
          2, 3.45, 1.0145, 45.0, '', '', 0.1704
        )
        """,
        (edge_run_id, MODEL_VERSION),
    )
    conn.execute(
        """
        INSERT INTO recommendation (
          run_id, fixture_id, market, selection, model_prob, market_odds,
          edge, edge_prob, kelly_fraction, stake_units, confidence, bookmaker,
          model_version, odds_captured_utc, edge_run_id, recommendation_score,
          odds_quality_flags, high_edge_reason, paper, generated_utc
        )
        VALUES (
          ?, 'm1', '1X2', 'home', 0.3403, 3.50, 0.1910, 0.0548,
          0.25, 0.0191, 'high', 'Bet365 (no latency)', ?,
          '2026-06-10T01:00:00+00:00', ?, 0.1704, '', '', 1,
          '2026-06-10T02:05:00+00:00'
        )
        """,
        (recommend_run_id, MODEL_VERSION, edge_run_id),
    )

    legacy_run_id = _run(conn, "recommend")
    conn.execute(
        """
        INSERT INTO recommendation (
          run_id, fixture_id, market, selection, model_prob, market_odds,
          edge, edge_prob, kelly_fraction, stake_units, confidence, bookmaker,
          model_version, odds_captured_utc, paper, generated_utc
        )
        VALUES (
          ?, 'm1', '1X2', 'draw', 0.29, 5.00, 0.45, 0.09,
          0.25, 0.01, 'medium', 'Unibet', ?,
          '2026-06-10T01:00:00+00:00', 1,
          '2026-06-10T02:10:00+00:00'
        )
        """,
        (legacy_run_id, MODEL_VERSION),
    )

    data = load_dashboard_data(conn, selected_run_id=None)
    html = render_dashboard(data, refresh_seconds=60)

    assert data.selected_run_id == recommend_run_id
    assert data.edge_run_id == edge_run_id
    assert len(data.picks) == 1
    assert data.kpis["rows_analyzed"] == 1
    assert data.kpis["candidates"] == 1
    assert data.kpis["recommendations"] == 1
    assert data.legacy_warning_count == 1
    assert "Service status:" in html
    assert "Data quality:" in html
    assert "Actionable Recommendations" in html
    assert "Audit-only / Not Actionable" in html
    assert "Colombia" in html
    assert f"#{legacy_run_id} / edge" not in html


def test_dashboard_filters_by_date_and_market() -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO team (team_id, canonical, canonical_key) VALUES (1, 'Croatia', 'croatia')"
    )
    conn.execute(
        "INSERT INTO team (team_id, canonical, canonical_key) VALUES (2, 'Ghana', 'ghana')"
    )
    conn.execute(
        "INSERT INTO team (team_id, canonical, canonical_key) VALUES (3, 'Algeria', 'algeria')"
    )
    conn.execute(
        "INSERT INTO team (team_id, canonical, canonical_key) VALUES (4, 'Austria', 'austria')"
    )
    conn.execute(
        """
        INSERT INTO fixture (fixture_id, kickoff_local, kickoff_utc, home_id, away_id)
        VALUES ('m1', '2026-06-20 13:00 UTC-6', '2026-06-20T19:00:00+00:00', 1, 2)
        """
    )
    conn.execute(
        """
        INSERT INTO fixture (fixture_id, kickoff_local, kickoff_utc, home_id, away_id)
        VALUES ('m2', '2026-06-21 13:00 UTC-6', '2026-06-21T19:00:00+00:00', 3, 4)
        """
    )
    edge_run_id = _run(conn, "compute-edge")
    recommend_run_id = _run(conn, "recommend")
    for fixture_id, market, selection, score in [
        ("m1", "1X2", "home", 0.12),
        ("m2", "BTTS", "yes", 0.11),
    ]:
        conn.execute(
            """
            INSERT INTO edge_analysis (
              fixture_id, run_id, model_version, market, selection, prob_model,
              odds_decimal, raw_implied_prob, prob_market_devig, market_overround,
              edge_ev, edge_prob, bookmaker, captured_utc, generated_utc,
              odds_books, median_odds, odds_ratio_to_median, odds_age_minutes,
              odds_quality_flags, high_edge_reason, recommendation_score
            )
            VALUES (
              ?, ?, ?, ?, ?, 0.55, 2.00, 0.50, 0.50, 0.04,
              0.10, 0.05, 'Bet365', '2026-06-10T01:00:00+00:00',
              '2026-06-10T02:00:00+00:00', 2, 2.00, 1.00, 30.0, '', '', ?
            )
            """,
            (fixture_id, edge_run_id, MODEL_VERSION, market, selection, score),
        )
        conn.execute(
            """
            INSERT INTO recommendation (
              run_id, fixture_id, market, selection, model_prob, market_odds,
              edge, edge_prob, kelly_fraction, stake_units, confidence, bookmaker,
              model_version, odds_captured_utc, edge_run_id, recommendation_score,
              odds_quality_flags, high_edge_reason, paper, generated_utc
            )
            VALUES (
              ?, ?, ?, ?, 0.55, 2.00, 0.10, 0.05, 0.25, 0.025,
              'high', 'Bet365', ?, '2026-06-10T01:00:00+00:00',
              ?, ?, '', '', 1, '2026-06-10T02:05:00+00:00'
            )
            """,
            (recommend_run_id, fixture_id, market, selection, MODEL_VERSION, edge_run_id, score),
        )

    data = load_dashboard_data(
        conn,
        selected_run_id=None,
        date_filter="2026-06-20",
        market_filter="1X2",
    )
    html = render_dashboard(data, refresh_seconds=60)

    assert len(data.picks) == 1
    assert data.kpis["rows_analyzed"] == 1
    assert "Croatia" in html
    assert "Ghana" in html
    assert "Algeria" not in html
    assert "market=1X2" in html


def test_dashboard_deduplicates_audit_rows_by_logical_pick() -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO team (team_id, canonical, canonical_key) VALUES (1, 'Algeria', 'algeria')"
    )
    conn.execute(
        "INSERT INTO team (team_id, canonical, canonical_key) VALUES (2, 'Austria', 'austria')"
    )
    conn.execute(
        """
        INSERT INTO fixture (fixture_id, kickoff_local, home_id, away_id)
        VALUES ('m3', '2026-06-21 13:00 UTC-6', 1, 2)
        """
    )
    edge_run_id = _run(conn, "compute-edge")
    recommend_run_id = _run(conn, "recommend")
    for bookmaker, edge, score in [
        ("Bet365", 0.3191, 0.1899),
        ("Unibet", 0.3364, 0.2002),
    ]:
        conn.execute(
            """
            INSERT INTO edge_analysis (
              fixture_id, run_id, model_version, market, selection, prob_model,
              odds_decimal, raw_implied_prob, prob_market_devig, market_overround,
              edge_ev, edge_prob, bookmaker, captured_utc, generated_utc,
              odds_books, median_odds, odds_ratio_to_median, odds_age_minutes,
              odds_quality_flags, high_edge_reason, recommendation_score
            )
            VALUES (
              'm3', ?, ?, '1X2', 'home', 0.45, 3.00, 0.33, 0.34, 0.05,
              ?, 0.11, ?, '2026-06-10T01:00:00+00:00',
              '2026-06-10T02:00:00+00:00', 2, 3.00, 1.00, 30.0,
              '', 'model_market_disagreement,long_odds_tail', ?
            )
            """,
            (edge_run_id, MODEL_VERSION, edge, bookmaker, score),
        )
    conn.execute(
        """
        INSERT INTO recommendation (
          run_id, fixture_id, market, selection, model_prob, market_odds,
          edge, edge_prob, kelly_fraction, stake_units, confidence, bookmaker,
          model_version, odds_captured_utc, edge_run_id, recommendation_score,
          odds_quality_flags, high_edge_reason, paper, generated_utc
        )
        VALUES (
          ?, 'm3', '1X2', 'away', 0.40, 2.40, 0.08, 0.04, 0.25,
          0.02, 'medium', 'Bet365', ?, '2026-06-10T01:00:00+00:00',
          ?, 0.08, '', '', 1, '2026-06-10T02:05:00+00:00'
        )
        """,
        (recommend_run_id, MODEL_VERSION, edge_run_id),
    )

    data = load_dashboard_data(conn, selected_run_id=None)
    html = render_dashboard(data, refresh_seconds=60)

    assert len(data.audit_rows) == 1
    assert data.audit_rows[0]["duplicate_rows"] == 2
    assert data.audit_rows[0]["bookmaker"] == "Unibet"
    assert "Raw Rows" in html


def test_forecast_today_reads_predictions_not_recommendations() -> None:
    conn = _conn()
    run_id = _insert_forecast_fixture(conn)

    data = load_forecast_data(
        conn,
        target_date="2026-07-04",
        now_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    html = render_today_forecast(data, refresh_seconds=60)

    assert data.latest_run_id == run_id
    assert data.forecastable_count == 1
    assert data.fixtures[0].primary is not None
    assert data.fixtures[0].primary.market == "OU3.5"
    assert data.fixtures[0].primary.selection == "under"
    assert "FORECAST_ONLY" in html
    assert "No es recomendacion de apuesta" in html
    assert "Mexico" in html
    assert "Canada" in html
    assert "Under 3.5 goles" in html
    assert "Actionable Recommendations" not in html
    assert "No usa EV" in html


def test_forecast_match_renders_goal_market_detail() -> None:
    conn = _conn()
    _insert_forecast_fixture(conn)

    data = load_forecast_data(
        conn,
        fixture_id="m-forecast",
        now_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    html = render_match_forecast(data, "m-forecast")

    assert "Mexico" in html
    assert "Canada" in html
    assert "Cuota justa" in html
    assert "OU2.5" in html
    assert "Ambos anotan: si" in html


def test_forecast_hides_ou_when_monotonicity_is_broken() -> None:
    conn = _conn()
    _insert_forecast_fixture(conn)
    conn.execute(
        """
        UPDATE prediction
        SET prob = 0.80, fair_odds = 1.25
        WHERE fixture_id = 'm-forecast'
          AND market = 'OU2.5'
          AND selection = 'over'
        """
    )

    data = load_forecast_data(
        conn,
        target_date="2026-07-04",
        now_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    html = render_today_forecast(data, refresh_seconds=60)

    assert data.inconsistent_count == 1
    assert data.fixtures[0].primary is None
    assert data.fixtures[0].state_label == "DATOS INCONSISTENTES"
    assert "Over 1.5 goles" not in html
