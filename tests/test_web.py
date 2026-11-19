"""Tests for the read-only web dashboard (scorerecord.web)."""

from __future__ import annotations

import gzip
import json
import math
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from scorerecord.db import init_db
from scorerecord.predict.fixtures import score_grid_summary
from scorerecord.web import data
from scorerecord.web.app import WebApp

# Thursday 2026-11-19 18:00 UTC = 12:00 in Mexico City.
NOW = datetime(2026, 11, 19, 18, 0, tzinfo=UTC)
UPCOMING_KICKOFF = "2026-11-21T20:00:00Z"  # Saturday, two days later
FINISHED_KICKOFF = "2026-11-16T20:00:00Z"
FORBIDDEN = ("kelly", "stake", "value bet", "apuesta segura", " edge", " ev ")


def _grid() -> str:
    rows = [[0.0] * 7 for _ in range(7)]
    rows[1][1], rows[1][0], rows[0][0], rows[2][1] = 0.12, 0.10, 0.08, 0.07
    return json.dumps(rows)


def _odds(conn: sqlite3.Connection, fixture_id: str, market: str, prices, taken: str) -> None:
    for selection, price in prices:
        conn.execute(
            """
            INSERT INTO odds_snapshots (event_id, fixture_id, bookmaker, market, selection,
              decimal_odds, snapshot_at, source)
            VALUES (?, ?, 'Unibet', ?, ?, ?, ?, 'test')
            """,
            (fixture_id, fixture_id, market, selection, price, taken),
        )


def _seed(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    for team_id, name in ((1, "Arsenal"), (2, "Chelsea"), (3, "Fulham")):
        conn.execute(
            "INSERT INTO team (team_id, canonical, canonical_key) VALUES (?, ?, ?)",
            (team_id, name, name.lower()),
        )
    # League history for base rates: 40 matches, one a week before the fixtures.
    for index in range(40):
        day = (date(2026, 11, 10) - timedelta(days=7 * index)).isoformat()
        home_goals, away_goals = (2, 1) if index % 2 else (0, 0)
        match_id = f"football-data:EPL:{index}"
        conn.execute(
            """
            INSERT INTO match_history (source_match_id, date, home_id, away_id,
              home_score, away_score, tournament, neutral)
            VALUES (?, ?, 1, 2, ?, ?, 'EPL', 0)
            """,
            (match_id, day, home_goals, away_goals),
        )
        for team_id, opponent, is_home in ((1, 2, 1), (2, 1, 0)):
            conn.execute(
                """
                INSERT INTO team_rating_history (team_id, opponent_id, source_match_id,
                  rating_system, rating_pre, rating_post, as_of, is_home,
                  expected_result, actual_result)
                VALUES (?, ?, ?, 'elo', 1500, ?, ?, ?, 0.5, 0.5)
                """,
                (team_id, opponent, match_id, 1500 + index, day, is_home),
            )
    run_id = int(
        conn.execute(
            "INSERT INTO ingest_run (job_name, status, source) "
            "VALUES ('predict-fixtures', 'succeeded', 'test')"
        ).lastrowid
    )
    conn.execute(
        "INSERT INTO model_artifact (artifact_id, model_version, market_validation_status, "
        "trained_until, training_rows, params_json) "
        "VALUES ('art-1', 'dc-test', 'NOT_TESTED', '2026-11-12', 1234, '{}')"
    )
    fixtures = (
        ("up-1", UPCOMING_KICKOFF, 1, 3, None, None),
        ("fin-1", FINISHED_KICKOFF, 2, 3, 1, 1),
    )
    rows = (
        ("1X2", "home", 0.50),
        ("1X2", "draw", 0.27),
        ("1X2", "away", 0.23),
        ("OU1.5", "over", 0.74),
        ("OU2.5", "over", 0.58),
        ("OU2.5", "under", 0.42),
        ("OU3.5", "over", 0.30),
        ("BTTS", "yes", 0.55),
        ("BTTS", "no", 0.45),
    )
    for fixture_id, kickoff, home, away, home_score, away_score in fixtures:
        conn.execute(
            """
            INSERT INTO fixture (fixture_id, kickoff_utc, home_id, away_id, home_score,
              away_score, league_code, competition, source, source_event_id, status)
            VALUES (?, ?, ?, ?, ?, ?, 'EPL', 'Premier', 'odds_api_io', ?, ?)
            """,
            (
                fixture_id,
                kickoff,
                home,
                away,
                home_score,
                away_score,
                fixture_id,
                "finished" if home_score is not None else "scheduled",
            ),
        )
        for market, selection, prob in rows:
            conn.execute(
                """
                INSERT INTO prediction (run_id, fixture_id, artifact_id, model, market,
                  selection, prob, fair_odds, generated_utc, model_version)
                VALUES (?, ?, 'art-1', 'dixon_coles', ?, ?, ?, ?,
                        '2026-11-15T11:00:00+00:00', 'dc-test')
                """,
                (run_id, fixture_id, market, selection, prob, 1 / prob),
            )
        conn.execute(
            "INSERT INTO prediction_score_grid VALUES (?, ?, 1.6, 1.1, 6, ?)",
            (run_id, fixture_id, _grid()),
        )
    taken = "2026-11-19T12:00:00Z"
    _odds(conn, "up-1", "1X2", (("home", 2.0), ("draw", 3.5), ("away", 4.0)), taken)
    _odds(conn, "up-1", "BTTS", (("yes", 1.8), ("no", 2.0)), taken)
    # A full set taken after kickoff must be ignored.
    after = "2026-11-16T21:00:00Z"
    _odds(conn, "fin-1", "1X2", (("home", 1.1), ("draw", 8.0), ("away", 15.0)), after)
    conn.commit()
    conn.close()


@pytest.fixture
def app(tmp_path: Path) -> WebApp:
    db_path = tmp_path / "scorerecord.db"
    _seed(db_path)
    return WebApp(db_path, clock=lambda: NOW)


def _get(app: WebApp, path: str, **headers: str):
    response = app.handle("GET", path, headers)
    return response, response.body.decode("utf-8", "replace")


# ------------------------------------------------------------ pure helpers


def test_devig_and_log_pool_are_proper_distributions() -> None:
    fair = data.devig({"home": 2.0, "draw": 3.5, "away": 4.0})
    assert math.isclose(sum(fair.values()), 1.0)
    assert fair["home"] > fair["draw"] > fair["away"]
    assert data.devig({"yes": 1.0, "no": 3.0}) is None
    pooled = data.log_pool({"a": 0.6, "b": 0.4}, {"a": 0.4, "b": 0.6}, 0.5)
    assert math.isclose(pooled["a"], 0.5)
    assert math.isclose(sum(pooled.values()), 1.0)


def test_base_rates_exclude_the_reference_day_and_need_a_sample(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite"
    _seed(db_path)
    conn = data.open_readonly(db_path)
    rates = data.BaseRates(conn, ("EPL",))
    assert rates.at("EPL", date(2026, 11, 10))["n"] == 39
    assert rates.at("EPL", date(2026, 11, 11))["n"] == 40
    assert rates.at("EPL", date(2026, 3, 1)) is None  # fewer than 30 prior matches
    assert rates.at("LIGA_MX", date(2026, 11, 11)) is None
    conn.close()


def test_score_grid_summary_expected_goals() -> None:
    grid = [[0.0, 0.0], [0.5, 0.5]]  # home always scores 1, away 0 or 1
    summary = score_grid_summary(grid, max_goals=6)
    assert math.isclose(summary["xg_home"], 1.0)
    assert math.isclose(summary["xg_away"], 0.5)
    assert summary["max_goals"] == 1


# ------------------------------------------------------------------ pages


def test_home_opens_next_day_with_matches_and_blends_market(app: WebApp) -> None:
    response, html = _get(app, "/partidos")
    assert response.status == 200
    assert "<h1>" in html and "Sábado 21 de noviembre" in html
    assert "Arsenal" in html and "Fulham" in html
    assert "modelo + mercado" in html
    assert "Chelsea" not in html  # the finished match is on another day
    assert 'role="img"' in html


def test_home_for_a_day_without_matches_is_friendly(app: WebApp) -> None:
    _, html = _get(app, "/partidos?fecha=2026-11-25")
    assert "No hay partidos este día" in html
    assert "fixture-summary" not in html


def test_match_page_shows_all_sections(app: WebApp) -> None:
    response, html = _get(app, "/partido/up-1")
    assert response.status == 200
    for text in (
        "Lectura",
        "Combinado",
        "Mercado",
        "Promedio liga",
        "goles esperados",
        "heatmap",
        "Últimos partidos de liga",
        "Elo",
        "Acerca de este pronóstico",
        "Unibet",
        "1234",
    ):
        assert text in html, text
    missing, _ = _get(app, "/partido/nope")
    assert missing.status == 404


def test_results_page_scores_the_pre_kickoff_forecast(app: WebApp) -> None:
    _, html = _get(app, "/resultados?dias=7")
    assert "Chelsea" in html and "1 – 1" in html
    # Model said home 50% (miss), over 2.5 at 58% (miss), both score at 55% (hit).
    assert html.count("verdict is-miss") == 2
    assert html.count("verdict is-hit") == 1


def test_performance_page_and_api(app: WebApp) -> None:
    response, html = _get(app, "/desempeno")
    assert response.status == 200
    assert "Muestra pequeña" in html and "Calibración" in html and "<svg" in html
    _, body = _get(app, "/api/desempeno")
    payload = json.loads(body)
    assert payload["matches"] == 1
    assert payload["by_market"]["1X2"]["n"] == 1
    assert payload["by_market"]["1X2"]["n_with_market"] == 0  # post-kickoff odds ignored


def test_status_page_lists_checks(app: WebApp) -> None:
    _, html = _get(app, "/estado")
    assert "Resultados al día" in html and "Últimas corridas" in html


def test_api_match_blend_is_between_model_and_market(app: WebApp) -> None:
    response, body = _get(app, "/api/partido/up-1")
    payload = json.loads(body)
    assert response.headers["Content-Type"] == "application/json"
    blend, model, market = payload["blend_1x2"], payload["model"], payload["market"]
    assert math.isclose(sum(blend.values()), 1.0)
    low, high = sorted((model["home"], market["home"]))
    assert low <= blend["home"] <= high
    assert payload["market_weight"] == data.MARKET_WEIGHT
    assert payload["expected_goals"] == [1.6, 1.1]
    assert payload["pick"]["key"] == "home"
    _, listing = _get(app, "/api/partidos?fecha=2026-11-21")
    assert [m["fixture_id"] for m in json.loads(listing)["matches"]] == ["up-1"]


def test_pages_avoid_betting_vocabulary(app: WebApp) -> None:
    for path in ("/", "/partidos", "/partido/up-1", "/resultados", "/desempeno"):
        _, html = _get(app, path)
        lowered = html.lower()
        for word in FORBIDDEN:
            assert word not in lowered, (path, word)


# ------------------------------------------------------------------- http


def test_legacy_urls_redirect(app: WebApp) -> None:
    for old, new in (
        ("/today", "/partidos"),
        ("/health", "/estado"),
        ("/performance", "/desempeno"),
        ("/history", "/resultados"),
        ("/match/up-1", "/partido/up-1"),
    ):
        response = app.handle("GET", old, {})
        assert response.status == 301 and response.headers["Location"] == new
    assert app.handle("GET", "/audit", {}).status == 404


def test_etag_and_gzip(app: WebApp) -> None:
    first = app.handle("GET", "/partidos", {"Accept-Encoding": "gzip"})
    assert first.headers["Content-Encoding"] == "gzip"
    assert b"Arsenal" in gzip.decompress(first.body)
    again = app.handle("GET", "/partidos", {"If-None-Match": first.headers["ETag"]})
    assert again.status == 304 and again.body == b""


def test_static_files_and_traversal(app: WebApp) -> None:
    css = app.handle("GET", "/static/app.css", {})
    assert css.status == 200 and "immutable" in css.headers["Cache-Control"]
    assert app.handle("GET", "/static/../data.py", {}).status == 404
    assert app.handle("POST", "/", {}).status == 405


def test_dashboard_never_writes_and_hides_errors(tmp_path: Path) -> None:
    db_path = tmp_path / "scorerecord.db"
    _seed(db_path)
    before = db_path.read_bytes()
    app = WebApp(db_path, clock=lambda: NOW)
    for path in ("/", "/partidos", "/partido/up-1", "/resultados", "/desempeno", "/estado"):
        assert app.handle("GET", path, {}).status == 200, path
    assert db_path.read_bytes() == before
    broken = WebApp(tmp_path / "missing.db", clock=lambda: NOW)
    response, html = _get(broken, "/partidos")
    assert response.status == 500
    assert "missing.db" not in html and "No pudimos generar" in html


def test_lean_shows_the_probability_of_the_side_it_names() -> None:
    lean = data._lean(0.47, 0.52, yes="Más de 2.5 goles", no="Menos de 2.5 goles")
    assert lean["side"] == "Menos de 2.5 goles"
    assert math.isclose(lean["side_prob"], 0.53)
    assert lean["highlight"] and lean["note"] == "algo menos que en la liga"
    neutral = data._lean(0.55, 0.54, yes="Ambos anotan", no="No anotan ambos")
    assert neutral["side"] == "Ambos anotan" and not neutral["highlight"]


def test_landing_page_presents_the_project(app: WebApp) -> None:
    response, html = _get(app, "/")
    assert response.status == 200
    assert "Pronósticos deportivos" in html and "hero-title" in html
    assert "github.com/perdomopro/ScoreRecord" in html
    for href in ('href="/partidos"', 'href="/resultados"', 'href="/desempeno"'):
        assert href in html
    assert 'class="topbar"' not in html and "data-notice" not in html
    _, inner = _get(app, "/resultados")
    assert 'class="topbar"' in inner and 'href="/partidos"' in inner
    no_db = WebApp(Path("missing.db"), clock=lambda: NOW)
    assert no_db.handle("GET", "/", {}).status == 200


def test_league_without_upcoming_fixtures_is_marked_off_season(app: WebApp) -> None:
    _, html = _get(app, "/partidos?liga=MLS")
    assert "MLS no tiene partidos programados" in html
    _, listing = _get(app, "/partidos")
    assert "is-quiet" in listing  # MLS chip is muted on every page
    _, epl = _get(app, "/partidos?liga=EPL")
    assert "no tiene partidos programados" not in epl


def test_status_page_skips_the_slow_integrity_scan(app: WebApp) -> None:
    _, html = _get(app, "/estado")
    assert "Integridad de la base" not in html
    assert "Servicios con fallas" in html
