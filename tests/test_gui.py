import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from scorerecord.db import init_db
from scorerecord.gui import (
    load_market_validation_data,
    load_dashboard_data,
    load_forecast_data,
    render_market_page,
    render_performance_page,
    render_dashboard,
    render_history_page,
    render_health_page,
    render_home_page,
    render_match_forecast,
    render_signals_page,
    render_today_forecast,
)
from scorerecord.forecast_performance import forecast_performance_report
from scorerecord.forecast_snapshots import forecast_snapshot_report
from scorerecord.models.manifest import MODEL_VERSION
from scorerecord.settings import Settings


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
        ("OU2.5", "over", 0.52),
        ("OU2.5", "under", 0.48),
        ("OU3.5", "over", 0.22),
        ("OU3.5", "under", 0.78),
        ("BTTS", "yes", 0.501),
        ("BTTS", "no", 0.499),
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


def _insert_forecast_quality(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    calibration_value: float,
    ou_violations: int,
    missing_elo_slots: int,
) -> None:
    for market in ["OU1.5", "OU2.5", "OU3.5", "BTTS"]:
        conn.execute(
            """
            INSERT INTO data_quality (run_id, check_name, metric_name, metric_value)
            VALUES (?, 'calibrator_sanity', ?, ?)
            """,
            (run_id, f"{market}_mean_calibration_improved", calibration_value),
        )
    conn.execute(
        """
        INSERT INTO data_quality (run_id, check_name, metric_name, metric_value)
        VALUES (?, 'prediction_sanity', 'ou_monotonicity_violations', ?)
        """,
        (run_id, float(ou_violations)),
    )
    conn.execute(
        """
        INSERT INTO data_quality (run_id, check_name, metric_name, metric_value)
        VALUES (?, 'fixture_elo', 'missing_team_rating_slots', ?)
        """,
        (run_id, float(missing_elo_slots)),
    )


def _insert_result_predictions(conn: sqlite3.Connection, *, run_id: int) -> None:
    for market, selection, prob in [
        ("1X2", "home", 0.42),
        ("1X2", "draw", 0.30),
        ("1X2", "away", 0.28),
        ("double_chance", "1X", 0.72),
        ("double_chance", "12", 0.70),
        ("double_chance", "X2", 0.58),
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


def _insert_forecast_insight_inputs(conn: sqlite3.Connection, *, run_id: int) -> None:
    conn.execute(
        """
        UPDATE fixture
        SET league_code='EPL', competition='English Premier League'
        WHERE fixture_id='m-forecast'
        """
    )
    conn.execute(
        """
        UPDATE model_artifact
        SET
          training_rows=1900,
          details_json=?,
          calibration_json=?
        WHERE artifact_id='artifact-forecast-1'
        """,
        (
            '{"training_policy":{"training_scope":"club","training_tournament":"EPL","calibrator_scope":"EPL"}}',
            '{"coefficients":{"OU2.5":{"status":"CALIBRATED","calibrator_scope":"EPL"},"BTTS":{"status":"CALIBRATED","calibrator_scope":"EPL"}}}',
        ),
    )
    conn.execute(
        """
        INSERT INTO team_rating (team_id, rating_system, rating, as_of)
        VALUES (1, 'elo', 1590, '2026-06-01')
        """
    )
    conn.execute(
        """
        INSERT INTO team_rating (team_id, rating_system, rating, as_of)
        VALUES (2, 'elo', 1510, '2026-06-01')
        """
    )
    for idx in range(120):
        conn.execute(
            """
            INSERT INTO match_history (
              source_match_id, date, home_id, away_id, home_score, away_score,
              tournament, neutral
            )
            VALUES (?, ?, 1, 2, ?, ?, 'EPL', 0)
            """,
            (
                f"epl-history-{idx}",
                f"2025-01-{(idx % 28) + 1:02d}",
                2 if idx % 3 else 1,
                1 if idx % 2 else 2,
            ),
        )
    _insert_forecast_quality(
        conn,
        run_id=run_id,
        calibration_value=1,
        ou_violations=0,
        missing_elo_slots=0,
    )


def _insert_odds_snapshot(
    conn: sqlite3.Connection,
    *,
    market: str,
    selection: str,
    odds: float,
    bookmaker: str = "BookA",
    snapshot_at: str = "2026-07-04T18:30:00+00:00",
) -> None:
    line = 2.5 if market == "OU2.5" else None
    conn.execute(
        """
        INSERT INTO odds_snapshots (
          event_id, fixture_id, bookmaker, market, selection, line,
          decimal_odds, snapshot_at, commence_time, source
        )
        VALUES (
          'event-forecast', 'm-forecast', ?, ?, ?, ?, ?,
          ?, '2026-07-04T20:00:00+00:00', 'test'
        )
        """,
        (bookmaker, market, selection, line, odds, snapshot_at),
    )


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
    assert "Legacy Paper Recommendations" in html
    assert "LEGACY AUDIT MODE" in html
    assert "Actionable Recommendations" not in html
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
    html = render_today_forecast(
        data, settings=Settings(db_path=Path(""), raw_dir=Path("")), refresh_seconds=60
    )

    assert data.latest_run_id == run_id
    assert data.forecastable_count == 1
    assert data.fixtures[0].primary is not None
    assert len(data.fixtures[0].primary_markets) == 2
    assert data.fixtures[0].primary.market == "OU2.5"
    assert data.fixtures[0].primary.selection == "over"
    assert "FORECAST_ONLY" in html
    assert "pronosticos deportivos" in html
    assert "Health" in html
    assert "Auditoria tecnica" not in html
    assert "Pronósticos principales del modelo" in html
    assert "Pronóstico principal del modelo" in html
    assert "Pronóstico principal del modelo (BTTS)" in html
    assert 'data-signal="NO_SIGNAL"' in html
    assert html.count('data-signal="NO_SIGNAL"') >= 2
    assert "Sin señal clara" in html
    assert "No es recomendacion de apuesta" in html
    assert "Mexico" in html
    assert "Canada" in html
    assert "OU2.5: Over 52.00%" in html
    assert "BTTS: Si 50.10%" in html
    assert "Actionable Recommendations" not in html
    assert "Solo pronostico y cobertura informativa" in html
    assert "match-modal" in html
    assert "data-detail-url" in html


def test_home_page_is_landing_without_top_nav() -> None:
    conn = _conn()
    run_id = _insert_forecast_fixture(conn)

    data = load_forecast_data(
        conn,
        target_date="2026-07-04",
        now_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    html = render_home_page(
        data, settings=Settings(db_path=Path(""), raw_dir=Path("")), refresh_seconds=60
    )

    assert data.latest_run_id == run_id
    assert 'class="forecast-nav"' not in html
    assert 'class="home-hero landing-hero"' in html
    assert "ScoreRecord" in html
    assert "pronosticos deportivos" in html
    assert "github.com/Perdomazo/ScoreRecord" in html
    assert "Ver partidos" in html
    assert "Liga MX" in html
    assert "MLS" in html
    assert "Premier League" in html
    assert "Argentina" in html
    assert "Brasileirao" in html
    assert "Health" in html
    assert "Historial" in html
    assert "FORECAST_ONLY" not in html
    assert "Proximos partidos cargados" not in html
    assert "FORECAST_ONLY</strong>" not in html
    assert "Auditoria tecnica" not in html


def test_forecast_date_toolbar_links_loaded_future_dates() -> None:
    conn = _conn()
    _insert_forecast_fixture(conn)
    conn.execute(
        """
        INSERT INTO fixture (
          fixture_id, kickoff_local, kickoff_utc, stage, home_id, away_id, ground
        )
        VALUES (
          'm-next', '2026-07-05 18:00 UTC-6', '2026-07-06T00:00:00+00:00',
          'Semi-finals', 1, 2, 'Azteca'
        )
        """
    )

    data = load_forecast_data(
        conn,
        target_date="2026-07-04",
        now_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    html = render_today_forecast(
        data, settings=Settings(db_path=Path(""), raw_dir=Path("")), refresh_seconds=60
    )

    assert data.upcoming_fixtures[0].display_date == "2026-07-05"
    assert "date-chip" in html
    assert "Proxima fecha con partido" in html
    assert "05 Jul" in html


def test_forecast_renders_quality_score_explanations_and_evidence() -> None:
    conn = _conn()
    run_id = _insert_forecast_fixture(conn)
    _insert_forecast_insight_inputs(conn, run_id=run_id)

    data = load_forecast_data(
        conn,
        target_date="2026-07-04",
        now_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
        league_filter="EPL",
    )
    html = render_today_forecast(
        data, settings=Settings(db_path=Path(""), raw_dir=Path("")), refresh_seconds=60
    )
    detail_html = render_match_forecast(
        data, "m-forecast", settings=Settings(db_path=Path(""), raw_dir=Path(""))
    )

    assert "Calidad de datos del pronostico" in html
    assert "Rango orientativo" in html
    assert "Promedio historico combinado" in html
    assert "Health operativo" not in html
    assert "Provider fixtures" not in html
    assert "Factores principales" in detail_html
    assert "Evidencia del modelo" in detail_html
    assert "Training rows" in detail_html
    assert "1900" in detail_html
    assert "Calibrador" in detail_html
    assert "EPL (CALIBRATED)" in detail_html
    assert "Prediction run" in detail_html


def test_forecast_explains_missing_prediction_rows() -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO team (team_id, canonical, canonical_key) VALUES (1, 'Puebla', 'puebla')"
    )
    conn.execute(
        "INSERT INTO team (team_id, canonical, canonical_key) VALUES (2, 'Chivas', 'chivas')"
    )
    conn.execute(
        """
        INSERT INTO fixture (
          fixture_id, league_code, competition, kickoff_utc, home_id, away_id
        )
        VALUES (
          'missing-prediction', 'LIGA_MX', 'Liga MX',
          '2026-08-01T01:00:00+00:00', 1, 2
        )
        """
    )

    data = load_forecast_data(
        conn,
        target_date="2026-07-31",
        now_utc=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
        view_mode="all",
        league_filter="LIGA_MX",
    )
    html = render_today_forecast(
        data, settings=Settings(db_path=Path(""), raw_dir=Path("")), refresh_seconds=60
    )

    assert "No se genero pronostico" in html
    assert "sin filas de prediction para este fixture" in html


def test_forecast_signal_strength_marks_ou25_moderate() -> None:
    conn = _conn()
    _insert_forecast_fixture(conn)
    conn.execute(
        """
        UPDATE prediction
        SET prob = CASE selection WHEN 'over' THEN 0.59 ELSE 0.41 END,
            fair_odds = CASE selection WHEN 'over' THEN 1.0 / 0.59 ELSE 1.0 / 0.41 END
        WHERE fixture_id = 'm-forecast'
          AND market = 'OU2.5'
        """
    )

    data = load_forecast_data(
        conn,
        target_date="2026-07-04",
        now_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    html = render_today_forecast(
        data, settings=Settings(db_path=Path(""), raw_dir=Path("")), refresh_seconds=60
    )

    assert data.fixtures[0].primary.market == "OU2.5"
    assert data.fixtures[0].primary.selection == "over"
    assert "OU2.5: Over 59.00%" in html
    assert 'data-signal="MODERATE"' in html
    assert "Señal moderada" in html


def test_forecast_quality_panel_renders_ok_and_warn() -> None:
    conn = _conn()
    run_id = _insert_forecast_fixture(conn)
    _insert_forecast_quality(
        conn,
        run_id=run_id,
        calibration_value=1.0,
        ou_violations=0,
        missing_elo_slots=0,
    )

    ok_data = load_forecast_data(
        conn,
        target_date="2026-07-04",
        now_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    today_html = render_today_forecast(
        ok_data, settings=Settings(db_path=Path(""), raw_dir=Path("")), refresh_seconds=60
    )
    ok_html = render_health_page(
        ok_data, settings=Settings(db_path=Path(""), raw_dir=Path("")), refresh_seconds=60
    )

    assert "Model quality panel" not in today_html
    assert "Model quality panel" in ok_html
    assert "Health operativo" in ok_html
    assert "Calibration" in ok_html
    assert "OU monotonicity" in ok_html
    assert "Elo coverage" in ok_html
    assert ok_html.count("<strong>OK</strong>") >= 3
    assert "Market validation" in ok_html
    assert "NOT_TESTED" in ok_html

    _insert_forecast_quality(
        conn,
        run_id=run_id,
        calibration_value=0.0,
        ou_violations=2,
        missing_elo_slots=1,
    )

    warn_data = load_forecast_data(
        conn,
        target_date="2026-07-04",
        now_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    warn_html = render_health_page(
        warn_data, settings=Settings(db_path=Path(""), raw_dir=Path("")), refresh_seconds=60
    )

    assert warn_html.count("<strong>WARN</strong>") >= 3
    assert "0/4 checks" in warn_html
    assert "2 violations" in warn_html
    assert "1 missing rating slots" in warn_html


def test_forecast_quality_panel_ignores_unrelated_later_quality_rows() -> None:
    conn = _conn()
    run_id = _insert_forecast_fixture(conn)
    _insert_forecast_quality(
        conn,
        run_id=run_id,
        calibration_value=1.0,
        ou_violations=0,
        missing_elo_slots=0,
    )
    unrelated_run_id = _run(conn, "predict-fixtures")
    _insert_forecast_quality(
        conn,
        run_id=unrelated_run_id,
        calibration_value=0.0,
        ou_violations=3,
        missing_elo_slots=4,
    )

    data = load_forecast_data(
        conn,
        target_date="2026-07-04",
        now_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    html = render_health_page(
        data, settings=Settings(db_path=Path(""), raw_dir=Path("")), refresh_seconds=60
    )

    assert html.count("<strong>OK</strong>") >= 3
    assert "3 violations" not in html
    assert "4 missing rating slots" not in html


def test_forecast_selector_ignores_uncalibrated_1x2_rows() -> None:
    conn = _conn()
    run_id = _insert_forecast_fixture(conn)
    conn.execute(
        """
        INSERT INTO prediction (
          run_id, fixture_id, artifact_id, model, market, selection, prob,
          fair_odds, generated_utc, model_version
        )
        VALUES (
          ?, 'm-forecast', 'artifact-forecast-1', 'dixon_coles',
          '1X2', 'home', 0.99, 1.01, '2026-07-04T12:00:00+00:00', ?
        )
        """,
        (run_id, MODEL_VERSION),
    )

    data = load_forecast_data(
        conn,
        target_date="2026-07-04",
        now_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    html = render_today_forecast(
        data, settings=Settings(db_path=Path(""), raw_dir=Path("")), refresh_seconds=60
    )

    assert all(market.market != "1X2" for market in data.fixtures[0].markets)
    assert all(market.market != "1X2" for market in data.fixtures[0].primary_markets)
    assert "Selector: solo OU/BTTS calibrados" in html
    assert "1X2" not in html


def test_forecast_card_renders_secondary_result_estimate() -> None:
    conn = _conn()
    run_id = _insert_forecast_fixture(conn)
    _insert_result_predictions(conn, run_id=run_id)

    data = load_forecast_data(
        conn,
        target_date="2026-07-04",
        now_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    html = render_today_forecast(
        data, settings=Settings(db_path=Path(""), raw_dir=Path("")), refresh_seconds=60
    )

    fixture = data.fixtures[0]
    assert all(market.market != "1X2" for market in fixture.markets)
    assert all(market.market != "1X2" for market in fixture.primary_markets)
    assert any(market.market == "1X2" for market in fixture.result_markets)
    assert "Resultado estimado" in html
    assert "Gana Mexico" in html
    assert "Mexico o empate" in html
    assert "Kelly" not in html
    assert "stake" not in html.lower()


def test_forecast_match_renders_goal_market_detail() -> None:
    conn = _conn()
    _insert_forecast_fixture(conn)

    data = load_forecast_data(
        conn,
        fixture_id="m-forecast",
        now_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    html = render_match_forecast(
        data, "m-forecast", settings=Settings(db_path=Path(""), raw_dir=Path(""))
    )

    assert "Mexico" in html
    assert "Canada" in html
    assert "Precio teorico del modelo" in html
    assert "Fuerza de señal" in html
    assert "90 min / tiempo reglamentario" in html
    assert "No es una cuota real" in html
    assert "OU2.5" in html
    assert "Ambos anotan: si" in html


def test_forecast_match_renders_result_and_double_chance_detail() -> None:
    conn = _conn()
    run_id = _insert_forecast_fixture(conn)
    _insert_result_predictions(conn, run_id=run_id)

    data = load_forecast_data(
        conn,
        fixture_id="m-forecast",
        now_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    html = render_match_forecast(
        data, "m-forecast", settings=Settings(db_path=Path(""), raw_dir=Path(""))
    )

    assert "Resultado estimado y doble oportunidad" in html
    assert "Gana Mexico" in html
    assert "Empate" in html
    assert "Canada o empate" in html
    assert "calibracion 1X2 por liga esta en revision" in html


def test_forecast_match_partial_is_modal_content_only() -> None:
    conn = _conn()
    _insert_forecast_fixture(conn)

    data = load_forecast_data(
        conn,
        fixture_id="m-forecast",
        now_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    html = render_match_forecast(
        data,
        "m-forecast",
        settings=Settings(db_path=Path(""), raw_dir=Path("")),
        partial=True,
    )

    assert "<!doctype html>" not in html
    assert "match-detail" in html
    assert "Mexico" in html
    assert "Volver" not in html


def test_forecast_match_renders_odds_coverage_without_1x2() -> None:
    conn = _conn()
    _insert_forecast_fixture(conn)
    _insert_odds_snapshot(conn, market="OU2.5", selection="over", odds=1.92, bookmaker="BookA")
    _insert_odds_snapshot(conn, market="OU2.5", selection="under", odds=1.88, bookmaker="BookA")
    _insert_odds_snapshot(conn, market="OU2.5", selection="over", odds=1.95, bookmaker="BookB")
    _insert_odds_snapshot(conn, market="OU2.5", selection="under", odds=1.90, bookmaker="BookB")
    _insert_odds_snapshot(conn, market="BTTS", selection="yes", odds=1.80, bookmaker="BookA")
    _insert_odds_snapshot(conn, market="1X2", selection="home", odds=2.40, bookmaker="BookA")

    data = load_forecast_data(
        conn,
        fixture_id="m-forecast",
        now_utc=datetime(2026, 7, 4, 19, 0, tzinfo=UTC),
    )
    html = render_match_forecast(
        data, "m-forecast", settings=Settings(db_path=Path(""), raw_dir=Path(""))
    )

    assert "Cobertura de cuotas" in html
    assert "Cobertura por horizontes" in html
    assert "OU2.5" in html
    assert "COVERAGE_OK" in html
    assert "BTTS" in html
    assert "INCOMPLETE_MARKET" in html
    assert "T24H" in html
    assert "T6H" in html
    assert "T1H" in html
    assert "T15M" in html
    assert "LAST_PRE_KICKOFF" in html
    assert "2 casas completas" in html
    assert "Over 2.5" in html
    assert "1.950" in html
    assert "Under 2.5" in html
    assert "1.900" in html
    assert "1X2" not in html
    assert "Kelly" not in html
    assert "stake" not in html
    assert "PAPER_CANDIDATE" not in html
    assert "VALIDATED_RECOMMENDATION" not in html


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
    html = render_today_forecast(
        data, settings=Settings(db_path=Path(""), raw_dir=Path("")), refresh_seconds=60
    )

    assert data.inconsistent_count == 1
    assert data.fixtures[0].primary is None
    assert data.fixtures[0].state_label == "DATOS INCONSISTENTES"
    assert "Over 1.5 goles" not in html

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
        INSERT INTO prediction (
          run_id, fixture_id, artifact_id, model, market, selection, prob,
          fair_odds, generated_utc, model_version
        )
        VALUES (
          101, 'epl-forecast', 'artifact-forecast-1', 'dixon_coles',
          'OU2.5', 'over', 0.60, 1.66, '2026-07-03T10:00:00Z', '0.1.0'
        )
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
    assert "Premier League &mdash; FORECAST_ONLY" in html_epl
    assert "Mundial 2026" not in html_epl
    assert "Liga MX" in html_epl
    assert "MLS" in html_epl
    assert "Paper recreativo" not in html_epl
    assert "view=paper" not in html_epl
    assert "view=research" not in html_epl
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
    assert "Premier League &mdash; FORECAST_ONLY" in html_empty


def test_forecast_all_uses_latest_prediction_per_fixture() -> None:
    conn = _conn()
    wc_run_id = _insert_forecast_fixture(conn)
    conn.execute(
        """
        UPDATE fixture
        SET league_code='WORLD_CUP_2026', competition='FIFA World Cup 2026'
        WHERE fixture_id='m-forecast'
        """
    )
    conn.execute(
        "INSERT INTO team (team_id, canonical, canonical_key) VALUES (57, 'Arsenal', 'arsenal')"
    )
    conn.execute(
        "INSERT INTO team (team_id, canonical, canonical_key) VALUES (58, 'Chelsea', 'chelsea')"
    )
    conn.execute(
        """
        INSERT INTO fixture (
          fixture_id, source, source_event_id, league_code, competition,
          kickoff_utc, kickoff_local, status, ground, home_id, away_id
        )
        VALUES (
          'epl-forecast', 'odds_api_io', 'odds-epl-1', 'EPL',
          'English Premier League', '2026-07-04T19:00:00Z',
          '2026-07-04T15:00:00', 'SCHEDULED', 'Emirates', 57, 58
        )
        """
    )
    epl_run_id = _run(conn, "predict-fixtures")
    conn.execute(
        """
        INSERT INTO model_artifact (
          artifact_id, model_version, market_validation_status, trained_until,
          training_rows, params_json, details_json
        )
        VALUES (
          'artifact-epl', ?, 'NOT_TESTED', '2026-06-11', 1900, '{}',
          '{"training_policy":{"league_code":"EPL","training_scope":"club"}}'
        )
        """,
        (MODEL_VERSION,),
    )
    for market, selection, prob in [
        ("OU1.5", "over", 0.80),
        ("OU1.5", "under", 0.20),
        ("OU2.5", "over", 0.59),
        ("OU2.5", "under", 0.41),
        ("OU3.5", "over", 0.35),
        ("OU3.5", "under", 0.65),
        ("BTTS", "yes", 0.57),
        ("BTTS", "no", 0.43),
    ]:
        conn.execute(
            """
            INSERT INTO prediction (
              run_id, fixture_id, artifact_id, model, market, selection, prob,
              fair_odds, generated_utc, model_version
            )
            VALUES (
              ?, 'epl-forecast', 'artifact-epl', 'dixon_coles',
              ?, ?, ?, ?, '2026-07-04T12:30:00+00:00', ?
            )
            """,
            (epl_run_id, market, selection, prob, 1 / prob, MODEL_VERSION),
        )

    data = load_forecast_data(
        conn,
        target_date="2026-07-04",
        now_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
        league_filter="ALL",
    )

    by_fixture = {fixture.fixture_id: fixture for fixture in data.fixtures}
    assert data.latest_run_id == epl_run_id
    assert by_fixture["m-forecast"].markets
    assert by_fixture["epl-forecast"].markets
    assert by_fixture["m-forecast"].markets[0].artifact_id == "artifact-forecast-1"
    assert by_fixture["epl-forecast"].markets[0].artifact_id == "artifact-epl"
    assert wc_run_id != epl_run_id


def test_forecast_renders_upcoming_fixture_links() -> None:
    conn = _conn()
    _insert_forecast_fixture(conn)
    conn.execute(
        """
        UPDATE fixture
        SET league_code='LIGA_MX', competition='Liga MX',
            kickoff_utc='2026-07-28T02:00:00Z',
            kickoff_local='2026-07-27T20:00:00'
        WHERE fixture_id='m-forecast'
        """
    )

    data = load_forecast_data(
        conn,
        target_date="2026-07-26",
        now_utc=datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
        league_filter="ALL",
    )
    html = render_today_forecast(
        data,
        settings=Settings(db_path=Path(""), raw_dir=Path("")),
        refresh_seconds=60,
    )
    history_html = render_history_page(
        data,
        active_tab="upcoming",
        settings=Settings(db_path=Path(""), raw_dir=Path("")),
        refresh_seconds=60,
    )

    assert "Proximos partidos cargados" not in html
    assert "Todos los próximos partidos cargados" in history_html
    assert "2026-07-27" in history_html
    assert "Liga MX" in history_html
    assert "Mundial 2026" not in html


def test_history_page_renders_past_fixtures() -> None:
    conn = _conn()
    _insert_forecast_fixture(conn)
    conn.execute(
        """
        UPDATE fixture
        SET league_code='EPL', competition='English Premier League',
            kickoff_utc='2026-07-20T18:00:00Z',
            kickoff_local='2026-07-20T12:00:00',
            home_score=2,
            away_score=1
        WHERE fixture_id='m-forecast'
        """
    )

    data = load_forecast_data(
        conn,
        target_date="2026-08-02",
        now_utc=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
        league_filter="EPL",
        include_past_fixtures=True,
    )
    html = render_history_page(
        data,
        active_tab="past",
        settings=Settings(db_path=Path(""), raw_dir=Path("")),
        refresh_seconds=60,
    )

    assert "Todos los partidos pasados" in html
    assert "Mexico vs Canada" in html
    assert "2-1" in html
    assert "Con predicción" in html


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


def test_history_page_renders_historical_matches_without_fixture_scores() -> None:
    conn = _conn()
    conn.execute(
        """
        INSERT INTO historical_match (
          source, source_event_id, league_code, season, match_date,
          home_team, away_team, home_goals, away_goals, result_1x2
        )
        VALUES (
          'football-data', 'mex-2026-08-01-pumas-america', 'MEX', '2026-2027',
          '2026-08-01', 'Pumas', 'America', 1, 0, 'H'
        )
        """
    )

    data = load_forecast_data(
        conn,
        target_date="2026-08-02",
        now_utc=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
        league_filter="LIGA_MX",
        include_past_fixtures=True,
    )
    html = render_history_page(
        data,
        active_tab="past",
        settings=Settings(db_path=Path(""), raw_dir=Path("")),
        refresh_seconds=60,
    )

    assert "Pumas vs America" in html
    assert "1-0" in html
    assert "Histórico cargado" in html
    assert "/match/None" not in html


def test_performance_page_reports_settled_prediction_results() -> None:
    conn = _conn()
    prediction_run = _insert_forecast_fixture(conn)
    settlement_run = _run(conn, "settle-fixtures")
    for market, selection, prob, label in [
        ("BTTS", "yes", 0.62, 1),
        ("BTTS", "no", 0.38, 0),
        ("OU2.5", "over", 0.44, 0),
        ("OU2.5", "under", 0.56, 1),
    ]:
        conn.execute(
            """
            INSERT INTO prediction_result (
              settlement_run_id, prediction_run_id, fixture_id, model_version,
              market, selection, predicted_prob, label, brier, logloss,
              settled_utc, prediction_generated_utc
            )
            VALUES (
              ?, ?, 'm-forecast', ?, ?, ?, ?, ?, 0.10, 0.50,
              '2026-07-05T00:30:00+00:00', '2026-07-04T12:00:00+00:00'
            )
            """,
            (settlement_run, prediction_run, MODEL_VERSION, market, selection, prob, label),
        )

    data = load_forecast_data(
        conn,
        target_date="2026-07-04",
        now_utc=datetime(2026, 7, 5, 12, 0, tzinfo=UTC),
        upcoming_limit=0,
    )
    report = forecast_performance_report(conn)
    html = render_performance_page(
        data,
        report,
        forecast_snapshot_report(conn),
        settings=Settings(db_path=Path(""), raw_dir=Path("")),
        refresh_seconds=60,
    )

    assert report.decisions == 2
    assert report.prediction_rows == 4
    assert "Precisión del modelo" in html
    assert "BTTS" in html
    assert "OU2.5" in html
    assert "100.00%" in html


def test_market_page_explains_rejected_market_validation() -> None:
    conn = _conn()
    _insert_forecast_fixture(conn)
    conn.execute(
        """
        UPDATE model_artifact
        SET market_validation_status='REJECTED_FOR_INDEPENDENT_VALUE'
        WHERE artifact_id='artifact-forecast-1'
        """
    )
    run_id = _run(conn, "market-benchmark-report")
    conn.execute(
        """
        INSERT INTO data_quality (run_id, check_name, metric_name, metric_value)
        VALUES (?, 'market_benchmark', 'model_minus_market_logloss', 0.009)
        """,
        (run_id,),
    )

    data = load_forecast_data(
        conn,
        target_date="2026-07-04",
        now_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
        upcoming_limit=0,
    )
    market_data = load_market_validation_data(conn)
    html = render_market_page(
        data,
        market_data,
        settings=Settings(db_path=Path(""), raw_dir=Path("")),
        refresh_seconds=60,
    )

    assert market_data.summary_status == "REJECTED"
    assert "Qué significa perder contra el mercado" in html
    assert "REJECTED_FOR_INDEPENDENT_VALUE" in html
    assert "model_minus_market_logloss" in html
    assert "Kelly" not in html


def test_signals_page_renders_top_three_forecasts() -> None:
    conn = _conn()
    _insert_forecast_fixture(conn)
    conn.execute(
        """
        UPDATE prediction
        SET prob = CASE selection WHEN 'over' THEN 0.64 ELSE 0.36 END,
            fair_odds = CASE selection WHEN 'over' THEN 1.0 / 0.64 ELSE 1.0 / 0.36 END
        WHERE fixture_id = 'm-forecast'
          AND market = 'OU2.5'
        """
    )

    data = load_forecast_data(
        conn,
        target_date="2026-07-04",
        now_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    html = render_signals_page(
        data,
        settings=Settings(db_path=Path(""), raw_dir=Path("")),
        refresh_seconds=60,
    )

    assert "Mejores señales del día" in html
    assert "Mexico vs Canada" in html
    assert "OU2.5: Over 64.00%" in html
    assert "NO VALIDADO CONTRA MERCADO" not in html
