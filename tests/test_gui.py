import sqlite3

from scorerecord.db import init_db
from scorerecord.gui import load_dashboard_data, render_dashboard
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
    assert "Pipeline status:" in html
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
