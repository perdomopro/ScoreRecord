"""Forecast-only dashboard and health views for ScoreRecord."""

from __future__ import annotations

import html
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scorerecord.db import connect, init_db
from scorerecord.forecast_performance import (
    ForecastPerformanceReport,
    forecast_performance_report,
)
from scorerecord.forecast_policy import (
    FORECAST_MARKETS,
    RESULT_DIAGNOSTIC_MARKETS,
    select_primary_forecasts,
)
from scorerecord.forecast_policy import signal_strength as forecast_signal_strength
from scorerecord.forecast_quality import ForecastQualityItem, forecast_quality_items
from scorerecord.forecast_snapshots import ForecastSnapshotReport, forecast_snapshot_report
from scorerecord.ingest.football_data import FOOTBALL_DATA_LEAGUES
from scorerecord.league_config import GUI_LEAGUE_ORDER, gui_league_filter, league_label
from scorerecord.odds_coverage import OddsMarketCoverage, load_fixture_odds_coverage
from scorerecord.odds_horizons import (
    DEFAULT_HORIZONS,
    OddsHorizonCoverage,
    load_fixture_odds_horizon_coverage,
)
from scorerecord.settings import Settings
from scorerecord.time_utils import parse_datetime_utc, parse_kickoff_utc

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MIN_EDGE = 0.05
DEFAULT_MAX_EDGE = 0.25
DEFAULT_MIN_PROB = 0.15
DEFAULT_MAX_ODDS_AGE_MINUTES = 180.0
DEFAULT_MIN_BOOKS = 2
DEFAULT_DISPLAY_TZ = "America/Mexico_City"


RECREATIONAL_DISCLAIMER = """
<div style="background-color: #fff3cd; color: #856404; padding: 10px; border-radius: 5px; \
margin-bottom: 20px; text-align: center; font-size: 14px; border: 1px solid #ffeeba;">
    <strong>Atención:</strong> ScoreRecord muestra pronósticos del modelo \
para análisis y entretenimiento.
    No es asesoría financiera ni recomendación de apuesta. \
El modelo no ha demostrado ganarle al mercado de forma consistente.
    Úsalo como registro y pronóstico recreativo, no como una forma de ganar dinero.
    {link}
</div>
"""

def _recreational_warning_banner(settings: Settings | None) -> str:
    if not settings:
        return ""
    link = ""
    if settings.responsible_gambling_url:
        link = (
            f'<br><a href="{html.escape(settings.responsible_gambling_url)}" '
            'target="_blank" style="color: #856404; text-decoration: underline;">'
            'Más información sobre juego responsable</a>'
        )
    return RECREATIONAL_DISCLAIMER.format(link=link)


@dataclass(frozen=True)
class DashboardData:
    selected_run_id: int | None
    edge_run_id: int | None
    date_filter: str | None
    market_filter: str | None
    run_options: list[sqlite3.Row]
    picks: list[sqlite3.Row]
    audit_rows: list[sqlite3.Row]
    kpis: dict[str, Any]
    edge_sanity: dict[str, Any]
    legacy_warning_count: int
    generated_utc: str


@dataclass(frozen=True)
class ForecastMarket:
    market: str
    selection: str
    prob: float
    fair_odds: float
    model_version: str
    artifact_id: str | None
    market_validation_status: str
    prediction_run_id: int | None = None
    generated_utc: str | None = None
    artifact_created_utc: str | None = None
    trained_until: str | None = None
    training_rows: int | None = None
    training_scope: str | None = None
    training_tournament: str | None = None
    calibrator_scope: str | None = None
    calibrator_status: str | None = None


@dataclass(frozen=True)
class ForecastSnapshotView:
    market: str
    selection: str
    prob: float
    signal_code: str
    as_of_utc: str
    settled: bool
    result_label: int | None
    correct: int | None
    settled_utc: str | None


@dataclass(frozen=True)
class TeamHistorySummary:
    matches: int
    avg_total_goals: float | None
    btts_rate: float | None


@dataclass(frozen=True)
class ForecastInsight:
    quality_score: int
    quality_label: str
    quality_css_class: str
    uncertainty_points: float
    explanation_items: list[str]
    no_prediction_reasons: list[str]
    evidence_items: list[tuple[str, str]]


@dataclass(frozen=True)
class SystemHealthItem:
    label: str
    status: str
    detail: str


@dataclass(frozen=True)
class ForecastFixture:
    fixture_id: str
    kickoff_utc: str | None
    kickoff_local: str
    display_date: str | None
    display_time: str
    stage: str | None
    group_name: str | None
    ground: str | None
    league_code: str | None
    competition: str | None
    home: str
    away: str
    is_resolved: bool
    is_started: bool
    state: str
    state_label: str
    primary: ForecastMarket | None
    primary_markets: list[ForecastMarket]
    markets: list[ForecastMarket]
    result_markets: list[ForecastMarket]
    artifact_id: str | None
    model_version: str | None
    market_validation_status: str
    ou_monotonicity_ok: bool
    snapshots: list[ForecastSnapshotView]
    odds_coverage: list[OddsMarketCoverage]
    odds_horizons: list[OddsHorizonCoverage]
    insight: ForecastInsight
    paper_picks: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class UpcomingFixtureLink:
    fixture_id: str
    display_date: str
    display_time: str
    league_code: str | None
    competition: str | None
    home: str
    away: str


@dataclass(frozen=True)
class HistoryFixtureLink:
    fixture_id: str | None
    display_date: str
    display_time: str
    league_code: str | None
    competition: str | None
    home: str
    away: str
    score: str | None
    predicted: bool
    source: str = "fixture"


@dataclass(frozen=True)
class MarketValidationMetric:
    run_id: int | None
    check_name: str
    metric_name: str
    value: float | None
    created_utc: str | None


@dataclass(frozen=True)
class MarketValidationArtifact:
    status: str
    count: int
    latest_created_utc: str | None


@dataclass(frozen=True)
class MarketValidationData:
    artifacts: list[MarketValidationArtifact]
    metrics: list[MarketValidationMetric]
    summary_status: str


@dataclass(frozen=True)
class ForecastData:
    target_date: str
    previous_date: str
    next_date: str
    display_tz: str
    latest_run_id: int | None
    generated_utc: str
    fixtures: list[ForecastFixture]
    all_fixture_count: int
    forecastable_count: int
    pending_definition_count: int
    inconsistent_count: int
    model_version: str | None
    artifact_id: str | None
    market_validation_status: str
    quality_items: list[ForecastQualityItem]
    system_health_items: list[SystemHealthItem]
    empty_state: str | None
    view_mode: str = "all"
    league_filter: str = "ALL"
    research_models: list[dict[str, Any]] = field(default_factory=list)
    upcoming_fixtures: list[UpcomingFixtureLink] = field(default_factory=list)
    past_fixtures: list[HistoryFixtureLink] = field(default_factory=list)


def serve_dashboard(
    db_path: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    refresh_seconds: int = 0,
    settings: Settings | None = None,
) -> None:
    with connect(db_path) as conn:
        init_db(conn)

    class Handler(BaseHTTPRequestHandler):
        _response_cache: dict[str, tuple[float, str, str]] = {}
        _cache_ttl_seconds = 10.0

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                self._write("ok\n", content_type="text/plain; charset=utf-8")
                return

            cached = self._cache_get(self.path)
            if cached is not None:
                body, content_type = cached
                self._write(
                    body,
                    content_type=content_type,
                    cache_response=False,
                )
                return

            params = parse_qs(parsed.query)
            mode = str(params.get("mode", [""])[0]).strip().lower()
            if parsed.path in {"/", "/index.html"} and mode != "audit":
                self._render_home(params)
                return
            if parsed.path == "/today":
                self._render_today(params)
                return
            if parsed.path == "/history":
                self._render_history(params)
                return
            if parsed.path == "/health":
                self._render_health(params)
                return
            if parsed.path == "/performance":
                self._render_performance(params)
                return
            if parsed.path == "/market":
                self._render_market(params)
                return
            if parsed.path == "/signals":
                self._render_signals(params)
                return
            if parsed.path.startswith("/match/"):
                fixture_id = parsed.path.removeprefix("/match/").strip("/")
                self._render_match(fixture_id, params)
                return
            if parsed.path == "/audit" or mode == "audit":
                self._render_audit(params)
                return
            self.send_error(404)

        def _render_today(self, params: dict[str, list[str]]) -> None:
            date_filter = _parse_date(params.get("date", [""])[0])
            view_mode = str(params.get("view", ["forecast"])[0]).strip().lower()
            league_filter = gui_league_filter(params.get("league", ["all"])[0])
            if view_mode not in ("all", "forecast", "paper", "research"):
                view_mode = "forecast"
            try:
                with connect(db_path) as conn:
                    data = load_forecast_data(
                        conn, 
                        target_date=date_filter, 
                        view_mode=view_mode, 
                        league_filter=league_filter,
                        settings=settings,
                        upcoming_limit=80,
                    )
                body = render_today_forecast(
                    data, settings=settings, refresh_seconds=refresh_seconds
                )
            except Exception as exc:  # pragma: no cover - defensive web path
                body = render_error(exc)
            self._write(body)

        def _render_match(self, fixture_id: str, params: dict[str, list[str]]) -> None:
            view_mode = str(params.get("view", ["forecast"])[0]).strip().lower()
            league_filter = gui_league_filter(params.get("league", ["all"])[0])
            partial = str(params.get("partial", ["0"])[0]).strip().lower() in {"1", "true", "yes"}
            if view_mode not in ("all", "forecast", "paper", "research"):
                view_mode = "forecast"
            try:
                with connect(db_path) as conn:
                    data = load_forecast_data(
                        conn, 
                        fixture_id=fixture_id, 
                        view_mode=view_mode, 
                        league_filter=league_filter,
                        settings=settings,
                        upcoming_limit=0,
                    )
                body = render_match_forecast(
                    data,
                    fixture_id,
                    settings=settings,
                    partial=partial,
                )
            except Exception as exc:  # pragma: no cover - defensive web path
                body = render_error(exc)
            self._write(body)

        def _render_home(self, params: dict[str, list[str]]) -> None:
            date_filter = _parse_date(params.get("date", [""])[0])
            league_filter = gui_league_filter(params.get("league", ["all"])[0])
            try:
                with connect(db_path) as conn:
                    data = load_forecast_data(
                        conn,
                        target_date=date_filter,
                        view_mode="forecast",
                        league_filter=league_filter,
                        settings=settings,
                        upcoming_limit=40,
                    )
                body = render_home_page(
                    data, settings=settings, refresh_seconds=refresh_seconds
                )
            except Exception as exc:  # pragma: no cover - defensive web path
                body = render_error(exc)
            self._write(body)

        def _render_health(self, params: dict[str, list[str]]) -> None:
            date_filter = _parse_date(params.get("date", [""])[0])
            league_filter = gui_league_filter(params.get("league", ["all"])[0])
            try:
                with connect(db_path) as conn:
                    data = load_forecast_data(
                        conn,
                        target_date=date_filter,
                        view_mode="forecast",
                        league_filter=league_filter,
                        settings=settings,
                        upcoming_limit=0,
                    )
                body = render_health_page(
                    data, settings=settings, refresh_seconds=refresh_seconds
                )
            except Exception as exc:  # pragma: no cover - defensive web path
                body = render_error(exc)
            self._write(body)

        def _render_history(self, params: dict[str, list[str]]) -> None:
            date_filter = _parse_date(params.get("date", [""])[0])
            league_filter = gui_league_filter(params.get("league", ["all"])[0])
            tab = str(params.get("tab", ["upcoming"])[0]).strip().lower()
            if tab not in {"upcoming", "past"}:
                tab = "upcoming"
            try:
                with connect(db_path) as conn:
                    data = load_forecast_data(
                        conn,
                        target_date=date_filter,
                        view_mode="forecast",
                        league_filter=league_filter,
                        settings=settings,
                        upcoming_limit=2000,
                        include_past_fixtures=True,
                    )
                body = render_history_page(
                    data,
                    active_tab=tab,
                    settings=settings,
                    refresh_seconds=refresh_seconds,
                )
            except Exception as exc:  # pragma: no cover - defensive web path
                body = render_error(exc)
            self._write(body)

        def _render_performance(self, params: dict[str, list[str]]) -> None:
            date_filter = _parse_date(params.get("date", [""])[0])
            league_filter = gui_league_filter(params.get("league", ["all"])[0])
            try:
                with connect(db_path) as conn:
                    data = load_forecast_data(
                        conn,
                        target_date=date_filter,
                        view_mode="forecast",
                        league_filter=league_filter,
                        settings=settings,
                        upcoming_limit=0,
                    )
                    report = forecast_performance_report(conn)
                    snapshot_report = forecast_snapshot_report(conn)
                body = render_performance_page(
                    data,
                    report,
                    snapshot_report,
                    settings=settings,
                    refresh_seconds=refresh_seconds,
                )
            except Exception as exc:  # pragma: no cover - defensive web path
                body = render_error(exc)
            self._write(body)

        def _render_market(self, params: dict[str, list[str]]) -> None:
            date_filter = _parse_date(params.get("date", [""])[0])
            league_filter = gui_league_filter(params.get("league", ["all"])[0])
            try:
                with connect(db_path) as conn:
                    data = load_forecast_data(
                        conn,
                        target_date=date_filter,
                        view_mode="forecast",
                        league_filter=league_filter,
                        settings=settings,
                        upcoming_limit=0,
                    )
                    market_data = load_market_validation_data(conn)
                body = render_market_page(
                    data,
                    market_data,
                    settings=settings,
                    refresh_seconds=refresh_seconds,
                )
            except Exception as exc:  # pragma: no cover - defensive web path
                body = render_error(exc)
            self._write(body)

        def _render_signals(self, params: dict[str, list[str]]) -> None:
            date_filter = _parse_date(params.get("date", [""])[0])
            league_filter = gui_league_filter(params.get("league", ["all"])[0])
            try:
                with connect(db_path) as conn:
                    data = load_forecast_data(
                        conn,
                        target_date=date_filter,
                        view_mode="forecast",
                        league_filter=league_filter,
                        settings=settings,
                        upcoming_limit=80,
                    )
                body = render_signals_page(
                    data,
                    settings=settings,
                    refresh_seconds=refresh_seconds,
                )
            except Exception as exc:  # pragma: no cover - defensive web path
                body = render_error(exc)
            self._write(body)

        def _render_audit(self, params: dict[str, list[str]]) -> None:
            selected_run_id = _parse_int(params.get("run_id", [""])[0])
            date_filter = _parse_date(params.get("date", [""])[0])
            market_filter = _parse_market(params.get("market", [""])[0])
            try:
                with connect(db_path) as conn:
                    data = load_dashboard_data(
                        conn,
                        selected_run_id=selected_run_id,
                        date_filter=date_filter,
                        market_filter=market_filter,
                    )
                body = render_dashboard(data, refresh_seconds=refresh_seconds)
            except Exception as exc:  # pragma: no cover - defensive web path
                body = render_error(exc)
            self._write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _write(
            self,
            body: str,
            *,
            status: int = 200,
            content_type: str = "text/html; charset=utf-8",
            cache_response: bool = True,
        ) -> None:
            encoded = body.encode("utf-8")
            if cache_response and status == 200 and "Dashboard Error" not in body:
                self._cache_set(self.path, body, content_type)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def _cache_get(self, key: str) -> tuple[str, str] | None:
            item = self._response_cache.get(key)
            if item is None:
                return None
            created, body, content_type = item
            if monotonic() - created > self._cache_ttl_seconds:
                self._response_cache.pop(key, None)
                return None
            return body, content_type

        def _cache_set(self, key: str, body: str, content_type: str) -> None:
            if len(self._response_cache) > 128:
                self._response_cache.clear()
            self._response_cache[key] = (monotonic(), body, content_type)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    finally:
        server.server_close()


def load_forecast_data(
    conn: sqlite3.Connection,
    *,
    target_date: str | None = None,
    fixture_id: str | None = None,
    display_tz_name: str | None = None,
    now_utc: datetime | None = None,
    view_mode: str = "all",
    league_filter: str = "ALL",
    settings: Settings | None = None,
    upcoming_limit: int = 80,
    include_past_fixtures: bool = False,
) -> ForecastData:
    display_tz = _display_tz(display_tz_name)
    now = now_utc or datetime.now(UTC)
    now_local = now.astimezone(display_tz)
    selected_date = target_date or now_local.date().isoformat()
    previous_date = (date.fromisoformat(selected_date) - timedelta(days=1)).isoformat()
    next_date = (date.fromisoformat(selected_date) + timedelta(days=1)).isoformat()
    league_filter = gui_league_filter(league_filter)
    inventory_rows = _forecast_fixture_rows(conn, fixture_id=fixture_id, league_filter=league_filter)
    upcoming_fixtures = (
        _upcoming_fixture_links(
            inventory_rows,
            display_tz=display_tz,
            now_utc=now,
            selected_date=selected_date,
            limit=upcoming_limit,
        )
        if upcoming_limit > 0
        else []
    )
    past_fixtures = (
        _past_fixture_links(
            conn,
            display_tz=display_tz,
            now_utc=now,
            league_filter=league_filter,
        )
        if include_past_fixtures
        else []
    )
    rows = (
        inventory_rows
        if fixture_id is not None
        else _fixture_rows_for_local_date(
            inventory_rows,
            display_tz=display_tz,
            target_date=selected_date,
        )
    )
    fixture_ids = [str(row["fixture_id"]) for row in rows]
    latest_run_id = _latest_prediction_run_id(conn, fixture_ids=fixture_ids)
    prediction_rows = _forecast_prediction_rows(conn, fixture_ids=fixture_ids)
    predictions_by_fixture = _group_forecast_predictions(prediction_rows)
    result_prediction_rows = _result_prediction_rows(conn, fixture_ids=fixture_ids)
    result_predictions_by_fixture = _group_forecast_predictions(result_prediction_rows)
    quality_items = forecast_quality_items(conn, latest_run_id)
    system_health_items = _system_health_items(conn, latest_run_id, quality_items, now)
    team_ratings = _latest_elo_by_team(conn, _team_ids_from_fixture_rows(rows))
    team_history = _team_history_by_scope(conn, rows)
    odds_coverage_by_fixture: dict[str, list[OddsMarketCoverage]] = {}
    odds_horizons_by_fixture: dict[str, list[OddsHorizonCoverage]] = {}
    if fixture_id is not None:
        odds_coverage_by_fixture = {
            str(row["fixture_id"]): load_fixture_odds_coverage(
                conn,
                str(row["fixture_id"]),
                as_of_utc=now,
            )
            for row in rows
        }
        odds_horizons_by_fixture = {
            str(row["fixture_id"]): load_fixture_odds_horizon_coverage(
                conn,
                str(row["fixture_id"]),
            )
            for row in rows
        }
    snapshots_by_fixture = _group_forecast_snapshots(
        _forecast_snapshot_rows(conn, [str(row["fixture_id"]) for row in rows])
    )
    paper_picks_by_fixture: dict[str, list[dict[str, Any]]] = {}
    if view_mode in ("all", "paper"):
        run_options = _recommendation_runs(conn)
        rec_run_id = int(run_options[0]["run_id"]) if run_options else None
        if rec_run_id is not None:
            # We fetch recommendations. For /match/<id> date_filter is None, for /today target_date is passed.
            raw_picks = _recommendation_rows(conn, rec_run_id, date_filter=selected_date if fixture_id is None else None, market_filter=None)
            for p in raw_picks:
                fix_id = str(p["fixture_id"])
                paper_picks_by_fixture.setdefault(fix_id, []).append(dict(p))
    fixtures: list[ForecastFixture] = []
    for row in rows:
        fixture = _forecast_fixture_from_row(
            row,
            predictions_by_fixture.get(str(row["fixture_id"]), []),
            result_predictions_by_fixture.get(str(row["fixture_id"]), []),
            snapshots_by_fixture.get(str(row["fixture_id"]), []),
            odds_coverage_by_fixture.get(str(row["fixture_id"]), []),
            odds_horizons_by_fixture.get(str(row["fixture_id"]), []),
            paper_picks_by_fixture.get(str(row["fixture_id"]), []),
            team_ratings=team_ratings,
            team_history=team_history,
            display_tz=display_tz,
            selected_date=selected_date if fixture_id is None else None,
            now_utc=now,
        )
        if fixture is not None:
            fixtures.append(fixture)

    model_version = next(
        (fixture.model_version for fixture in fixtures if fixture.model_version),
        None,
    )
    artifact_id = next((fixture.artifact_id for fixture in fixtures if fixture.artifact_id), None)
    validation_status = next(
        (
            fixture.market_validation_status
            for fixture in fixtures
            if fixture.market_validation_status != "NOT_TESTED"
        ),
        "NOT_TESTED",
    )
    empty_state = None
    if latest_run_id is None:
        empty_state = "SIN DATOS"
    elif not fixtures:
        empty_state = "No hay partidos para esta liga en esta fecha." if league_filter != "ALL" else "SIN PARTIDOS"
    elif not any(fixture.markets for fixture in fixtures):
        empty_state = "SIN PREDICCIONES"
    research_models: list[dict[str, Any]] = []
    if view_mode == "research" and settings is not None:
        outputs_dir = settings.raw_dir.parent / "outputs"
        for sub in ["benchmarks", "backtests", "backtests-smoke", "coverage", "diagnostics"]:
            d_dir = outputs_dir / sub
            if d_dir.exists():
                for f in d_dir.glob("*.json"):
                    if "metadata" in f.name or "benchmark" in f.name or "audit" in f.name or "decision" in f.name or "coverage" in f.name or "probe" in f.name:
                        try:
                            d = json.loads(f.read_text("utf-8"))
                            st = d.get("benchmark_state") or d.get("overall_state") or d.get("overall", {}).get("benchmark_state", "UNKNOWN")

                            # Additional inspection for Goals V2
                            if "goals_v2" in f.name:
                                m_st = d.get("market_status")
                                if m_st:
                                    st = f"{st} | {m_st}"

                            research_models.append({
                                "name": f"{sub}/{f.name}",
                                "file": f.name,
                                "state": st,
                                "status": "RESEARCH_ONLY / no conectado a fixtures actuales"
                            })
                        except Exception: pass

    return ForecastData(
        target_date=selected_date,
        previous_date=previous_date,
        next_date=next_date,
        display_tz=getattr(display_tz, "key", str(display_tz)),
        latest_run_id=latest_run_id,
        generated_utc=now.isoformat(timespec="seconds"),
        fixtures=fixtures,
        all_fixture_count=len(fixtures),
        forecastable_count=sum(1 for fixture in fixtures if fixture.primary is not None),
        pending_definition_count=sum(1 for fixture in fixtures if not fixture.is_resolved),
        inconsistent_count=sum(1 for fixture in fixtures if not fixture.ou_monotonicity_ok),
        model_version=model_version,
        artifact_id=artifact_id,
        market_validation_status=validation_status,
        quality_items=quality_items,
        system_health_items=system_health_items,
        empty_state=empty_state,
        view_mode=view_mode,
        league_filter=league_filter,
        research_models=research_models,
        upcoming_fixtures=upcoming_fixtures,
        past_fixtures=past_fixtures,
    )


def render_today_forecast(data: ForecastData, *, settings: Settings | None = None, refresh_seconds: int = 60) -> str:
    cards = ""
    view_mode = data.view_mode
    league = data.league_filter
    league_nav = _league_nav(data)
    date_toolbar = _date_toolbar(data)

    if view_mode == "research":
        cards = "<h3>Modelos de investigación disponibles</h3><ul>"
        if not data.research_models:
            cards += "<li>No hay modelos de investigación en outputs/</li>"
        else:
            for rm in data.research_models:
                cards += f'''<li>
                    <strong>Modelo:</strong> {rm['name']}<br>
                    <strong>Estado interno:</strong> {rm['state']}<br>
                    <strong>Estado actual:</strong> {rm['status']}<br>
                </li>'''
        cards += "</ul>"
    else:
        grouped_cards: dict[str, str] = {}
        for fixture in data.fixtures:
            if view_mode == "paper" and not fixture.paper_picks:
                continue
            if view_mode == "forecast" and not fixture.primary:
                continue

            if league == "ALL":
                comp_label = fixture.competition or league_label(fixture.league_code)
                grouped_cards[comp_label] = (
                    grouped_cards.get(comp_label, "") + _forecast_card(fixture, view_mode)
                )
            else:
                cards += _forecast_card(fixture, view_mode)

        if grouped_cards:
            cards = "".join(
                _league_match_group(comp_label, group_cards)
                for comp_label, group_cards in grouped_cards.items()
            )

        if not cards:
            if view_mode == "paper":
                cards = _empty_forecast_state("No hay paper picks recreativos para esta fecha. Puedes ver pronósticos del modelo en la vista Todo o Pronósticos.")
            else:
                cards = _empty_forecast_state(data.empty_state or "No hay partidos para esta liga en esta fecha.")

    league_banner = _league_banner(league)

    return f"""<!doctype html>
<html lang="es" data-theme="scorerecord">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {_auto_refresh_meta(refresh_seconds)}
  <title>ScoreRecord Forecast</title>
  <style>{_forecast_css()}</style>
</head>
<body class="forecast-page">
  {_forecast_nav(data)}
  <main class="forecast-shell">
    {_recreational_warning_banner(settings)}
    {_forecast_warning_banner(data)}
    {league_nav}
    {league_banner}
    {date_toolbar}
    {_top_signals_panel(data)}
    {cards if league == "ALL" else f'<section class="match-grid">{cards}</section>'}
  </main>
  {_match_modal_shell()}
  {_forecast_modal_script()}
</body>
</html>"""


def render_home_page(
    data: ForecastData,
    *,
    settings: Settings | None = None,
    refresh_seconds: int = 60,
) -> str:
    return f"""<!doctype html>
<html lang="es" data-theme="scorerecord">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {_auto_refresh_meta(refresh_seconds)}
  <title>ScoreRecord</title>
  <style>{_forecast_css()}</style>
</head>
<body class="forecast-page landing-page">
  <main class="landing-shell">
    {_home_hero(data, landing=True)}
  </main>
</body>
</html>"""


def render_history_page(
    data: ForecastData,
    *,
    active_tab: str = "upcoming",
    settings: Settings | None = None,
    refresh_seconds: int = 60,
) -> str:
    active_tab = active_tab if active_tab in {"upcoming", "past"} else "upcoming"
    content = (
        _history_upcoming_section(data)
        if active_tab == "upcoming"
        else _history_past_section(data)
    )
    return f"""<!doctype html>
<html lang="es" data-theme="scorerecord">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {_auto_refresh_meta(refresh_seconds)}
  <title>ScoreRecord Historial</title>
  <style>{_forecast_css()}</style>
</head>
<body class="forecast-page">
  {_forecast_nav(data, active="history")}
  <main class="forecast-shell">
    <section class="health-hero history-hero">
      <span>Historial</span>
      <h1>Partidos cargados</h1>
      <p>
        Inventario de próximos fixtures y partidos pasados disponibles en ScoreRecord.
        Esta vista ayuda a saber qué está cargado, analizado y listo para auditoría.
      </p>
    </section>
    {_recreational_warning_banner(settings)}
    {_league_nav(data, base_path="/history", extra_params={"tab": active_tab})}
    {_history_tabs(data, active_tab)}
    {content}
  </main>
  {_match_modal_shell()}
  {_forecast_modal_script()}
</body>
</html>"""


def render_health_page(
    data: ForecastData,
    *,
    settings: Settings | None = None,
    refresh_seconds: int = 60,
) -> str:
    return f"""<!doctype html>
<html lang="es" data-theme="scorerecord">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {_auto_refresh_meta(refresh_seconds)}
  <title>ScoreRecord Health</title>
  <style>{_forecast_css()}</style>
</head>
<body class="forecast-page">
  {_forecast_nav(data, active="health")}
  <main class="forecast-shell">
    <section class="health-hero">
      <span>Health</span>
      <h1>Estado operativo de ScoreRecord</h1>
      <p>Resumen de servidor, corridas, modelo, calibradores, Elo, readiness y datos de la GUI forecast-only.</p>
      <div class="hero-actions">
        <a class="btn primary" href="/today?view=forecast&league=ALL">Ver partidos</a>
        <a class="btn ghost" href="/health">Actualizar health</a>
      </div>
    </section>
    {_recreational_warning_banner(settings)}
    {_forecast_warning_banner(data)}
    {_forecast_quality_panel(data)}
    {_system_health_panel(data)}
    {_forecast_stats(data)}
    {_health_model_summary(data)}
  </main>
</body>
</html>"""


def render_performance_page(
    data: ForecastData,
    report: ForecastPerformanceReport,
    snapshot_report: ForecastSnapshotReport,
    *,
    settings: Settings | None = None,
    refresh_seconds: int = 60,
) -> str:
    return f"""<!doctype html>
<html lang="es" data-theme="scorerecord">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {_auto_refresh_meta(refresh_seconds)}
  <title>ScoreRecord Performance</title>
  <style>{_forecast_css()}</style>
</head>
<body class="forecast-page">
  {_forecast_nav(data, active="performance")}
  <main class="forecast-shell">
    <section class="health-hero">
      <span>Performance</span>
      <h1>Precisión del modelo</h1>
      <p>
        Comparación de predicciones ya liquidadas contra marcadores finales.
        Es evaluación estadística, no rendimiento financiero.
      </p>
    </section>
    {_recreational_warning_banner(settings)}
    {_performance_summary_panel(report, snapshot_report)}
    {_performance_table("Por mercado", report.by_market)}
    {_performance_table("Por liga", report.by_league)}
  </main>
</body>
</html>"""


def render_market_page(
    data: ForecastData,
    market_data: MarketValidationData,
    *,
    settings: Settings | None = None,
    refresh_seconds: int = 60,
) -> str:
    return f"""<!doctype html>
<html lang="es" data-theme="scorerecord">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {_auto_refresh_meta(refresh_seconds)}
  <title>ScoreRecord Mercado</title>
  <style>{_forecast_css()}</style>
</head>
<body class="forecast-page">
  {_forecast_nav(data, active="market")}
  <main class="forecast-shell">
    <section class="health-hero market-hero">
      <span>Mercado</span>
      <h1>Modelo vs mercado</h1>
      <p>
        El mercado es la referencia colectiva de probabilidades implícitas. Si el
        modelo tiene peor log-loss que el mercado, sus probabilidades pueden servir
        como pronóstico recreativo, pero no como evidencia de ventaja independiente.
      </p>
    </section>
    {_recreational_warning_banner(settings)}
    {_market_status_panel(market_data)}
    {_market_explainer_panel()}
    {_market_metrics_panel(market_data)}
  </main>
</body>
</html>"""


def render_signals_page(
    data: ForecastData,
    *,
    settings: Settings | None = None,
    refresh_seconds: int = 60,
) -> str:
    return f"""<!doctype html>
<html lang="es" data-theme="scorerecord">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {_auto_refresh_meta(refresh_seconds)}
  <title>ScoreRecord Señales</title>
  <style>{_forecast_css()}</style>
</head>
<body class="forecast-page">
  {_forecast_nav(data, active="signals")}
  <main class="forecast-shell">
    <section class="health-hero signals-hero">
      <span>Top 3</span>
      <h1>Mejores señales del día</h1>
      <p>
        Partidos ordenados por separación del 50% en OU2.5 y BTTS, ponderados por
        calidad de datos. No usa cuotas ni calcula valor.
      </p>
      <div class="hero-actions">
        <a class="btn primary" href="/today?date={_e(data.target_date)}&league={_e(data.league_filter)}">Ver todos</a>
        <a class="btn ghost" href="/market">Contexto de mercado</a>
      </div>
    </section>
    {_recreational_warning_banner(settings)}
    {_top_signals_panel(data, expanded=True)}
  </main>
  {_match_modal_shell()}
  {_forecast_modal_script()}
</body>
</html>"""


def _home_hero(data: ForecastData, *, landing: bool = False) -> str:
    league_buttons = "".join(
        f'<a class="btn ghost" href="/today?view=forecast&league={_e(code)}">{_e(league_label(code))}</a>'
        for code in GUI_LEAGUE_ORDER
    )
    css_class = "home-hero landing-hero" if landing else "home-hero"
    return f"""
    <section class="{css_class}">
      <div class="hero-copy">
        <span>pronosticos deportivos</span>
        <h1>ScoreRecord</h1>
        <p>
          Sistema forecast-only para analizar partidos con modelos estadisticos,
          trazabilidad de artifacts y explicaciones auditables. No es una
          recomendacion de apuesta.
        </p>
        <a class="repo-link" href="https://github.com/Perdomazo/ScoreRecord" target="_blank" rel="noreferrer">
          github.com/Perdomazo/ScoreRecord
        </a>
      </div>
      <div class="hero-actions">
        <a class="btn primary" href="/today?view=forecast&league=ALL">Ver partidos</a>
        <a class="btn ghost" href="/signals?league={_e(data.league_filter)}">Mejores señales</a>
        {league_buttons}
        <div class="hero-dropdown">
          <button class="btn ghost" type="button">Historial</button>
          <div class="nav-dropdown-menu">
            <a href="/history?tab=upcoming&league={_e(data.league_filter)}">Próximos cargados</a>
            <a href="/history?tab=past&league={_e(data.league_filter)}">Partidos pasados</a>
          </div>
        </div>
        <a class="btn ghost" href="/performance?league={_e(data.league_filter)}">Performance</a>
        <a class="btn ghost" href="/market?league={_e(data.league_filter)}">Mercado</a>
        <a class="btn ghost" href="/health?date={_e(data.target_date)}&league={_e(data.league_filter)}">Health</a>
      </div>
    </section>
    """


def _date_toolbar(data: ForecastData) -> str:
    league = data.league_filter
    chips: list[str] = []
    counts: dict[str, int] = {}
    for item in data.upcoming_fixtures:
        counts[item.display_date] = counts.get(item.display_date, 0) + 1
    for day, count in list(counts.items())[:7]:
        cls = "date-chip active" if day == data.target_date else "date-chip"
        chips.append(
            f'<a class="{cls}" href="/today?date={_e(day)}&view={_e(data.view_mode)}'
            f'&league={_e(league)}"><strong>{_e(_short_date_label(day))}</strong>'
            f'<span>{count} partidos</span></a>'
        )
    next_loaded = ""
    if data.upcoming_fixtures:
        first_date = data.upcoming_fixtures[0].display_date
        next_loaded = (
            f'<a class="btn secondary" href="/today?date={_e(first_date)}'
            f'&view={_e(data.view_mode)}&league={_e(league)}">Proxima fecha con partido</a>'
        )
    chips_html = (
        f'<div class="date-chip-row" aria-label="Fechas con partidos">{"".join(chips)}</div>'
        if chips
        else '<p class="muted">No hay fechas futuras cargadas para esta seleccion.</p>'
    )
    return f"""
    <section class="date-toolbar">
      <div class="date-toolbar-main">
        <a class="btn ghost" href="/today?date={_e(data.previous_date)}&view={_e(data.view_mode)}&league={_e(league)}">Anterior</a>
        <form method="get" action="/today" class="date-form">
          <input type="hidden" name="view" value="{_e(data.view_mode)}">
          <input type="hidden" name="league" value="{_e(league)}">
          <label for="date">Elegir fecha</label>
          <input id="date" name="date" type="date" value="{_e(data.target_date)}">
          <button class="btn" type="submit">Ver fecha</button>
        </form>
        <a class="btn ghost" href="/today?date={_e(data.next_date)}&view={_e(data.view_mode)}&league={_e(league)}">Siguiente</a>
        {next_loaded}
      </div>
      {chips_html}
    </section>
    """


def _health_model_summary(data: ForecastData) -> str:
    items = [
        ("Modelo", data.model_version or "sin prediccion"),
        ("Artifact", _short_id(data.artifact_id) or "n/a"),
        ("Market validation", data.market_validation_status or "NOT_TESTED"),
        ("Prediction run", str(data.latest_run_id or "n/a")),
        ("Fecha", data.target_date),
        ("TZ", data.display_tz),
    ]
    cells = "".join(
        f"""
        <div class="health-summary-item">
          <span>{_e(label)}</span>
          <strong>{_e(value)}</strong>
        </div>
        """
        for label, value in items
    )
    return f"""
    <section class="health-summary-panel">
      <div class="section-head">
        <h2>Modelo activo en la vista</h2>
        <span>{_e(data.generated_utc)}</span>
      </div>
      <div class="health-summary-grid">{cells}</div>
    </section>
    """


def load_market_validation_data(conn: sqlite3.Connection) -> MarketValidationData:
    artifact_rows = conn.execute(
        """
        SELECT market_validation_status, COUNT(*) AS count, MAX(created_utc) AS latest
        FROM model_artifact
        GROUP BY market_validation_status
        ORDER BY latest DESC
        """
    ).fetchall()
    artifacts = [
        MarketValidationArtifact(
            status=str(row["market_validation_status"] or "NOT_TESTED"),
            count=int(row["count"] or 0),
            latest_created_utc=str(row["latest"]) if row["latest"] else None,
        )
        for row in artifact_rows
    ]
    metric_rows = conn.execute(
        """
        SELECT run_id, check_name, metric_name, metric_value, created_utc
        FROM data_quality
        WHERE lower(check_name) LIKE '%market%'
           OR lower(metric_name) LIKE '%market%'
           OR lower(check_name) LIKE '%benchmark%'
           OR lower(metric_name) LIKE '%benchmark%'
        ORDER BY quality_id DESC
        LIMIT 24
        """
    ).fetchall()
    metrics = [
        MarketValidationMetric(
            run_id=int(row["run_id"]) if row["run_id"] is not None else None,
            check_name=str(row["check_name"]),
            metric_name=str(row["metric_name"]),
            value=float(row["metric_value"]) if row["metric_value"] is not None else None,
            created_utc=str(row["created_utc"]) if row["created_utc"] else None,
        )
        for row in metric_rows
    ]
    statuses = {artifact.status.upper() for artifact in artifacts}
    if "PROMISING" in statuses or "FORWARD_VALIDATED" in statuses:
        summary = "PROMISING"
    elif "INCONCLUSIVE" in statuses:
        summary = "INCONCLUSIVE"
    elif "REJECTED" in statuses or "REJECTED_FOR_INDEPENDENT_VALUE" in statuses:
        summary = "REJECTED"
    else:
        summary = "NOT_TESTED"
    return MarketValidationData(artifacts=artifacts, metrics=metrics, summary_status=summary)


def _performance_summary_panel(
    report: ForecastPerformanceReport,
    snapshot_report: ForecastSnapshotReport,
) -> str:
    items = [
        ("Decisiones evaluadas", report.decisions),
        ("Filas de predicción", report.prediction_rows),
        ("Fixtures con resultado", report.fixture_count),
        ("Último settlement", report.latest_settled_utc or "n/a"),
        ("Snapshots congelados", snapshot_report.total_snapshots),
        ("Snapshots liquidados", snapshot_report.settled_snapshots),
    ]
    cells = "".join(
        f"""
        <div class="stat">
          <span>{_e(label)}</span>
          <strong>{_v(value)}</strong>
        </div>
        """
        for label, value in items
    )
    return f"""
    <section class="forecast-stats performance-stats">
      {cells}
    </section>
    """


def _performance_table(title: str, rows: list[Any]) -> str:
    if not rows:
        return _empty_forecast_state("Sin resultados liquidados todavía")
    body = "".join(
        f"""
        <tr>
          <td>{_e(row.group_value)}</td>
          <td>{row.decisions}</td>
          <td>{row.prediction_rows}</td>
          <td>{_pct(row.accuracy)}</td>
          <td>{_num(row.brier)}</td>
          <td>{_num(row.logloss)}</td>
        </tr>
        """
        for row in rows
    )
    return f"""
    <section class="health-summary-panel">
      <div class="section-head">
        <h2>{_e(title)}</h2>
        <span>Top-pick accuracy; Brier/log-loss por unidad estadística</span>
      </div>
      <table class="forecast-table">
        <thead>
          <tr>
            <th>Grupo</th><th>Decisiones</th><th>Filas</th>
            <th>Accuracy</th><th>Brier</th><th>Log-loss</th>
          </tr>
        </thead>
        <tbody>{body}</tbody>
      </table>
    </section>
    """


def _market_status_panel(market_data: MarketValidationData) -> str:
    artifact_rows = "".join(
        f"""
        <div class="quality-item {_market_status_class(item.status)}">
          <span>{_e(item.status)}</span>
          <strong>{item.count}</strong>
          <small>{_e(item.latest_created_utc or 'sin fecha')}</small>
        </div>
        """
        for item in market_data.artifacts
    )
    if not artifact_rows:
        artifact_rows = """
        <div class="quality-item quality-warn">
          <span>Artifacts</span><strong>WARN</strong><small>Sin artifacts registrados</small>
        </div>
        """
    return f"""
    <section class="quality-panel market-status-panel">
      <div class="panel-title">Estado de validación contra mercado: {_e(market_data.summary_status)}</div>
      {artifact_rows}
    </section>
    """


def _market_explainer_panel() -> str:
    return """
    <section class="health-summary-panel market-explainer">
      <div class="section-head">
        <h2>Qué significa perder contra el mercado</h2>
        <span>Lectura operativa</span>
      </div>
      <p>
        El mercado agrega información pública, noticias, dinero y correcciones de muchas
        casas. Si ScoreRecord tiene peor log-loss que la probabilidad justa del mercado,
        el modelo no debe usarse como señal de ventaja independiente.
      </p>
      <p>
        Esto no invalida la GUI como pronóstico: sigue explicando goles, BTTS, resultado
        estimado, cobertura y calidad de datos. Lo que bloquea es el lenguaje de edge,
        tamaño de posición o recomendación de apuesta.
      </p>
    </section>
    """


def _market_metrics_panel(market_data: MarketValidationData) -> str:
    if not market_data.metrics:
        return """
        <section class="health-summary-panel">
          <div class="section-head">
            <h2>Métricas recientes</h2>
            <span>Sin filas de benchmark en data_quality</span>
          </div>
          <p class="muted">Ejecuta los reportes de benchmark para poblar esta sección.</p>
        </section>
        """
    rows = "".join(
        f"""
        <tr>
          <td>{_e(str(metric.run_id or 'n/a'))}</td>
          <td>{_e(metric.check_name)}</td>
          <td>{_e(metric.metric_name)}</td>
          <td>{_num(metric.value)}</td>
          <td>{_e(metric.created_utc or 'n/a')}</td>
        </tr>
        """
        for metric in market_data.metrics
    )
    return f"""
    <section class="health-summary-panel">
      <div class="section-head">
        <h2>Métricas recientes</h2>
        <span>data_quality</span>
      </div>
      <table class="forecast-table">
        <thead><tr><th>Run</th><th>Check</th><th>Métrica</th><th>Valor</th><th>Creado</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </section>
    """


def _market_status_class(status: str) -> str:
    normalized = status.lower().replace("_", "-")
    if "rejected" in normalized:
        return "quality-warn"
    if normalized in {"promising", "forward-validated"}:
        return "quality-ok"
    if normalized == "not-tested":
        return "quality-not-tested"
    return f"quality-{normalized}"


def _top_signals_panel(data: ForecastData, *, expanded: bool = False) -> str:
    fixtures = _top_signal_fixtures(data, limit=3)
    if not fixtures:
        return "" if not expanded else _empty_forecast_state("Sin señales para esta fecha")
    rows = "".join(_top_signal_item(fixture, data) for fixture in fixtures)
    title = "Mejores 3 señales del día" if expanded else "Top 3 señales del día"
    action = "" if expanded else '<a class="btn ghost" href="/signals">Ver detalle</a>'
    return f"""
    <section class="top-signals-panel">
      <div class="section-head">
        <h2>{title}</h2>
        {action}
      </div>
      <div class="top-signal-list">{rows}</div>
    </section>
    """


def _top_signal_fixtures(data: ForecastData, *, limit: int) -> list[ForecastFixture]:
    candidates = [
        fixture
        for fixture in data.fixtures
        if fixture.primary_markets and fixture.state == "forecast"
    ]
    return sorted(candidates, key=_signal_sort_key, reverse=True)[:limit]


def _signal_sort_key(fixture: ForecastFixture) -> tuple[float, int]:
    signal_delta = max(abs(market.prob - 0.5) for market in fixture.primary_markets)
    return (signal_delta * (fixture.insight.quality_score / 100.0), fixture.insight.quality_score)


def _top_signal_item(fixture: ForecastFixture, data: ForecastData) -> str:
    market = max(fixture.primary_markets, key=lambda item: abs(item.prob - 0.5))
    signal = forecast_signal_strength(market.prob)
    detail_href = (
        f"/match/{_e(fixture.fixture_id)}?view=forecast&league={_e(fixture.league_code or data.league_filter)}"
    )
    modal_href = f"{detail_href}&partial=1"
    return f"""
    <article class="top-signal-item {signal.css_class}">
      <div>
        <span>{_e(fixture.display_time)} · {_e(fixture.competition or league_label(fixture.league_code))}</span>
        <strong>{_e(fixture.home)} vs {_e(fixture.away)}</strong>
        <small>{_e(signal.label)} · {_e(_forecast_diagnostic_label(market))}</small>
      </div>
      <div>
        <strong>{fixture.insight.quality_score}%</strong>
        <a class="btn detail-button" href="{detail_href}" data-detail-url="{modal_href}">Detalle</a>
      </div>
    </article>
    """


def _match_modal_shell() -> str:
    return """
    <div id="match-modal" class="match-modal" hidden>
      <div class="match-modal-backdrop" data-modal-close></div>
      <section class="match-modal-panel" role="dialog" aria-modal="true" aria-label="Detalle de partido">
        <button class="modal-close" type="button" data-modal-close aria-label="Cerrar">x</button>
        <div id="match-modal-content" class="match-modal-content"></div>
      </section>
    </div>
    """


def _forecast_modal_script() -> str:
    return """
    <script>
    (function () {
      const modal = document.getElementById("match-modal");
      const content = document.getElementById("match-modal-content");
      if (!modal || !content) return;

      function closeModal() {
        modal.hidden = true;
        content.innerHTML = "";
        document.body.classList.remove("modal-open");
      }

      function openModal(html) {
        content.innerHTML = html;
        modal.hidden = false;
        document.body.classList.add("modal-open");
      }

      document.addEventListener("click", async function (event) {
        const trigger = event.target.closest("[data-detail-url]");
        if (trigger) {
          event.preventDefault();
          openModal('<div class="modal-loading">Cargando detalle...</div>');
          try {
            const response = await fetch(trigger.getAttribute("data-detail-url"), {
              headers: {"X-Requested-With": "ScoreRecordModal"}
            });
            if (!response.ok) throw new Error("HTTP " + response.status);
            openModal(await response.text());
          } catch (error) {
            openModal('<div class="modal-loading">No se pudo cargar el detalle.</div>');
          }
          return;
        }
        if (event.target.closest("[data-modal-close]")) {
          closeModal();
        }
      });

      document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && !modal.hidden) closeModal();
      });
    })();
    </script>
    """


def _league_nav(
    data: ForecastData,
    *,
    base_path: str = "/today",
    extra_params: dict[str, str] | None = None,
) -> str:
    league = data.league_filter
    view_mode = data.view_mode

    def link(target: str, label: str) -> str:
        cls = "btn primary" if league == target else "btn ghost"
        params = {
            "date": data.target_date,
            "view": view_mode,
            "league": target,
            **(extra_params or {}),
        }
        href = f"{base_path}?{_e(urlencode(params))}"
        return f'<a class="{cls}" href="{href}">{_e(label)}</a>'

    links = [link("ALL", "Todo")]
    links.extend(link(code, league_label(code)) for code in GUI_LEAGUE_ORDER)
    return f'<section class="forecast-toolbar league-nav">{"".join(links)}</section>'


def _league_banner(league: str) -> str:
    if league == "ALL":
        return ""
    return f"""
    <div class="league-banner">
      <h2>{_e(league_label(league))} &mdash; FORECAST_ONLY</h2>
      <div>Pronosticos por liga con modelo club-aware. No es recomendacion de apuesta.</div>
    </div>
    """


def _league_match_group(label: str, cards: str) -> str:
    return f"""
    <section class="league-match-group">
      <h2 class="league-group-title">{_e(label)}</h2>
      <div class="match-grid">{cards}</div>
    </section>
    """


def _upcoming_fixture_section(data: ForecastData) -> str:
    if not data.upcoming_fixtures:
        return ""
    rows = "".join(_upcoming_fixture_item(item, data) for item in data.upcoming_fixtures)
    return f"""
    <section class="upcoming-panel">
      <div class="section-head">
        <h2>Próximos partidos cargados</h2>
        <span>{len(data.upcoming_fixtures)} visibles</span>
      </div>
      <div class="upcoming-list">{rows}</div>
    </section>
    """


def _history_tabs(data: ForecastData, active_tab: str) -> str:
    league = data.league_filter
    upcoming_class = "btn primary" if active_tab == "upcoming" else "btn ghost"
    past_class = "btn primary" if active_tab == "past" else "btn ghost"
    return f"""
    <section class="history-tabs" aria-label="Historial de partidos">
      <a class="{upcoming_class}" href="/history?tab=upcoming&league={_e(league)}">
        Próximos partidos cargados
      </a>
      <a class="{past_class}" href="/history?tab=past&league={_e(league)}">
        Todos los partidos pasados
      </a>
    </section>
    """


def _history_upcoming_section(data: ForecastData) -> str:
    if not data.upcoming_fixtures:
        return _empty_forecast_state("No hay próximos partidos cargados para esta liga.")
    rows = "".join(_upcoming_fixture_item(item, data) for item in data.upcoming_fixtures)
    return f"""
    <section class="upcoming-panel history-panel">
      <div class="section-head">
        <h2>Todos los próximos partidos cargados</h2>
        <span>{len(data.upcoming_fixtures)} visibles</span>
      </div>
      <div class="upcoming-list">{rows}</div>
    </section>
    """


def _history_past_section(data: ForecastData) -> str:
    if not data.past_fixtures:
        return _empty_forecast_state("No hay partidos pasados cargados para esta liga.")
    rows = "".join(_history_past_item(item, data) for item in data.past_fixtures)
    return f"""
    <section class="upcoming-panel history-panel">
      <div class="section-head">
        <h2>Todos los partidos pasados</h2>
        <span>{len(data.past_fixtures)} cargados</span>
      </div>
      <div class="history-list">{rows}</div>
    </section>
    """


def _upcoming_fixture_item(item: UpcomingFixtureLink, data: ForecastData) -> str:
    league = item.league_code or "ALL"
    href = (
        f"/today?date={_e(item.display_date)}&view={_e(data.view_mode)}"
        f"&league={_e(data.league_filter)}"
    )
    match_href = (
        f"/match/{_e(item.fixture_id)}?view={_e(data.view_mode)}"
        f"&league={_e(data.league_filter)}"
    )
    modal_href = f"{match_href}&partial=1"
    competition = item.competition or league_label(item.league_code)
    return f"""
    <article class="upcoming-item">
      <a class="upcoming-date" href="{href}">
        <strong>{_e(item.display_date)}</strong>
        <span>{_e(item.display_time)}</span>
      </a>
      <div>
        <small>{_e(competition)} · {_e(league)}</small>
        <a href="{match_href}" data-detail-url="{modal_href}">{_e(item.home)} vs {_e(item.away)}</a>
      </div>
    </article>
    """


def _history_past_item(item: HistoryFixtureLink, data: ForecastData) -> str:
    if item.fixture_id is None:
        competition = item.competition or league_label(item.league_code)
        return f"""
        <article class="history-item">
          <div class="upcoming-date">
            <strong>{_e(item.display_date)}</strong>
            <span>{_e(item.display_time)}</span>
          </div>
          <div>
            <small>{_e(competition)} · {_e(item.league_code or 'ALL')}</small>
            <span>{_e(item.home)} vs {_e(item.away)}</span>
          </div>
          <strong class="score-pill">{_e(item.score or 'Final')}</strong>
          <span class="history-prediction-state">Histórico cargado</span>
        </article>
        """
    match_href = (
        f"/match/{_e(item.fixture_id)}?view={_e(data.view_mode)}"
        f"&league={_e(data.league_filter)}"
    )
    modal_href = f"{match_href}&partial=1"
    competition = item.competition or league_label(item.league_code)
    predicted = "Con predicción" if item.predicted else "Sin predicción"
    return f"""
    <article class="history-item">
      <div class="upcoming-date">
        <strong>{_e(item.display_date)}</strong>
        <span>{_e(item.display_time)}</span>
      </div>
      <div>
        <small>{_e(competition)} · {_e(item.league_code or 'ALL')}</small>
        <a href="{match_href}" data-detail-url="{modal_href}">{_e(item.home)} vs {_e(item.away)}</a>
      </div>
      <strong class="score-pill">{_e(item.score or 'Final')}</strong>
      <span class="history-prediction-state">{_e(predicted)}</span>
    </article>
    """


def render_match_forecast(
    data: ForecastData,
    fixture_id: str,
    *,
    settings: Settings | None = None,
    partial: bool = False,
) -> str:
    fixture = next((item for item in data.fixtures if item.fixture_id == fixture_id), None)
    if fixture is None:
        content = _empty_forecast_state("PARTIDO NO ENCONTRADO")
    else:
        content = _match_detail(
            fixture,
            view_mode=data.view_mode,
            league=data.league_filter,
            embedded=partial,
        )
    if partial:
        return content
    return f"""<!doctype html>
<html lang="es" data-theme="scorerecord">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ScoreRecord Match</title>
  <style>{_forecast_css()}</style>
</head>
<body class="forecast-page">
  {_forecast_nav(data)}
  <main class="forecast-shell">
    {_recreational_warning_banner(settings)}
    {_forecast_warning_banner(data)}
    {content}
  </main>
</body>
</html>"""


def load_dashboard_data(
    conn: sqlite3.Connection,
    *,
    selected_run_id: int | None,
    date_filter: str | None = None,
    market_filter: str | None = None,
) -> DashboardData:
    run_options = _recommendation_runs(conn)
    selected = selected_run_id if _run_exists(run_options, selected_run_id) else None
    if selected is None and run_options:
        selected = int(run_options[0]["run_id"])
    edge_run_id = _edge_run_for_recommendation(conn, selected) if selected is not None else None
    picks = (
        _recommendation_rows(
            conn,
            selected,
            date_filter=date_filter,
            market_filter=market_filter,
        )
        if selected is not None
        else []
    )
    audit_rows = (
        _audit_rows(conn, edge_run_id, date_filter=date_filter, market_filter=market_filter)
        if edge_run_id is not None
        else []
    )
    kpis = _kpis(
        conn,
        selected_run_id=selected,
        edge_run_id=edge_run_id,
        picks=picks,
        date_filter=date_filter,
        market_filter=market_filter,
    )
    edge_sanity = _edge_sanity(conn, edge_run_id) if edge_run_id is not None else {}
    legacy_warning_count = _legacy_warning_count(conn, current_run_id=selected)
    return DashboardData(
        selected_run_id=selected,
        edge_run_id=edge_run_id,
        date_filter=date_filter,
        market_filter=market_filter,
        run_options=run_options,
        picks=picks,
        audit_rows=audit_rows,
        kpis=kpis,
        edge_sanity=edge_sanity,
        legacy_warning_count=legacy_warning_count,
        generated_utc=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def render_dashboard(data: DashboardData, *, refresh_seconds: int) -> str:
    title = "ScoreRecord"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {_auto_refresh_meta(refresh_seconds)}
  <title>{title} Audit</title>
  <style>{_css()}</style>
</head>
<body>
  <header class="topbar">
    <div>
      <div class="brand">ScoreRecord</div>
      <div class="subline">Read-only paper / audit mode</div>
      <a class="button ghost" href="/today">Forecast</a>
    </div>
    <form class="run-form" method="get" action="/audit">
      <label for="run_id">Recommend Run</label>
      <select id="run_id" name="run_id">{_run_options(data)}</select>
      <label for="date">Date</label>
      <input id="date" name="date" value="{_e(data.date_filter)}" placeholder="YYYY-MM-DD">
      <label for="market">Market</label>
      <select id="market" name="market">{_market_options(data.market_filter)}</select>
      <button type="submit">Apply</button>
      <a class="button ghost" href="{_e(_query_url(data, latest=True))}">Latest</a>
      <a class="button ghost" href="{_e(_query_url(data))}">Refresh</a>
    </form>
  </header>
  <main>
    {_status_band(data)}
    {_kpi_grid(data)}
    <section class="legacy-alert">
      <strong>LEGACY AUDIT MODE - NO BETTING RECOMMENDATIONS</strong>
      <span>
        This page is retained for traceability only. Do not use it as an actionable
        betting screen.
      </span>
    </section>
    <section class="section">
      <div class="section-head">
        <div>
          <h1>Legacy Paper Recommendations</h1>
          <p>Deprecated audit view. Not actionable. Do not use for betting decisions.</p>
        </div>
        <span class="badge good">{len(data.picks)} rows</span>
      </div>
      {_picks_table(data.picks)}
    </section>
    <section class="section">
      <div class="section-head">
        <div>
          <h2>Audit-only / Not Actionable</h2>
          <p class="danger-note">
            These rows are for audit only. Do not treat them as actionable picks.
          </p>
        </div>
        <span class="badge bad">{len(data.audit_rows)} rows</span>
      </div>
      {_audit_table(data.audit_rows)}
    </section>
  </main>
</body>
</html>"""


def render_error(exc: Exception) -> str:
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ScoreRecord</title><style>{_css()}</style></head>
<body><main>
<section class="section error"><h1>Dashboard Error</h1><pre>{_e(str(exc))}</pre></section>
</main></body>
</html>"""


def _display_tz(display_tz_name: str | None = None) -> ZoneInfo:
    name = display_tz_name or os.getenv("SCORERECORD_DISPLAY_TZ", DEFAULT_DISPLAY_TZ)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_DISPLAY_TZ)


def _latest_prediction_run_id(
    conn: sqlite3.Connection,
    *,
    fixture_ids: list[str] | None = None,
) -> int | None:
    filter_sql = ""
    params: tuple[Any, ...] = ()
    if fixture_ids:
        placeholders = ",".join("?" for _ in fixture_ids)
        filter_sql = f"AND p.fixture_id IN ({placeholders})"
        params = tuple(fixture_ids)
    row = conn.execute(
        f"""
        SELECT ir.run_id
        FROM ingest_run ir
        JOIN prediction p ON p.run_id = ir.run_id
        WHERE ir.job_name = 'predict-fixtures'
          AND ir.status = 'succeeded'
          {filter_sql}
        GROUP BY ir.run_id
        ORDER BY ir.run_id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row:
        return int(row["run_id"])
    if fixture_ids:
        placeholders = ",".join("?" for _ in fixture_ids)
        fallback = conn.execute(
            f"""
            SELECT MAX(run_id) AS run_id
            FROM prediction
            WHERE run_id IS NOT NULL
              AND fixture_id IN ({placeholders})
            """,
            tuple(fixture_ids),
        ).fetchone()
        return int(fallback["run_id"]) if fallback and fallback["run_id"] is not None else None
    fallback = conn.execute(
        "SELECT MAX(run_id) AS run_id FROM prediction WHERE run_id IS NOT NULL"
    ).fetchone()
    return int(fallback["run_id"]) if fallback and fallback["run_id"] is not None else None


def _forecast_prediction_rows(
    conn: sqlite3.Connection,
    fixture_ids: list[str] | None = None,
) -> list[sqlite3.Row]:
    return _prediction_rows_for_markets(conn, FORECAST_MARKETS, fixture_ids=fixture_ids)


def _result_prediction_rows(
    conn: sqlite3.Connection,
    fixture_ids: list[str] | None = None,
) -> list[sqlite3.Row]:
    return _prediction_rows_for_markets(
        conn,
        RESULT_DIAGNOSTIC_MARKETS,
        fixture_ids=fixture_ids,
    )


def _prediction_rows_for_markets(
    conn: sqlite3.Connection,
    markets: tuple[str, ...],
    *,
    fixture_ids: list[str] | None = None,
) -> list[sqlite3.Row]:
    if fixture_ids is not None and not fixture_ids:
        return []
    market_placeholders = ",".join("?" for _ in markets)
    fixture_filter = ""
    params: list[Any] = [*markets]
    if fixture_ids is not None:
        fixture_placeholders = ",".join("?" for _ in fixture_ids)
        fixture_filter = f"AND p.fixture_id IN ({fixture_placeholders})"
        params.extend(fixture_ids)
    return conn.execute(
        f"""
        WITH latest_fixture_run AS (
          SELECT p.fixture_id, MAX(p.run_id) AS run_id
          FROM prediction p
          JOIN ingest_run ir ON ir.run_id = p.run_id
          WHERE ir.job_name = 'predict-fixtures'
            AND ir.status = 'succeeded'
            AND p.market IN ({market_placeholders})
            {fixture_filter}
          GROUP BY p.fixture_id
        )
        SELECT
          p.fixture_id,
          p.artifact_id,
          p.market,
          p.selection,
          p.prob,
          p.fair_odds,
          p.model_version,
          p.run_id AS prediction_run_id,
          p.generated_utc,
          COALESCE(ma.market_validation_status, 'NOT_TESTED') AS market_validation_status,
          ma.created_utc AS artifact_created_utc,
          ma.trained_until,
          ma.training_rows,
          ma.details_json,
          ma.calibration_json
        FROM prediction p
        JOIN latest_fixture_run latest
          ON latest.fixture_id = p.fixture_id
         AND latest.run_id = p.run_id
        LEFT JOIN model_artifact ma ON ma.artifact_id = p.artifact_id
        WHERE p.market IN ({market_placeholders})
        ORDER BY p.fixture_id, p.market, p.selection
        """,
        (*params, *markets),
    ).fetchall()


def _forecast_snapshot_rows(
    conn: sqlite3.Connection,
    fixture_ids: list[str],
) -> list[sqlite3.Row]:
    if not fixture_ids:
        return []
    placeholders = ",".join("?" for _ in fixture_ids)
    return conn.execute(
        f"""
        SELECT fixture_id, market, selection, prob, signal_code, as_of_utc,
               settled, result_label, correct, settled_utc
        FROM forecast_snapshot
        WHERE fixture_id IN ({placeholders})
        ORDER BY fixture_id, snapshot_id
        """,
        tuple(fixture_ids),
    ).fetchall()


def _forecast_fixture_rows(
    conn: sqlite3.Connection,
    *,
    fixture_id: str | None = None,
    league_filter: str | None = None,
) -> list[sqlite3.Row]:
    fixture_filter = "AND f.fixture_id = ?" if fixture_id else ""
    score_filter = "" if fixture_id else "AND f.home_score IS NULL AND f.away_score IS NULL"
    
    league_sql = ""
    params: tuple[Any, ...] = ()
    if fixture_id:
        params = (fixture_id,)
    elif league_filter and league_filter != "ALL":
        league_sql = "AND f.league_code = ?"
        params = (league_filter,)
    
    return conn.execute(
        f"""
        SELECT
          f.fixture_id,
          f.kickoff_local,
          f.kickoff_utc,
          f.stage,
          f.group_name,
          f.ground,
          f.league_code,
          f.competition,
          f.home_id,
          f.away_id,
          f.home_placeholder,
          f.away_placeholder,
          ht.canonical AS home,
          at.canonical AS away
        FROM fixture f
        LEFT JOIN team ht ON ht.team_id = f.home_id
        LEFT JOIN team at ON at.team_id = f.away_id
        WHERE 1 = 1
          {score_filter}
          {fixture_filter}
          {league_sql}
        ORDER BY COALESCE(f.kickoff_utc, f.kickoff_local), f.fixture_id
        """,
        params,
    ).fetchall()


def _team_ids_from_fixture_rows(rows: list[sqlite3.Row]) -> list[int]:
    team_ids: set[int] = set()
    for row in rows:
        for column in ("home_id", "away_id"):
            if row[column] is not None:
                team_ids.add(int(row[column]))
    return sorted(team_ids)


def _latest_elo_by_team(
    conn: sqlite3.Connection,
    team_ids: list[int],
) -> dict[int, tuple[float, str]]:
    if not team_ids:
        return {}
    placeholders = ",".join("?" for _ in team_ids)
    rows = conn.execute(
        f"""
        WITH latest AS (
          SELECT team_id, MAX(as_of) AS as_of
          FROM team_rating
          WHERE rating_system = 'elo'
            AND team_id IN ({placeholders})
          GROUP BY team_id
        )
        SELECT tr.team_id, tr.rating, tr.as_of
        FROM team_rating tr
        JOIN latest
          ON latest.team_id = tr.team_id
         AND latest.as_of = tr.as_of
        WHERE tr.rating_system = 'elo'
        """,
        tuple(team_ids),
    ).fetchall()
    return {
        int(row["team_id"]): (float(row["rating"]), str(row["as_of"]))
        for row in rows
    }


def _team_history_by_scope(
    conn: sqlite3.Connection,
    fixture_rows: list[sqlite3.Row],
) -> dict[tuple[str, int], TeamHistorySummary]:
    scopes = sorted({str(row["league_code"]) for row in fixture_rows if row["league_code"]})
    if not scopes:
        return {}
    placeholders = ",".join("?" for _ in scopes)
    rows = conn.execute(
        f"""
        WITH team_matches AS (
          SELECT
            tournament,
            home_id AS team_id,
            home_score + away_score AS total_goals,
            CASE WHEN home_score > 0 AND away_score > 0 THEN 1.0 ELSE 0.0 END AS btts
          FROM match_history
          WHERE tournament IN ({placeholders})
          UNION ALL
          SELECT
            tournament,
            away_id AS team_id,
            home_score + away_score AS total_goals,
            CASE WHEN home_score > 0 AND away_score > 0 THEN 1.0 ELSE 0.0 END AS btts
          FROM match_history
          WHERE tournament IN ({placeholders})
        )
        SELECT
          tournament,
          team_id,
          COUNT(*) AS matches,
          AVG(total_goals) AS avg_total_goals,
          AVG(btts) AS btts_rate
        FROM team_matches
        GROUP BY tournament, team_id
        """,
        (*scopes, *scopes),
    ).fetchall()
    return {
        (str(row["tournament"]), int(row["team_id"])): TeamHistorySummary(
            matches=int(row["matches"] or 0),
            avg_total_goals=(
                float(row["avg_total_goals"])
                if row["avg_total_goals"] is not None
                else None
            ),
            btts_rate=(
                float(row["btts_rate"])
                if row["btts_rate"] is not None
                else None
            ),
        )
        for row in rows
    }


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _system_health_items(
    conn: sqlite3.Connection,
    latest_run_id: int | None,
    quality_items: list[ForecastQualityItem],
    now_utc: datetime,
) -> list[SystemHealthItem]:
    quality_by_label = {item.label: item for item in quality_items}
    refresh_run = _latest_run_by_jobs(
        conn,
        ["run-club-pilot-refresh", "run-epl-pilot-refresh", "predict-fixtures"],
    )
    provider_run = _latest_run_by_jobs(
        conn,
        ["run-club-pilot-refresh", "run-epl-pilot-refresh", "ingest-fixtures-odds-api"],
    )
    readiness = _forecast_readiness_health(conn, now_utc)
    calibration = quality_by_label.get("Calibration")
    elo = quality_by_label.get("Elo coverage")
    return [
        SystemHealthItem("DB", "OK", "SQLite readable"),
        SystemHealthItem("GUI", "OK", "request served"),
        _run_health_item("Refresh", refresh_run, now_utc, max_age_hours=30),
        SystemHealthItem(
            "Elo",
            elo.status if elo else "WARN",
            elo.detail if elo else "missing quality row",
        ),
        SystemHealthItem(
            "Calibrators",
            calibration.status if calibration else "WARN",
            calibration.detail if calibration else "missing quality row",
        ),
        readiness,
        _run_health_item("Provider fixtures", provider_run, now_utc, max_age_hours=30),
        SystemHealthItem(
            "Prediction run",
            "OK" if latest_run_id is not None else "WARN",
            f"run {latest_run_id}" if latest_run_id is not None else "no run",
        ),
    ]


def _latest_run_by_jobs(
    conn: sqlite3.Connection,
    job_names: list[str],
) -> sqlite3.Row | None:
    if not job_names:
        return None
    placeholders = ",".join("?" for _ in job_names)
    return conn.execute(
        f"""
        SELECT run_id, job_name, status, started_utc, finished_utc
        FROM ingest_run
        WHERE job_name IN ({placeholders})
        ORDER BY run_id DESC
        LIMIT 1
        """,
        tuple(job_names),
    ).fetchone()


def _run_health_item(
    label: str,
    row: sqlite3.Row | None,
    now_utc: datetime,
    *,
    max_age_hours: float,
) -> SystemHealthItem:
    if row is None:
        return SystemHealthItem(label, "WARN", "no run found")
    status = str(row["status"] or "")
    finished = parse_datetime_utc(row["finished_utc"] or row["started_utc"])
    age = _age_label(finished, now_utc)
    if status == "succeeded" and _within_age(finished, now_utc, max_age_hours):
        health = "OK"
    else:
        health = "WARN"
    return SystemHealthItem(
        label,
        health,
        f"{row['job_name']} run {row['run_id']} {status}; {age}",
    )


def _forecast_readiness_health(
    conn: sqlite3.Connection,
    now_utc: datetime,
) -> SystemHealthItem:
    placeholders = ",".join("?" for _ in GUI_LEAGUE_ORDER)
    row = conn.execute(
        f"""
        SELECT
          COUNT(*) AS future_fixtures,
          SUM(CASE WHEN home_id IS NULL OR away_id IS NULL THEN 1 ELSE 0 END) AS unresolved,
          SUM(CASE WHEN p.fixture_id IS NOT NULL THEN 1 ELSE 0 END) AS with_predictions
        FROM fixture f
        LEFT JOIN (SELECT DISTINCT fixture_id FROM prediction) p
          ON p.fixture_id = f.fixture_id
        WHERE f.league_code IN ({placeholders})
          AND f.home_score IS NULL
          AND f.away_score IS NULL
          AND datetime(f.kickoff_utc) >= datetime(?)
        """,
        (*GUI_LEAGUE_ORDER, now_utc.isoformat()),
    ).fetchone()
    future = int(row["future_fixtures"] or 0) if row else 0
    unresolved = int(row["unresolved"] or 0) if row else 0
    predicted = int(row["with_predictions"] or 0) if row else 0
    ok = future > 0 and unresolved == 0 and predicted == future
    return SystemHealthItem(
        "Readiness",
        "OK" if ok else "WARN",
        f"{predicted}/{future} fixtures predicted; {unresolved} unresolved",
    )


def _within_age(
    timestamp: datetime | None,
    now_utc: datetime,
    max_age_hours: float,
) -> bool:
    if timestamp is None:
        return False
    delta = now_utc - timestamp
    return 0 <= delta.total_seconds() <= max_age_hours * 3600


def _age_label(timestamp: datetime | None, now_utc: datetime) -> str:
    if timestamp is None:
        return "sin timestamp"
    delta_seconds = max(0, int((now_utc - timestamp).total_seconds()))
    minutes = delta_seconds // 60
    if minutes < 1:
        return "hace menos de 1 min"
    if minutes < 60:
        return f"hace {minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"hace {hours} h"
    days = hours // 24
    return f"hace {days} dias"


def _upcoming_fixture_links(
    rows: list[sqlite3.Row],
    *,
    display_tz: ZoneInfo,
    now_utc: datetime,
    selected_date: str,
    limit: int,
) -> list[UpcomingFixtureLink]:
    upcoming: list[UpcomingFixtureLink] = []
    for row in rows:
        kickoff_utc = parse_kickoff_utc(
            kickoff_utc=row["kickoff_utc"],
            kickoff_local=row["kickoff_local"],
        )
        if kickoff_utc is None or kickoff_utc < now_utc:
            continue
        local_dt = kickoff_utc.astimezone(display_tz)
        display_date = local_dt.date().isoformat()
        if display_date == selected_date:
            continue
        upcoming.append(
            UpcomingFixtureLink(
                fixture_id=str(row["fixture_id"]),
                display_date=display_date,
                display_time=local_dt.strftime("%H:%M"),
                league_code=row["league_code"],
                competition=row["competition"],
                home=str(row["home"] or row["home_placeholder"] or "Por definir"),
                away=str(row["away"] or row["away_placeholder"] or "Por definir"),
            )
        )
    upcoming.sort(key=lambda item: (item.display_date, item.display_time, item.fixture_id))
    return upcoming[:limit]


def _fixture_rows_for_local_date(
    rows: list[sqlite3.Row],
    *,
    display_tz: ZoneInfo,
    target_date: str,
) -> list[sqlite3.Row]:
    selected: list[sqlite3.Row] = []
    for row in rows:
        kickoff_utc = parse_kickoff_utc(
            kickoff_utc=row["kickoff_utc"],
            kickoff_local=row["kickoff_local"],
        )
        if kickoff_utc is None:
            continue
        if kickoff_utc.astimezone(display_tz).date().isoformat() == target_date:
            selected.append(row)
    return selected


def _past_fixture_links(
    conn: sqlite3.Connection,
    *,
    display_tz: ZoneInfo,
    now_utc: datetime,
    league_filter: str,
) -> list[HistoryFixtureLink]:
    fixture_links = _past_fixture_links_from_fixture(
        conn,
        display_tz=display_tz,
        now_utc=now_utc,
        league_filter=league_filter,
    )
    historical_links = _past_fixture_links_from_historical_match(
        conn,
        league_filter=league_filter,
    )
    seen: set[tuple[str, str, str, str]] = set()
    merged: list[HistoryFixtureLink] = []
    for item in sorted(
        [*fixture_links, *historical_links],
        key=lambda link: (link.display_date, link.display_time, link.home, link.away),
        reverse=True,
    ):
        key = (
            item.display_date,
            (item.league_code or "").upper(),
            item.home.casefold(),
            item.away.casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged[:1000]


def _past_fixture_links_from_fixture(
    conn: sqlite3.Connection,
    *,
    display_tz: ZoneInfo,
    now_utc: datetime,
    league_filter: str,
) -> list[HistoryFixtureLink]:
    league_sql = ""
    params: list[Any] = [now_utc.isoformat()]
    if league_filter != "ALL":
        league_sql = "AND f.league_code = ?"
        params.append(league_filter)
    rows = conn.execute(
        f"""
        SELECT
          f.fixture_id,
          f.kickoff_local,
          f.kickoff_utc,
          f.league_code,
          f.competition,
          f.home_score,
          f.away_score,
          f.home_placeholder,
          f.away_placeholder,
          ht.canonical AS home,
          at.canonical AS away,
          CASE WHEN p.fixture_id IS NULL THEN 0 ELSE 1 END AS predicted
        FROM fixture f
        LEFT JOIN team ht ON ht.team_id = f.home_id
        LEFT JOIN team at ON at.team_id = f.away_id
        LEFT JOIN (SELECT DISTINCT fixture_id FROM prediction) p
          ON p.fixture_id = f.fixture_id
        WHERE f.home_score IS NOT NULL
          AND f.away_score IS NOT NULL
          AND datetime(f.kickoff_utc) < datetime(?)
          {league_sql}
        ORDER BY datetime(f.kickoff_utc) DESC, f.fixture_id
        """,
        tuple(params),
    ).fetchall()
    past: list[HistoryFixtureLink] = []
    for row in rows:
        kickoff_utc = parse_kickoff_utc(
            kickoff_utc=row["kickoff_utc"],
            kickoff_local=row["kickoff_local"],
        )
        if kickoff_utc is None:
            continue
        local_dt = kickoff_utc.astimezone(display_tz)
        home = str(row["home"] or row["home_placeholder"] or "Por definir")
        away = str(row["away"] or row["away_placeholder"] or "Por definir")
        past.append(
            HistoryFixtureLink(
                fixture_id=str(row["fixture_id"]),
                display_date=local_dt.date().isoformat(),
                display_time=local_dt.strftime("%H:%M"),
                league_code=row["league_code"],
                competition=row["competition"],
                home=home,
                away=away,
                score=f"{int(row['home_score'])}-{int(row['away_score'])}",
                predicted=bool(row["predicted"]),
                source="fixture",
            )
        )
    return past


def _past_fixture_links_from_historical_match(
    conn: sqlite3.Connection,
    *,
    league_filter: str,
) -> list[HistoryFixtureLink]:
    league_codes = _historical_codes_for_gui_filter(league_filter)
    if not league_codes:
        return []
    placeholders = ",".join("?" for _ in league_codes)
    rows = conn.execute(
        f"""
        SELECT
          hm.source,
          hm.source_event_id,
          hm.league_code,
          hm.season,
          hm.match_date,
          hm.home_team,
          hm.away_team,
          hm.home_goals,
          hm.away_goals,
          f.fixture_id,
          CASE WHEN p.fixture_id IS NULL THEN 0 ELSE 1 END AS predicted
        FROM historical_match hm
        LEFT JOIN fixture f
          ON f.source_event_id = hm.source_event_id
         AND f.league_code = ?
        LEFT JOIN (SELECT DISTINCT fixture_id FROM prediction) p
          ON p.fixture_id = f.fixture_id
        WHERE hm.league_code IN ({placeholders})
          AND hm.home_goals IS NOT NULL
          AND hm.away_goals IS NOT NULL
        ORDER BY hm.match_date DESC, hm.source_event_id DESC
        LIMIT 1000
        """,
        (league_filter if league_filter != "ALL" else "", *league_codes),
    ).fetchall()
    links: list[HistoryFixtureLink] = []
    for row in rows:
        internal_code = _gui_league_from_historical_code(str(row["league_code"]))
        links.append(
            HistoryFixtureLink(
                fixture_id=str(row["fixture_id"]) if row["fixture_id"] else None,
                display_date=str(row["match_date"]),
                display_time="Final",
                league_code=internal_code,
                competition=league_label(internal_code),
                home=str(row["home_team"]),
                away=str(row["away_team"]),
                score=f"{int(row['home_goals'])}-{int(row['away_goals'])}",
                predicted=bool(row["predicted"]),
                source=str(row["source"] or "historical_match"),
            )
        )
    return links


def _historical_codes_for_gui_filter(league_filter: str) -> list[str]:
    preferred = {
        "EPL": "E0",
        "LIGA_MX": "MEX",
        "MLS": "USA",
        "ARGENTINA": "ARG",
        "BRASILEIRAO": "BRA",
    }
    if league_filter == "ALL":
        codes: set[str] = set()
        for code in GUI_LEAGUE_ORDER:
            codes.add(code)
            if code in preferred:
                codes.add(preferred[code])
            alias = FOOTBALL_DATA_LEAGUES.get(code)
            if alias:
                codes.add(alias)
        return sorted(codes)
    codes = {league_filter}
    if league_filter in preferred:
        codes.add(preferred[league_filter])
    alias = FOOTBALL_DATA_LEAGUES.get(league_filter)
    if alias:
        codes.add(alias)
    return sorted(codes)


def _gui_league_from_historical_code(value: str) -> str:
    mapping = {
        "E0": "EPL",
        "MEX": "LIGA_MX",
        "USA": "MLS",
        "ARG": "ARGENTINA",
        "BRA": "BRASILEIRAO",
    }
    return mapping.get(value.upper(), value)


def _group_forecast_predictions(
    rows: list[sqlite3.Row],
) -> dict[str, list[ForecastMarket]]:
    grouped: dict[str, list[ForecastMarket]] = {}
    for row in rows:
        fixture_id = str(row["fixture_id"])
        details = _json_dict(row["details_json"])
        training_policy = _json_dict(details.get("training_policy"))
        calibration = _json_dict(row["calibration_json"])
        coefficient = _json_dict(
            _json_dict(calibration.get("coefficients")).get(str(row["market"]))
        )
        grouped.setdefault(fixture_id, []).append(
            ForecastMarket(
                market=str(row["market"]),
                selection=str(row["selection"]),
                prob=float(row["prob"]),
                fair_odds=float(row["fair_odds"]),
                model_version=str(row["model_version"]),
                artifact_id=row["artifact_id"],
                market_validation_status=str(row["market_validation_status"] or "NOT_TESTED"),
                prediction_run_id=(
                    int(row["prediction_run_id"])
                    if row["prediction_run_id"] is not None
                    else None
                ),
                generated_utc=str(row["generated_utc"]) if row["generated_utc"] else None,
                artifact_created_utc=(
                    str(row["artifact_created_utc"])
                    if row["artifact_created_utc"]
                    else None
                ),
                trained_until=str(row["trained_until"]) if row["trained_until"] else None,
                training_rows=(
                    int(row["training_rows"]) if row["training_rows"] is not None else None
                ),
                training_scope=_str_or_none(training_policy.get("training_scope")),
                training_tournament=_str_or_none(
                    training_policy.get("training_tournament")
                    or training_policy.get("league_code")
                ),
                calibrator_scope=_str_or_none(
                    coefficient.get("calibrator_scope")
                    or training_policy.get("calibrator_scope")
                ),
                calibrator_status=_str_or_none(coefficient.get("status")),
            )
        )
    return grouped


def _group_forecast_snapshots(
    rows: list[sqlite3.Row],
) -> dict[str, list[ForecastSnapshotView]]:
    grouped: dict[str, list[ForecastSnapshotView]] = {}
    for row in rows:
        grouped.setdefault(str(row["fixture_id"]), []).append(
            ForecastSnapshotView(
                market=str(row["market"]),
                selection=str(row["selection"]),
                prob=float(row["prob"]),
                signal_code=str(row["signal_code"]),
                as_of_utc=str(row["as_of_utc"]),
                settled=bool(row["settled"]),
                result_label=(
                    int(row["result_label"]) if row["result_label"] is not None else None
                ),
                correct=int(row["correct"]) if row["correct"] is not None else None,
                settled_utc=str(row["settled_utc"]) if row["settled_utc"] else None,
            )
        )
    return grouped


def _forecast_fixture_from_row(
    row: sqlite3.Row,
    predictions: list[ForecastMarket],
    result_predictions: list[ForecastMarket],
    snapshots: list[ForecastSnapshotView],
    odds_coverage: list[OddsMarketCoverage],
    odds_horizons: list[OddsHorizonCoverage],
    paper_picks: list[dict[str, Any]],
    *,
    team_ratings: dict[int, tuple[float, str]],
    team_history: dict[tuple[str, int], TeamHistorySummary],
    display_tz: ZoneInfo,
    selected_date: str | None,
    now_utc: datetime,
) -> ForecastFixture | None:
    kickoff_utc = parse_kickoff_utc(
        kickoff_utc=row["kickoff_utc"],
        kickoff_local=row["kickoff_local"],
    )
    local_dt = kickoff_utc.astimezone(display_tz) if kickoff_utc else None
    display_date = local_dt.date().isoformat() if local_dt else None
    if selected_date is not None and display_date != selected_date:
        return None
    is_resolved = row["home_id"] is not None and row["away_id"] is not None
    is_started = bool(kickoff_utc and now_utc >= kickoff_utc)
    monotonicity_ok = _ou_monotonicity_ok(predictions)
    visible_markets = predictions if is_resolved and monotonicity_ok else []
    result_markets = result_predictions if is_resolved else []
    primary_markets = [] if is_started else select_primary_forecasts(visible_markets)
    primary = primary_markets[0] if primary_markets else None
    state, state_label = _forecast_state(
        is_resolved=is_resolved,
        is_started=is_started,
        has_predictions=bool(predictions),
        monotonicity_ok=monotonicity_ok,
        has_primary_forecast=bool(primary_markets),
    )
    insight = _forecast_insight(
        row,
        predictions=predictions,
        visible_markets=visible_markets,
        primary_markets=primary_markets,
        is_resolved=is_resolved,
        is_started=is_started,
        monotonicity_ok=monotonicity_ok,
        team_ratings=team_ratings,
        team_history=team_history,
    )
    return ForecastFixture(
        fixture_id=str(row["fixture_id"]),
        kickoff_utc=row["kickoff_utc"],
        kickoff_local=str(row["kickoff_local"] or ""),
        display_date=display_date,
        display_time=local_dt.strftime("%H:%M") if local_dt else "hora por definir",
        stage=row["stage"],
        group_name=row["group_name"],
        ground=row["ground"],
        league_code=row["league_code"],
        competition=row["competition"],
        home=str(row["home"] or row["home_placeholder"] or "Por definir"),
        away=str(row["away"] or row["away_placeholder"] or "Por definir"),
        is_resolved=is_resolved,
        is_started=is_started,
        state=state,
        state_label=state_label,
        primary=primary,
        primary_markets=primary_markets,
        markets=visible_markets,
        result_markets=result_markets,
        artifact_id=visible_markets[0].artifact_id if visible_markets else None,
        model_version=visible_markets[0].model_version if visible_markets else None,
        market_validation_status=(
            visible_markets[0].market_validation_status if visible_markets else "NOT_TESTED"
        ),
        ou_monotonicity_ok=monotonicity_ok,
        snapshots=snapshots,
        odds_coverage=odds_coverage,
        odds_horizons=odds_horizons,
        insight=insight,
        paper_picks=paper_picks,
    )


def _forecast_insight(
    row: sqlite3.Row,
    *,
    predictions: list[ForecastMarket],
    visible_markets: list[ForecastMarket],
    primary_markets: list[ForecastMarket],
    is_resolved: bool,
    is_started: bool,
    monotonicity_ok: bool,
    team_ratings: dict[int, tuple[float, str]],
    team_history: dict[tuple[str, int], TeamHistorySummary],
) -> ForecastInsight:
    home_id = int(row["home_id"]) if row["home_id"] is not None else None
    away_id = int(row["away_id"]) if row["away_id"] is not None else None
    scope = str(row["league_code"] or "")
    home_rating = team_ratings.get(home_id) if home_id is not None else None
    away_rating = team_ratings.get(away_id) if away_id is not None else None
    home_history = (
        team_history.get((scope, home_id)) if home_id is not None and scope else None
    )
    away_history = (
        team_history.get((scope, away_id)) if away_id is not None and scope else None
    )
    base_market = (
        primary_markets[0]
        if primary_markets
        else (visible_markets[0] if visible_markets else None)
    )
    no_prediction_reasons = _no_prediction_reasons(
        predictions=predictions,
        is_resolved=is_resolved,
        is_started=is_started,
        monotonicity_ok=monotonicity_ok,
        primary_markets=primary_markets,
        home_id=home_id,
        away_id=away_id,
    )
    quality_score, quality_label, quality_css = _prediction_quality_score(
        primary_markets=primary_markets,
        monotonicity_ok=monotonicity_ok,
        home_rating=home_rating,
        away_rating=away_rating,
        home_history=home_history,
        away_history=away_history,
    )
    uncertainty_points = _heuristic_uncertainty_points(
        primary_markets=primary_markets,
        home_rating=home_rating,
        away_rating=away_rating,
        home_history=home_history,
        away_history=away_history,
        monotonicity_ok=monotonicity_ok,
    )
    return ForecastInsight(
        quality_score=quality_score,
        quality_label=quality_label,
        quality_css_class=quality_css,
        uncertainty_points=uncertainty_points,
        explanation_items=_explanation_items(
            row,
            primary_markets=primary_markets,
            base_market=base_market,
            home_rating=home_rating,
            away_rating=away_rating,
            home_history=home_history,
            away_history=away_history,
        ),
        no_prediction_reasons=no_prediction_reasons,
        evidence_items=_evidence_items(
            base_market=base_market,
            home_rating=home_rating,
            away_rating=away_rating,
            home_history=home_history,
            away_history=away_history,
        ),
    )


def _no_prediction_reasons(
    *,
    predictions: list[ForecastMarket],
    is_resolved: bool,
    is_started: bool,
    monotonicity_ok: bool,
    primary_markets: list[ForecastMarket],
    home_id: int | None,
    away_id: int | None,
) -> list[str]:
    reasons: list[str] = []
    if not is_resolved:
        if home_id is None:
            reasons.append("equipo local sin resolver")
        if away_id is None:
            reasons.append("equipo visitante sin resolver")
    if is_started:
        reasons.append("kickoff ya paso")
    if not predictions:
        reasons.append("sin filas de prediction para este fixture")
    if predictions and not monotonicity_ok:
        reasons.append("OU1.5/OU2.5/OU3.5 no pasan monotonicidad")
    if predictions and monotonicity_ok and not primary_markets and not is_started:
        reasons.append("sin OU2.5 o BTTS principal disponible")
    return reasons


def _prediction_quality_score(
    *,
    primary_markets: list[ForecastMarket],
    monotonicity_ok: bool,
    home_rating: tuple[float, str] | None,
    away_rating: tuple[float, str] | None,
    home_history: TeamHistorySummary | None,
    away_history: TeamHistorySummary | None,
) -> tuple[int, str, str]:
    if not primary_markets:
        return 0, "Sin score", "quality-score-empty"

    signal_delta = max(abs(market.prob - 0.5) for market in primary_markets)
    signal_points = min(25.0, signal_delta / 0.20 * 25.0)
    min_matches = min(
        home_history.matches if home_history else 0,
        away_history.matches if away_history else 0,
    )
    coverage_points = _coverage_points(min_matches)
    elo_points = 20.0 if home_rating and away_rating else 10.0 if home_rating or away_rating else 0.0
    calibrator_points = _calibrator_points(primary_markets)
    monotonicity_points = 10.0 if monotonicity_ok else 0.0
    score = int(
        round(
            signal_points
            + coverage_points
            + elo_points
            + calibrator_points
            + monotonicity_points
        )
    )
    score = max(0, min(100, score))
    if score >= 80:
        return score, "Alta calidad de datos", "quality-score-high"
    if score >= 65:
        return score, "Buena calidad de datos", "quality-score-good"
    if score >= 45:
        return score, "Calidad limitada", "quality-score-limited"
    return score, "Calidad baja", "quality-score-low"


def _coverage_points(min_matches: int) -> float:
    if min_matches >= 200:
        return 25.0
    if min_matches >= 100:
        return 20.0
    if min_matches >= 50:
        return 14.0
    if min_matches > 0:
        return 8.0
    return 0.0


def _calibrator_points(markets: list[ForecastMarket]) -> float:
    statuses = {str(market.calibrator_status or "").upper() for market in markets}
    if statuses and statuses <= {"CALIBRATED"}:
        return 20.0
    if "CALIBRATED" in statuses:
        return 14.0
    if "CALIBRATOR_INSUFFICIENT_DATA" in statuses:
        return 8.0
    return 0.0


def _heuristic_uncertainty_points(
    *,
    primary_markets: list[ForecastMarket],
    home_rating: tuple[float, str] | None,
    away_rating: tuple[float, str] | None,
    home_history: TeamHistorySummary | None,
    away_history: TeamHistorySummary | None,
    monotonicity_ok: bool,
) -> float:
    points = 0.035
    if primary_markets:
        signal_delta = max(abs(market.prob - 0.5) for market in primary_markets)
        if signal_delta < 0.03:
            points += 0.020
        elif signal_delta < 0.08:
            points += 0.010
    else:
        points += 0.040

    min_matches = min(
        home_history.matches if home_history else 0,
        away_history.matches if away_history else 0,
    )
    if min_matches <= 0:
        points += 0.050
    elif min_matches < 50:
        points += 0.040
    elif min_matches < 100:
        points += 0.030
    elif min_matches < 200:
        points += 0.015

    if not (home_rating and away_rating):
        points += 0.030
    if any((market.calibrator_status or "").upper() != "CALIBRATED" for market in primary_markets):
        points += 0.025
    if not monotonicity_ok:
        points += 0.040
    return min(0.14, max(0.03, points))


def _explanation_items(
    row: sqlite3.Row,
    *,
    primary_markets: list[ForecastMarket],
    base_market: ForecastMarket | None,
    home_rating: tuple[float, str] | None,
    away_rating: tuple[float, str] | None,
    home_history: TeamHistorySummary | None,
    away_history: TeamHistorySummary | None,
) -> list[str]:
    items: list[str] = []
    if base_market is not None:
        items.append(f"Pronostico principal: {_forecast_diagnostic_label(base_market)}")
    if home_rating and away_rating:
        elo_diff = home_rating[0] - away_rating[0]
        home = str(row["home"] or row["home_placeholder"] or "Local")
        away = str(row["away"] or row["away_placeholder"] or "Visitante")
        if abs(elo_diff) < 25:
            items.append(f"Elo parejo: {elo_diff:+.0f} para {home}")
        elif elo_diff > 0:
            items.append(f"{home} llega con Elo {elo_diff:+.0f} sobre {away}")
        else:
            items.append(f"{away} llega con Elo {abs(elo_diff):+.0f} sobre {home}")
    if home_history and away_history:
        avg_goals = _mean_ignore_none(home_history.avg_total_goals, away_history.avg_total_goals)
        btts_rate = _mean_ignore_none(home_history.btts_rate, away_history.btts_rate)
        min_matches = min(home_history.matches, away_history.matches)
        if avg_goals is not None:
            items.append(f"Promedio historico combinado: {avg_goals:.2f} goles por partido")
        if btts_rate is not None:
            items.append(f"BTTS historico combinado: {_pct(btts_rate)}")
        items.append(f"Cobertura historica minima por equipo: {min_matches} partidos")
    if primary_markets:
        scopes = sorted({market.calibrator_scope for market in primary_markets if market.calibrator_scope})
        statuses = sorted({market.calibrator_status for market in primary_markets if market.calibrator_status})
        if scopes or statuses:
            items.append(
                "Calibrador: "
                + ("/".join(scopes) if scopes else "sin scope")
                + " "
                + ("/".join(statuses) if statuses else "sin estado")
            )
    return items[:5]


def _evidence_items(
    *,
    base_market: ForecastMarket | None,
    home_rating: tuple[float, str] | None,
    away_rating: tuple[float, str] | None,
    home_history: TeamHistorySummary | None,
    away_history: TeamHistorySummary | None,
) -> list[tuple[str, str]]:
    if base_market is None:
        return [("Estado", "Sin prediction principal")]
    training_scope = base_market.training_tournament or base_market.training_scope or "n/a"
    calibrator = (
        f"{base_market.calibrator_scope or 'n/a'}"
        f" ({base_market.calibrator_status or 'sin estado'})"
    )
    evidence = [
        ("Modelo", base_market.model_version or "n/a"),
        ("Artifact", _short_id(base_market.artifact_id) or "n/a"),
        ("Prediction run", str(base_market.prediction_run_id or "n/a")),
        ("Generado", base_market.generated_utc or "n/a"),
        ("Training", training_scope),
        ("Training rows", str(base_market.training_rows or "n/a")),
        ("Trained until", base_market.trained_until or "n/a"),
        ("Calibrador", calibrator),
        ("Market validation", base_market.market_validation_status or "NOT_TESTED"),
    ]
    if home_rating and away_rating:
        evidence.append(("Elo", f"local {home_rating[0]:.0f} / visitante {away_rating[0]:.0f}"))
    if home_history and away_history:
        evidence.append(("Historial equipos", f"{home_history.matches} / {away_history.matches} partidos"))
    return evidence


def _mean_ignore_none(*values: float | None) -> float | None:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


def _ou_monotonicity_ok(markets: list[ForecastMarket]) -> bool:
    over = {
        market.market: market.prob
        for market in markets
        if market.selection == "over" and market.market in {"OU1.5", "OU2.5", "OU3.5"}
    }
    if not {"OU1.5", "OU2.5", "OU3.5"}.issubset(over):
        return True
    return over["OU1.5"] + 1e-9 >= over["OU2.5"] >= over["OU3.5"] - 1e-9


def _forecast_state(
    *,
    is_resolved: bool,
    is_started: bool,
    has_predictions: bool,
    monotonicity_ok: bool,
    has_primary_forecast: bool,
) -> tuple[str, str]:
    if not is_resolved:
        return "pending", "POR DEFINIR"
    if not has_predictions:
        return "nodata", "SIN DATOS"
    if not monotonicity_ok:
        return "bad", "DATOS INCONSISTENTES"
    if is_started:
        return "stale", "KICKOFF PASADO"
    if not has_primary_forecast:
        return "quiet", "SIN PRONOSTICO"
    return "forecast", "FORECAST_ONLY"


def _forecast_nav(data: ForecastData, *, active: str = "matches") -> str:
    league = getattr(data, "league_filter", "ALL")
    matches_class = "active" if active == "matches" else ""
    history_class = "active" if active == "history" else ""
    health_class = "active" if active == "health" else ""
    signals_class = "active" if active == "signals" else ""
    performance_class = "active" if active == "performance" else ""
    market_class = "active" if active == "market" else ""
    return f"""
    <header class="forecast-nav">
      <div>
        <div class="brand">ScoreRecord</div>
        <div class="subline">pronosticos deportivos</div>
      </div>
      <nav>
        <a class="{matches_class}" href="/today?date={_e(data.target_date)}&view=forecast&league={_e(league)}">Partidos</a>
        <a class="{signals_class}" href="/signals?date={_e(data.target_date)}&league={_e(league)}">Mejores señales</a>
        <div class="nav-dropdown">
          <button class="{history_class}" type="button">Historial</button>
          <div class="nav-dropdown-menu">
            <a href="/history?tab=upcoming&league={_e(league)}">Próximos cargados</a>
            <a href="/history?tab=past&league={_e(league)}">Partidos pasados</a>
          </div>
        </div>
        <a class="{performance_class}" href="/performance?league={_e(league)}">Performance</a>
        <a class="{market_class}" href="/market?league={_e(league)}">Mercado</a>
        <a class="{health_class}" href="/health?date={_e(data.target_date)}&league={_e(league)}">Health</a>
        <span class="badge forecast">FORECAST_ONLY</span>
      </nav>
    </header>
    """


def _forecast_warning_banner(data: ForecastData) -> str:
    artifact = _short_id(data.artifact_id)
    return f"""
    <section class="forecast-alert">
      <strong>FORECAST_ONLY</strong>
      <span>No es recomendacion de apuesta. Solo pronostico y cobertura informativa.</span>
      <span>Market validation: {_e(data.market_validation_status or 'NOT_TESTED')}</span>
      <span>Modelo: {_e(data.model_version or 'sin prediccion')}</span>
      <span>Artifact: {_e(artifact or 'n/a')}</span>
      <span>TZ: {_e(data.display_tz)}</span>
      <span>Mercados de goles: 90 min / tiempo reglamentario.</span>
      <span>Selector: solo OU/BTTS calibrados.</span>
    </section>
    """


def _forecast_quality_panel(data: ForecastData) -> str:
    items = [
        *data.quality_items,
        ForecastQualityItem(
            "Market validation",
            data.market_validation_status or "NOT_TESTED",
            "Model vs market benchmark not completed",
        ),
    ]
    rendered = "".join(
        f"""
        <div class="quality-item {_quality_class(item.status)}">
          <span>{_e(item.label)}</span>
          <strong>{_e(item.status)}</strong>
          <small>{_e(item.detail)}</small>
        </div>
        """
        for item in items
    )
    return f"""
    <section class="quality-panel" aria-label="Model quality panel">
      {rendered}
    </section>
    """


def _system_health_panel(data: ForecastData) -> str:
    if not data.system_health_items:
        return ""
    rows = "".join(
        f"""
        <div class="quality-item {_quality_class(item.status)}">
          <span>{_e(item.label)}</span>
          <strong>{_e(item.status)}</strong>
          <small>{_e(item.detail)}</small>
        </div>
        """
        for item in data.system_health_items
    )
    return f"""
    <section class="quality-panel system-health-panel" aria-label="System health">
      <div class="panel-title">Health operativo</div>
      {rows}
    </section>
    """


def _forecast_stats(data: ForecastData) -> str:
    items = [
        ("Fecha", data.target_date),
        ("Partidos", data.all_fixture_count),
        ("Con pronostico", data.forecastable_count),
        ("Por definir", data.pending_definition_count),
        ("Datos inconsistentes", data.inconsistent_count),
        ("Prediction run", data.latest_run_id),
    ]
    cells = "".join(
        f'<div class="stat"><span>{_e(label)}</span><strong>{_v(value)}</strong></div>'
        for label, value in items
    )
    return f'<section class="forecast-stats">{cells}</section>'



def _render_paper_picks(paper_picks: list[dict[str, Any]]) -> str:
    if not paper_picks:
        return ""

    rows_html = ""
    for p in paper_picks:
        market = p.get("market", "")
        selection = p.get("selection", "")
        bookmaker = p.get("bookmaker", "")
        market_odds = p.get("market_odds")
        model_prob = p.get("model_prob")
        generated_utc = str(p.get("generated_utc", ""))

        rows_html += f'''
        <tr>
            <td>{_e(market)}</td>
            <td>{_e(selection)}</td>
            <td>{_e(bookmaker)}</td>
            <td>{_num(market_odds)}</td>
            <td>{_pct(model_prob)}</td>
            <td>{_e(generated_utc[:16])}</td>
            <td><span class="label">PAPER_ONLY / NO VALIDADO</span></td>
        </tr>'''

    return f'''
    <div class="paper-picks-section">
      <h4>Paper picks recreativos</h4>
      <p class="text-muted"><small>Estos picks son simulados/recreativos. No han demostrado ganarle al mercado.</small></p>
      <table>
        <thead>
          <tr>
            <th>Mercado</th>
            <th>Selección</th>
            <th>Bookmaker</th>
            <th>Cuota</th>
            <th>Prob. Modelo</th>
            <th>Generado</th>
            <th>Estado</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
    </div>
    '''

def _forecast_card(fixture: ForecastFixture, view_mode: str = "all") -> str:
    primary = _primary_forecasts_block(fixture)
    result_estimate = _result_estimate_card(fixture)
    insight = _forecast_card_insight(fixture)
    markets = _forecast_market_chips(fixture.markets[:8])
    paper_html = _render_paper_picks(fixture.paper_picks) if view_mode in ("all", "paper") else ""
    detail_href = (
        f"/match/{_e(fixture.fixture_id)}?view={_e(view_mode)}"
        f"&league={_e(fixture.league_code or 'ALL')}"
    )
    modal_href = f"{detail_href}&partial=1"
    return f"""
    <article class="match-card {fixture.state}">
      <div class="match-card-head">
        <div>
          <div class="kickoff">{_e(fixture.display_time)} - {_e(fixture.display_date or '')}</div>
          <h2>{_e(fixture.home)} <span>vs</span> {_e(fixture.away)}</h2>
          <p>{_e(_fixture_context(fixture))}</p>
        </div>
        <span class="state {fixture.state}">{_e(fixture.state_label)}</span>
      </div>
      {primary}
      {result_estimate}
      {insight}
      {paper_html}
      <div class="market-chips">{markets}</div>
      <div class="card-actions">
        <a class="btn detail-button" href="{detail_href}" data-detail-url="{modal_href}">Detalle</a>
      </div>
    </article>
    """


def _primary_forecasts_block(fixture: ForecastFixture) -> str:
    if not fixture.primary_markets:
        reasons = _no_prediction_reason_html(fixture)
        return f'<div class="primary-forecast empty">{_e(fixture.state_label)}{reasons}</div>'
    items = "".join(
        _primary_forecast_item(market, fixture.insight.uncertainty_points)
        for market in fixture.primary_markets
    )
    return f"""
    <div class="primary-forecast multi">
      <div class="primary-title">Pronósticos principales del modelo</div>
      <div class="primary-list">{items}</div>
    </div>
    """


def _primary_forecast_item(market: ForecastMarket, uncertainty_points: float) -> str:
    signal = forecast_signal_strength(market.prob)
    return f"""
        <div class="primary-pick {signal.css_class}" data-signal="{_e(signal.code)}">
          <span>Pronóstico principal del modelo ({_e(_forecast_family_label(market))})</span>
          <strong>{_e(signal.label)}</strong>
          <em>{_e(_forecast_diagnostic_label(market))}</em>
          <small>{_e(_uncertainty_label(market.prob, uncertainty_points))}</small>
          <small>
            NO VALIDADO CONTRA MERCADO. Úsalo solo como pronóstico recreativo.
            <a href="/market">Ver contexto</a>.
          </small>
        </div>
        """


def _result_estimate_card(fixture: ForecastFixture) -> str:
    if not fixture.primary_markets:
        return ""
    one_x_two = _result_markets_by_selection(fixture, "1X2")
    if not {"home", "draw", "away"}.issubset(one_x_two):
        return ""
    best_selection, best_market = max(
        one_x_two.items(),
        key=lambda item: item[1].prob,
    )
    double_chance = _result_markets_by_selection(fixture, "double_chance")
    best_dc = max(double_chance.values(), key=lambda market: market.prob, default=None)
    dc_line = ""
    if best_dc is not None:
        dc_line = (
            f'<small>Doble oportunidad mas alta: '
            f'{_e(_double_chance_label(best_dc.selection, fixture))} '
            f'{_pct(best_dc.prob)}</small>'
        )
    items = "".join(
        f"""
        <li>
          <span>{_e(_result_selection_label(selection, fixture))}</span>
          <strong>{_pct(market.prob)}</strong>
        </li>
        """
        for selection, market in (
            ("home", one_x_two["home"]),
            ("draw", one_x_two["draw"]),
            ("away", one_x_two["away"]),
        )
    )
    return f"""
    <section class="result-estimate-card" aria-label="Resultado estimado">
      <div>
        <span>Resultado estimado</span>
        <strong>{_e(_result_selection_label(best_selection, fixture))} {_pct(best_market.prob)}</strong>
        <small>Resultado del modelo; calibracion por liga en revision.</small>
        {dc_line}
      </div>
      <ul>{items}</ul>
    </section>
    """


def _forecast_card_insight(fixture: ForecastFixture) -> str:
    insight = fixture.insight
    if not fixture.primary_markets:
        return ""
    factors = "".join(
        f"<li>{_e(item)}</li>"
        for item in insight.explanation_items[:3]
    )
    return f"""
    <section class="prediction-insight {_e(insight.quality_css_class)}">
      <div class="insight-score">
        <div>
          <span>Calidad de datos del pronostico</span>
          <strong>{insight.quality_score}%</strong>
          <small>{_e(insight.quality_label)}</small>
        </div>
        <div class="quality-meter" aria-hidden="true">
          <span style="width:{insight.quality_score}%"></span>
        </div>
      </div>
      <ul class="insight-factors">{factors}</ul>
    </section>
    """


def _no_prediction_reason_html(fixture: ForecastFixture) -> str:
    if not fixture.insight.no_prediction_reasons:
        return ""
    items = "".join(
        f"<li>{_e(reason)}</li>"
        for reason in fixture.insight.no_prediction_reasons
    )
    return f"""
      <div class="no-prediction-reasons">
        <strong>No se genero pronostico</strong>
        <ul>{items}</ul>
      </div>
    """


def _forecast_diagnostic_label(market: ForecastMarket) -> str:
    if market.market.startswith("OU"):
        side = "Over" if market.selection == "over" else "Under"
        return f"{market.market}: {side} {_pct(market.prob)}"
    if market.market == "BTTS":
        side = "Si" if market.selection == "yes" else "No"
        return f"BTTS: {side} {_pct(market.prob)}"
    return f"{market.market}: {market.selection} {_pct(market.prob)}"


def _uncertainty_label(prob: float, uncertainty_points: float) -> str:
    low = max(0.0, prob - uncertainty_points)
    high = min(1.0, prob + uncertainty_points)
    return f"Rango orientativo: {_pct(low)}-{_pct(high)}"


def _signal_label(prob: float) -> str:
    signal = forecast_signal_strength(prob)
    return f"{signal.label} ({signal.code})"


def _quality_class(status: str) -> str:
    return f"quality-{status.lower().replace('_', '-')}"


def _match_detail(
    fixture: ForecastFixture,
    view_mode: str = "all",
    league: str = "ALL",
    *,
    embedded: bool = False,
) -> str:
    snapshots = _forecast_snapshot_section(fixture)
    explanations = _forecast_explanation_section(fixture)
    evidence = _model_evidence_section(fixture)
    result_estimate = _result_estimate_detail_section(fixture)
    back_link = "" if embedded else (
        f'<a class="btn ghost" href="/today?date={_e(fixture.display_date or "")}'
        f'&view={_e(view_mode)}&league={_e(league)}">Volver</a>'
    )
    has_complete_odds = any(cov.status == "COVERAGE_OK" for cov in fixture.odds_coverage)
    if has_complete_odds:
        odds_section = f"""
        <section class="modelo-vs-mercado">
            <h2>Modelo vs mercado</h2>
            <p class="muted" style="font-size: 0.9em; margin-bottom: 10px;">
                <em>Nota: El mercado suele ser una referencia fuerte; esta comparación es \
informativa, no una señal de apuesta.</em>
            </p>
            {_odds_coverage_section(fixture)}
            {_odds_horizon_section(fixture)}
        </section>
        """
    else:
        odds_section = (
            '<p class="muted">Datos de mercado incompletos; comparativa Modelo vs '
            'mercado no disponible.</p>'
        )

    rows = "".join(
        f"""
        <tr>
          <td>{_e(market.market)}</td>
          <td>{_e(_forecast_label(market))}</td>
          <td>{_pct(market.prob)}</td>
          <td>{_e(_signal_label(market.prob))}</td>
          <td>{_num(market.fair_odds)}</td>
          <td>{_e(_short_id(market.artifact_id) or 'n/a')}</td>
        </tr>
        """
        for market in fixture.markets
    )
    if not rows:
        rows = '<tr><td colspan="6" class="empty">No hay mercados de goles para mostrar.</td></tr>'
    return f"""
    <section class="match-detail">
      {back_link}
      <div class="detail-head">
        <div>
          <div class="kickoff">{_e(fixture.display_time)} - {_e(fixture.display_date or '')}</div>
          <h1>{_e(fixture.home)} <span>vs</span> {_e(fixture.away)}</h1>
          <div style="color: var(--txt-secondary); font-size: 0.9rem; margin-bottom: 0.5rem; font-weight: bold;">
            {_e(fixture.competition or fixture.league_code or "Otras Competiciones")}
          </div>
          <p>{_e(_fixture_context(fixture))}</p>
        </div>
        <span class="state {fixture.state}">{_e(fixture.state_label)}</span>
      </div>
      {snapshots}
      {result_estimate}
      {explanations}
      {evidence}
      <table class="forecast-table">
        <thead>
          <tr>
            <th>Mercado</th><th>Seleccion</th><th>Probabilidad</th>
            <th>Fuerza de señal</th>
            <th>Precio teorico del modelo</th><th>Artifact</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <p class="model-price-note">
        El precio teorico sale de 1 / probabilidad del modelo. No es una cuota real,
        no incluye prorroga ni penales, y no es recomendacion de apuesta.
      </p>
      {odds_section}
    </section>
    """


def _forecast_snapshot_section(fixture: ForecastFixture) -> str:
    if not fixture.snapshots:
        return ""
    rows = "".join(
        f"""
        <tr>
          <td>{_e(snapshot.as_of_utc)}</td>
          <td>{_e(snapshot.market)}</td>
          <td>{_e(_snapshot_selection_label(snapshot))}</td>
          <td>{_pct(snapshot.prob)}</td>
          <td>{_e(snapshot.signal_code)}</td>
          <td>{_e(_snapshot_result_label(snapshot))}</td>
        </tr>
        """
        for snapshot in fixture.snapshots
    )
    return f"""
    <section class="snapshot-panel" aria-label="Forecast congelado">
      <h2>Forecast congelado</h2>
      <p>Registro de lo que mostraba la GUI forecast-only antes del kickoff.</p>
      <table class="forecast-table">
        <thead>
          <tr>
            <th>As of UTC</th><th>Mercado</th><th>Seleccion</th>
            <th>Probabilidad</th><th>Señal</th><th>Resultado</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </section>
    """


def _result_estimate_detail_section(fixture: ForecastFixture) -> str:
    one_x_two = _result_markets_by_selection(fixture, "1X2")
    double_chance = _result_markets_by_selection(fixture, "double_chance")
    if not {"home", "draw", "away"}.issubset(one_x_two):
        return ""
    result_rows = "".join(
        f"""
        <tr>
          <td>{_e(_result_selection_label(selection, fixture))}</td>
          <td>{_pct(market.prob)}</td>
          <td>{_num(market.fair_odds)}</td>
        </tr>
        """
        for selection, market in (
            ("home", one_x_two["home"]),
            ("draw", one_x_two["draw"]),
            ("away", one_x_two["away"]),
        )
    )
    dc_rows = "".join(
        f"""
        <tr>
          <td>{_e(_double_chance_label(market.selection, fixture))}</td>
          <td>{_pct(market.prob)}</td>
          <td>{_num(market.fair_odds)}</td>
        </tr>
        """
        for market in sorted(double_chance.values(), key=lambda item: item.selection)
    )
    if not dc_rows:
        dc_rows = '<tr><td colspan="3" class="empty">Sin doble oportunidad disponible.</td></tr>'
    return f"""
    <section class="result-detail-panel" aria-label="Resultado estimado">
      <h2>Resultado estimado y doble oportunidad</h2>
      <p>
        Diagnostico secundario derivado de la matriz de marcadores del modelo.
        La calibracion 1X2 por liga esta en revision; no es recomendacion.
      </p>
      <div class="result-detail-grid">
        <div>
          <h3>Resultado</h3>
          <table class="forecast-table">
            <thead><tr><th>Seleccion</th><th>Probabilidad</th><th>Precio teorico</th></tr></thead>
            <tbody>{result_rows}</tbody>
          </table>
        </div>
        <div>
          <h3>Doble oportunidad</h3>
          <table class="forecast-table">
            <thead><tr><th>Seleccion</th><th>Probabilidad</th><th>Precio teorico</th></tr></thead>
            <tbody>{dc_rows}</tbody>
          </table>
        </div>
      </div>
    </section>
    """


def _forecast_explanation_section(fixture: ForecastFixture) -> str:
    if not fixture.insight.explanation_items:
        return ""
    items = "".join(
        f"<li>{_e(item)}</li>"
        for item in fixture.insight.explanation_items
    )
    return f"""
    <section class="explain-panel" aria-label="Factores principales">
      <h2>Factores principales</h2>
      <p>Reglas simples calculadas desde Elo, historial y calibrador; no usa IA ni cuotas.</p>
      <ul>{items}</ul>
    </section>
    """


def _model_evidence_section(fixture: ForecastFixture) -> str:
    rows = "".join(
        f"""
        <tr>
          <th>{_e(label)}</th>
          <td>{_e(value)}</td>
        </tr>
        """
        for label, value in fixture.insight.evidence_items
    )
    return f"""
    <section class="evidence-panel" aria-label="Evidencia del modelo">
      <h2>Evidencia del modelo</h2>
      <div class="quality-summary {_e(fixture.insight.quality_css_class)}">
        <div>
          <span>Calidad de datos del pronostico</span>
          <strong>{fixture.insight.quality_score}%</strong>
          <small>{_e(fixture.insight.quality_label)}</small>
        </div>
        <div>
          <span>Incertidumbre</span>
          <strong>±{fixture.insight.uncertainty_points * 100:.1f} pp</strong>
          <small>rango orientativo; no intervalo estadistico formal</small>
        </div>
      </div>
      <table class="evidence-table">{rows}</table>
    </section>
    """


def _odds_coverage_section(fixture: ForecastFixture) -> str:
    if not fixture.odds_coverage:
        return ""
    cards = "".join(_odds_coverage_card(item) for item in fixture.odds_coverage)
    return f"""
    <section class="odds-coverage-panel" aria-label="Cobertura de cuotas">
      <h2>Cobertura de cuotas</h2>
      <p>Cobertura informativa. No calcula EV ni recomendación de apuesta.</p>
      <p>Mejor cuota observada por lado; puede venir de casas distintas.</p>
      <div class="odds-coverage-grid">{cards}</div>
    </section>
    """


def _odds_coverage_card(item: OddsMarketCoverage) -> str:
    latest = _e(item.latest_captured_utc or "n/a")
    age = "n/a" if item.age_minutes is None else f"{item.age_minutes:.0f}m"
    sides = "".join(
        f"""
        <li>
          <span>{_e(_odds_side_label(item.market, side.selection))}</span>
          <strong>{_num(side.best_odds) if side.best_odds is not None else 'n/a'}</strong>
          <small>{_e(side.bookmaker or 'n/a')}</small>
        </li>
        """
        for side in item.sides
    )
    if not sides:
        sides = "<li><span>Sin cuotas capturadas.</span></li>"
    return f"""
      <article class="odds-coverage-card {_odds_status_class(item.status)}">
        <div class="odds-coverage-head">
          <h3>{_e(item.market)}</h3>
          <strong>{_e(item.status)}</strong>
        </div>
        <dl>
          <div><dt>Casas</dt><dd>{item.bookmaker_count}</dd></div>
          <div><dt>Mercado completo</dt><dd>{item.complete_bookmaker_count}</dd></div>
          <div><dt>Ultima captura</dt><dd>{latest}</dd></div>
          <div><dt>Edad</dt><dd>{_e(age)}</dd></div>
        </dl>
        <ul>{sides}</ul>
        <p>{_e(item.details)}</p>
      </article>
    """


def _odds_status_class(status: str) -> str:
    return f"odds-{status.lower().replace('_', '-')}"


def _odds_side_label(market: str, selection: str) -> str:
    if market == "OU2.5":
        return f"{selection.title()} 2.5"
    if market == "BTTS":
        return "Yes" if selection == "yes" else "No"
    return selection


def _odds_horizon_section(fixture: ForecastFixture) -> str:
    if not fixture.odds_horizons:
        return ""
    by_key = {(item.market, item.horizon): item for item in fixture.odds_horizons}
    header = "".join(f"<th>{_e(horizon)}</th>" for horizon in DEFAULT_HORIZONS)
    row_chunks = []
    for market in ("OU2.5", "BTTS"):
        cells = "".join(
            _odds_horizon_cell(by_key.get((market, horizon)))
            for horizon in DEFAULT_HORIZONS
        )
        row_chunks.append(
            f"""
        <tr>
          <th>{_e(market)}</th>
          {cells}
        </tr>
        """
        )
    rows = "".join(row_chunks)
    return f"""
    <section class="odds-horizon-panel" aria-label="Cobertura por horizontes">
      <h2>Cobertura por horizontes</h2>
      <p>Observabilidad pre-kickoff. No es cierre oficial, no calcula EV ni recomendación.</p>
      <table class="odds-horizon-table">
        <thead>
          <tr><th>Mercado</th>{header}</tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </section>
    """


def _odds_horizon_cell(item: OddsHorizonCoverage | None) -> str:
    if item is None:
        return '<td class="odds-horizon-cell odds-no-odds"><strong>NO_ODDS</strong></td>'
    snapshot = _short_timestamp(item.selected_snapshot_at)
    return f"""
      <td class="odds-horizon-cell {_odds_status_class(item.status)}">
        <strong>{_e(item.status)}</strong>
        <span>{item.complete_bookmaker_count} casas completas</span>
        <small>{_e(snapshot or 'sin snapshot')}</small>
      </td>
    """


def _short_timestamp(value: str | None) -> str | None:
    if not value:
        return None
    return value.replace("+00:00", "Z")


def _snapshot_selection_label(snapshot: ForecastSnapshotView) -> str:
    market = ForecastMarket(
        market=snapshot.market,
        selection=snapshot.selection,
        prob=snapshot.prob,
        fair_odds=1.0 / snapshot.prob,
        model_version="snapshot",
        artifact_id=None,
        market_validation_status="snapshot",
    )
    return _forecast_label(market)


def _snapshot_result_label(snapshot: ForecastSnapshotView) -> str:
    if not snapshot.settled:
        return "Pendiente"
    outcome = "Correcto" if snapshot.correct else "Incorrecto"
    result = _settled_result_text(snapshot)
    return f"{outcome} ({result})"


def _settled_result_text(snapshot: ForecastSnapshotView) -> str:
    if snapshot.result_label is None:
        return "sin resultado"
    if snapshot.market == "OU2.5":
        return "over" if snapshot.result_label == 1 else "under"
    if snapshot.market == "BTTS":
        return "yes" if snapshot.result_label == 1 else "no"
    return str(snapshot.result_label)


def _forecast_market_chips(markets: list[ForecastMarket]) -> str:
    if not markets:
        return '<span class="muted">Sin mercados visibles.</span>'
    return "".join(
        f'<span class="chip"><b>{_e(_forecast_label(market))}</b> {_pct(market.prob)}</span>'
        for market in markets
    )


def _empty_forecast_state(label: str) -> str:
    return f"""
    <section class="empty-forecast">
      <h1>{_e(label)}</h1>
      <p>No hay fixtures pronosticables para esta vista. Revisa fixture-summary,
      refresh de liga y predict-fixtures antes de interpretar la pantalla.</p>
    </section>
    """


def _forecast_label(market: ForecastMarket | None) -> str:
    if market is None:
        return "Sin pronostico"
    if market.market.startswith("OU"):
        line = market.market.removeprefix("OU")
        direction = "Over" if market.selection == "over" else "Under"
        return f"{direction} {line} goles"
    if market.market == "BTTS":
        return "Ambos anotan: si" if market.selection == "yes" else "Ambos anotan: no"
    return f"{market.market} {market.selection}"


def _result_markets_by_selection(
    fixture: ForecastFixture,
    market_name: str,
) -> dict[str, ForecastMarket]:
    return {
        market.selection: market
        for market in fixture.result_markets
        if market.market == market_name
    }


def _result_selection_label(selection: str, fixture: ForecastFixture) -> str:
    if selection == "home":
        return f"Gana {fixture.home}"
    if selection == "draw":
        return "Empate"
    if selection == "away":
        return f"Gana {fixture.away}"
    return selection


def _double_chance_label(selection: str, fixture: ForecastFixture) -> str:
    if selection == "1X":
        return f"{fixture.home} o empate"
    if selection == "X2":
        return f"{fixture.away} o empate"
    if selection == "12":
        return "Cualquier equipo gana"
    return selection


def _forecast_family_label(market: ForecastMarket) -> str:
    if market.market.startswith("OU"):
        return "GOLES"
    if market.market == "BTTS":
        return "BTTS"
    return market.market


def _fixture_context(fixture: ForecastFixture) -> str:
    parts = [part for part in [fixture.stage, fixture.group_name, fixture.ground] if part]
    return " - ".join(str(part) for part in parts) or (
        fixture.competition or league_label(fixture.league_code)
    )


def _short_id(value: str | None) -> str | None:
    if not value:
        return None
    return value[:12]


def _recommendation_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
          r.run_id,
          r.edge_run_id,
          COUNT(*) AS picks,
          MAX(r.edge) AS max_edge,
          MAX(r.recommendation_score) AS max_score,
          MAX(r.generated_utc) AS generated_utc,
          ir.code_git_sha
        FROM recommendation r
        LEFT JOIN ingest_run ir ON ir.run_id = r.run_id
        WHERE r.edge_run_id IS NOT NULL
          AND r.recommendation_score IS NOT NULL
        GROUP BY r.run_id, r.edge_run_id, ir.code_git_sha
        ORDER BY r.run_id DESC
        LIMIT 20
        """
    ).fetchall()


def _run_exists(run_options: list[sqlite3.Row], run_id: int | None) -> bool:
    return run_id is not None and any(int(row["run_id"]) == run_id for row in run_options)


def _edge_run_for_recommendation(conn: sqlite3.Connection, run_id: int | None) -> int | None:
    if run_id is None:
        return None
    row = conn.execute(
        """
        SELECT edge_run_id
        FROM recommendation
        WHERE run_id = ?
          AND edge_run_id IS NOT NULL
        ORDER BY edge_run_id DESC
        LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    return int(row["edge_run_id"]) if row else None


def _recommendation_rows(
    conn: sqlite3.Connection,
    run_id: int | None,
    *,
    date_filter: str | None,
    market_filter: str | None,
) -> list[sqlite3.Row]:
    if run_id is None:
        return []
    filter_sql, filter_params = _filter_sql("r", date_filter, market_filter)
    return conn.execute(
        f"""
        SELECT
          r.run_id,
          r.edge_run_id,
          r.fixture_id,
          f.kickoff_local,
          ht.canonical AS home,
          at.canonical AS away,
          r.market,
          r.selection,
          r.market_odds,
          r.model_prob,
          r.edge,
          r.edge_prob,
          r.recommendation_score,
          r.stake_units,
          r.capped_stake_units,
          r.confidence,
          r.bookmaker,
          r.odds_quality_flags,
          r.high_edge_reason,
          r.generated_utc,
          ea.raw_implied_prob,
          ea.prob_market_devig,
          ea.market_overround,
          ea.odds_books,
          ea.median_odds,
          ea.odds_ratio_to_median,
          ea.odds_age_minutes,
          ea.close_match,
          ea.elo_diff
        FROM recommendation r
        JOIN fixture f ON f.fixture_id = r.fixture_id
        LEFT JOIN team ht ON ht.team_id = f.home_id
        LEFT JOIN team at ON at.team_id = f.away_id
        LEFT JOIN edge_analysis ea
          ON ea.run_id = r.edge_run_id
         AND ea.fixture_id = r.fixture_id
         AND ea.market = r.market
         AND ea.selection = r.selection
         AND ea.bookmaker = r.bookmaker
         AND ea.captured_utc = r.odds_captured_utc
        WHERE r.run_id = ?
          {filter_sql}
        ORDER BY r.recommendation_score DESC, r.edge DESC
        """,
        (run_id, *filter_params),
    ).fetchall()


def _audit_rows(
    conn: sqlite3.Connection,
    edge_run_id: int,
    *,
    date_filter: str | None,
    market_filter: str | None,
) -> list[sqlite3.Row]:
    filter_sql, filter_params = _filter_sql("ea", date_filter, market_filter)
    return conn.execute(
        f"""
        WITH ranked_audit AS (
        SELECT
          ea.fixture_id,
          f.kickoff_local,
          ht.canonical AS home,
          at.canonical AS away,
          ea.market,
          ea.selection,
          ea.bookmaker,
          ea.odds_decimal,
          ea.prob_model,
          ea.raw_implied_prob,
          ea.prob_market_devig,
          ea.edge_ev,
          ea.edge_prob,
          ea.recommendation_score,
          ea.odds_books,
          ea.median_odds,
          ea.odds_ratio_to_median,
          ea.odds_age_minutes,
          ea.odds_quality_flags,
          ea.high_edge_reason,
          ea.close_match,
          ea.elo_diff,
          COUNT(1) OVER (
            PARTITION BY ea.fixture_id, ea.market, ea.selection
          ) AS duplicate_rows,
          ROW_NUMBER() OVER (
            PARTITION BY ea.fixture_id, ea.market, ea.selection
            ORDER BY
              COALESCE(ea.recommendation_score, ea.edge_ev) DESC,
              ea.edge_ev DESC,
              COALESCE(ea.odds_age_minutes, 999999.0) ASC,
              ea.captured_utc DESC,
              ea.bookmaker ASC
          ) AS logical_rank
        FROM edge_analysis ea
        JOIN fixture f ON f.fixture_id = ea.fixture_id
        LEFT JOIN team ht ON ht.team_id = f.home_id
        LEFT JOIN team at ON at.team_id = f.away_id
        WHERE ea.run_id = ?
          AND (
            ea.edge_ev > ?
            OR COALESCE(ea.odds_quality_flags, '') <> ''
            OR COALESCE(ea.high_edge_reason, '') <> ''
          )
          {filter_sql}
        )
        SELECT *
        FROM ranked_audit
        WHERE logical_rank = 1
        ORDER BY edge_ev DESC, recommendation_score DESC
        LIMIT 80
        """,
        (edge_run_id, DEFAULT_MAX_EDGE, *filter_params),
    ).fetchall()


def _kpis(
    conn: sqlite3.Connection,
    *,
    selected_run_id: int | None,
    edge_run_id: int | None,
    picks: list[sqlite3.Row],
    date_filter: str | None,
    market_filter: str | None,
) -> dict[str, Any]:
    if edge_run_id is None:
        return {}
    filter_sql, filter_params = _filter_sql("ea", date_filter, market_filter)
    edge = conn.execute(
        f"""
        SELECT
          COUNT(*) AS rows_analyzed,
          SUM(CASE WHEN COALESCE(odds_quality_flags, '') = '' THEN 1 ELSE 0 END) AS clean_rows,
          SUM(CASE WHEN COALESCE(odds_quality_flags, '') <> '' THEN 1 ELSE 0 END) AS flagged_rows,
          SUM(CASE WHEN edge_ev > ? THEN 1 ELSE 0 END) AS high_edge_rows,
          MAX(edge_ev) AS max_edge,
          MAX(recommendation_score) AS max_score,
          MAX(odds_age_minutes) AS max_odds_age,
          MAX(ea.generated_utc) AS last_edge_update,
          MAX(ea.captured_utc) AS last_odds_capture
        FROM edge_analysis ea
        JOIN fixture f ON f.fixture_id = ea.fixture_id
        WHERE ea.run_id = ?
          {filter_sql}
        """,
        (DEFAULT_MAX_EDGE, edge_run_id, *filter_params),
    ).fetchone()
    candidate_filter_sql, candidate_filter_params = _filter_sql("ea", date_filter, market_filter)
    candidates = conn.execute(
        f"""
        SELECT COUNT(*) AS candidates
        FROM edge_analysis ea
        JOIN fixture f ON f.fixture_id = ea.fixture_id
        WHERE ea.run_id = ?
          AND ea.edge_ev >= ?
          AND ea.edge_ev <= ?
          AND ea.prob_model >= ?
          AND (ea.odds_age_minutes IS NULL OR ea.odds_age_minutes <= ?)
          AND (ea.odds_books IS NULL OR ea.odds_books >= ?)
          AND COALESCE(ea.odds_quality_flags, '') = ''
          {candidate_filter_sql}
        """,
        (
            edge_run_id,
            DEFAULT_MIN_EDGE,
            DEFAULT_MAX_EDGE,
            DEFAULT_MIN_PROB,
            DEFAULT_MAX_ODDS_AGE_MINUTES,
            DEFAULT_MIN_BOOKS,
            *candidate_filter_params,
        ),
    ).fetchone()
    recommendation = conn.execute(
        """
        SELECT MAX(generated_utc) AS last_recommendation_update
        FROM recommendation
        WHERE run_id = ?
        """,
        (selected_run_id,),
    ).fetchone()
    legacy = _legacy_warning_count(conn, current_run_id=selected_run_id)
    last_edge_update = edge["last_edge_update"] if edge else None
    last_recommendation_update = (
        recommendation["last_recommendation_update"] if recommendation else None
    )
    return {
        "edge_run_id": edge_run_id,
        "recommend_run_id": selected_run_id,
        "rows_analyzed": edge["rows_analyzed"] if edge else 0,
        "candidates": candidates["candidates"] if candidates else 0,
        "recommendations": len(picks),
        "clean_rows": edge["clean_rows"] if edge else 0,
        "flagged_rows": edge["flagged_rows"] if edge else 0,
        "high_edge_rows": edge["high_edge_rows"] if edge else 0,
        "max_edge": edge["max_edge"] if edge else None,
        "max_score": edge["max_score"] if edge else None,
        "max_odds_age": edge["max_odds_age"] if edge else None,
        "last_edge_update": last_edge_update,
        "last_recommendation_update": last_recommendation_update,
        "last_odds_capture": edge["last_odds_capture"] if edge else None,
        "last_data_update": _max_timestamp(last_edge_update, last_recommendation_update),
        "legacy_warning_count": legacy,
    }


def _edge_sanity(conn: sqlite3.Connection, edge_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT details_json
        FROM data_quality
        WHERE run_id = ?
          AND check_name = 'edge_sanity'
          AND metric_name = 'high_edge_alert'
        ORDER BY quality_id DESC
        LIMIT 1
        """,
        (edge_run_id,),
    ).fetchone()
    if not row or not row["details_json"]:
        return {}
    return json.loads(str(row["details_json"]))


def _legacy_warning_count(conn: sqlite3.Connection, *, current_run_id: int | None) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM recommendation
        WHERE (? IS NULL OR run_id <> ?)
          AND (
            edge > ?
            OR edge_run_id IS NULL
            OR recommendation_score IS NULL
            OR COALESCE(odds_quality_flags, '') <> ''
            OR COALESCE(high_edge_reason, '') <> ''
          )
        """,
        (current_run_id, current_run_id, DEFAULT_MAX_EDGE),
    ).fetchone()
    return int(row["n"]) if row else 0


def _run_options(data: DashboardData) -> str:
    if not data.run_options:
        return '<option value="">none</option>'
    options = []
    for row in data.run_options:
        run_id = int(row["run_id"])
        selected = " selected" if run_id == data.selected_run_id else ""
        label = f"#{run_id} / edge #{row['edge_run_id']} / {row['picks']} picks"
        options.append(f'<option value="{run_id}"{selected}>{_e(label)}</option>')
    return "".join(options)


def _market_options(selected_market: str | None) -> str:
    options = [("", "All"), ("1X2", "1X2"), ("OU", "OU"), ("BTTS", "BTTS")]
    return "".join(
        f'<option value="{_e(value)}"{" selected" if value == (selected_market or "") else ""}>'
        f"{_e(label)}</option>"
        for value, label in options
    )


def _status_band(data: DashboardData) -> str:
    status_label, status_message, status_class = _data_quality_status(data)
    high_rows = int(data.edge_sanity.get("high_edge_rows") or 0)
    return f"""
    <section class="status {status_class}">
      <div>
        <strong>Service status: <span class="status-label">OK</span></strong>
        <strong>Data quality: <span class="status-label">{_e(status_label)}</span></strong>
        <span>{_e(status_message)}</span>
        <span>Edge run: {_v(data.edge_run_id)}</span>
        <span>Recommendation run: {_v(data.selected_run_id)}</span>
        <span>Updated: {_format_time(data.kpis.get("last_data_update"))}</span>
        <span>Odds captured: {_format_time(data.kpis.get("last_odds_capture"))}</span>
        <span>High-edge rows: {_v(high_rows)}</span>
        <span>Legacy audit rows: {_v(data.legacy_warning_count)}</span>
      </div>
      <div class="timestamp">{_e(data.generated_utc)}</div>
    </section>
    """


def _kpi_grid(data: DashboardData) -> str:
    items = [
        ("Edge Run", data.kpis.get("edge_run_id")),
        ("Recommend Run", data.kpis.get("recommend_run_id")),
        ("Rows", data.kpis.get("rows_analyzed")),
        ("Candidates", data.kpis.get("candidates")),
        ("Picks", data.kpis.get("recommendations")),
        ("Clean", data.kpis.get("clean_rows")),
        ("Flagged", data.kpis.get("flagged_rows")),
        ("High Edge", data.kpis.get("high_edge_rows")),
        ("Max EV", _pct(data.kpis.get("max_edge"))),
        ("Max Score", _pct(data.kpis.get("max_score"))),
        ("Max Age", _minutes(data.kpis.get("max_odds_age"))),
        ("Edge Update", _format_time(data.kpis.get("last_edge_update"))),
        ("Rec Update", _format_time(data.kpis.get("last_recommendation_update"))),
    ]
    cells = "".join(
        f'<div class="kpi"><span>{_e(label)}</span><strong>{_v(value)}</strong></div>'
        for label, value in items
    )
    return f'<section class="kpis">{cells}</section>'


def _data_quality_status(data: DashboardData) -> tuple[str, str, str]:
    if data.selected_run_id is None or data.edge_run_id is None:
        return ("NO DATA", "No post-guardrail recommendation run found.", "nodata")
    if not _is_today(data.kpis.get("last_data_update")):
        return ("STALE", "The latest selected run was not generated today.", "stale")
    if data.kpis.get("recommendations") == 0:
        return ("WARNING", "No actionable paper picks for the current filters.", "warn")
    if int(data.kpis.get("flagged_rows") or 0) > 0:
        return ("WARNING", "Odds-quality flags detected in the selected edge run.", "warn")
    if int(data.kpis.get("high_edge_rows") or 0) > 0:
        return ("WARNING", "High-edge audit rows detected in the selected edge run.", "warn")
    if data.legacy_warning_count > 0:
        return ("WARNING", "Legacy or pre-guardrail recommendation rows exist for audit.", "warn")
    return ("OK", "Latest post-guardrail paper picks are clean.", "good")


def _picks_table(rows: list[sqlite3.Row]) -> str:
    headers = [
        "Kickoff",
        "Match",
        "Market",
        "Pick",
        "Odds",
        "Model",
        "Raw Imp",
        "Fair Mkt",
        "EV",
        "Score",
        "Model Stake",
        "Capped Stake",
        "Books",
        "Age",
        "Flags",
        "Reason",
    ]
    body = []
    for row in rows:
        body.append(
            f"<tr class='{_pick_row_class(row)}'>"
            f"<td>{_e(row['kickoff_local'])}</td>"
            f"<td>{_match(row)}</td>"
            f"<td>{_e(row['market'])}</td>"
            f"<td>{_e(row['selection'])}</td>"
            f"<td>{_num(row['market_odds'])}</td>"
            f"<td>{_pct(row['model_prob'])}</td>"
            f"<td>{_pct(row['raw_implied_prob'])}</td>"
            f"<td>{_pct(row['prob_market_devig'])}</td>"
            f"<td class='{_edge_class(row['edge'])}'>{_pct(row['edge'])}</td>"
            f"<td>{_pct(row['recommendation_score'])}</td>"
            f"<td>{_pct(row['stake_units'])}</td>"
            f"<td>{_pct(row['capped_stake_units'])}</td>"
            f"<td>{_v(row['odds_books'])}</td>"
            f"<td>{_minutes(row['odds_age_minutes'])}</td>"
            f"<td>{_pill(row['odds_quality_flags'])}</td>"
            f"<td>{_pill(row['high_edge_reason'])}</td>"
            "</tr>"
        )
    return _table(headers, body, empty="No post-guardrail paper picks.")


def _audit_table(rows: list[sqlite3.Row]) -> str:
    headers = [
        "Kickoff",
        "Match",
        "Reason",
        "Flags",
        "Age",
        "Books",
        "Raw Rows",
        "Best Book",
        "Ratio",
        "EV",
        "Score",
        "Market",
        "Pick",
        "Odds",
        "Model",
        "Fair Mkt",
    ]
    body = []
    for row in rows:
        body.append(
            "<tr class='audit-row'>"
            f"<td>{_e(row['kickoff_local'])}</td>"
            f"<td>{_match(row)}</td>"
            f"<td>{_pill(row['high_edge_reason'])}</td>"
            f"<td>{_pill(row['odds_quality_flags'])}</td>"
            f"<td>{_minutes(row['odds_age_minutes'])}</td>"
            f"<td>{_v(row['odds_books'])}</td>"
            f"<td>{_v(row['duplicate_rows'])}</td>"
            f"<td>{_e(row['bookmaker'])}</td>"
            f"<td>{_num(row['odds_ratio_to_median'])}</td>"
            f"<td class='{_edge_class(row['edge_ev'])}'>{_pct(row['edge_ev'])}</td>"
            f"<td>{_pct(row['recommendation_score'])}</td>"
            f"<td>{_e(row['market'])}</td>"
            f"<td>{_e(row['selection'])}</td>"
            f"<td>{_num(row['odds_decimal'])}</td>"
            f"<td>{_pct(row['prob_model'])}</td>"
            f"<td>{_pct(row['prob_market_devig'])}</td>"
            "</tr>"
        )
    return _table(headers, body, empty="No audit-only rows for this edge run.")


def _table(headers: list[str], body: list[str], *, empty: str) -> str:
    head = "".join(f"<th>{_e(header)}</th>" for header in headers)
    if not body:
        body = [f'<tr><td colspan="{len(headers)}" class="empty">{_e(empty)}</td></tr>']
    rows = "".join(body)
    return (
        f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
    )


def _match(row: sqlite3.Row) -> str:
    return f"{_e(row['home'] or 'TBD')} <span class='muted'>vs</span> {_e(row['away'] or 'TBD')}"


def _pick_row_class(row: sqlite3.Row) -> str:
    flags = str(row["odds_quality_flags"] or "")
    reasons = str(row["high_edge_reason"] or "")
    if _has_bad_marker(flags) or _has_bad_marker(reasons):
        return "pick-bad"
    if flags or reasons:
        return "pick-warn"
    return "pick-clean"


def _pill(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return '<span class="pill ok">clear</span>'
    parts = [part.strip() for part in text.split(",") if part.strip()]
    return "".join(f'<span class="pill {_pill_class(part)}">{_e(part)}</span>' for part in parts)


def _pill_class(value: str) -> str:
    if _has_bad_marker(value):
        return "bad"
    return "warn"


def _has_bad_marker(value: str) -> bool:
    markers = {
        "bookmaker_outlier",
        "stale_odds",
        "draw_tail_risk",
        "calibration_tail_risk",
    }
    return any(marker in value for marker in markers)


def _edge_class(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number > DEFAULT_MAX_EDGE:
        return "bad"
    if number > 0:
        return "pos"
    return "neg"


def _pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def _num(value: Any) -> str:
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "n/a"


def _minutes(value: Any) -> str:
    try:
        return f"{float(value):.0f}m"
    except (TypeError, ValueError):
        return "n/a"


def _format_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "n/a"
    return _e(text.replace("T", " ")[:19])


def _v(value: Any) -> str:
    if value is None:
        return "n/a"
    return _e(str(value))


def _auto_refresh_meta(refresh_seconds: int) -> str:
    if refresh_seconds <= 0:
        return ""
    return f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">'


def _e(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _parse_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value: str) -> str | None:
    text = str(value or "").strip()
    if len(text) != 10:
        return None
    if text[4] != "-" or text[7] != "-":
        return None
    yyyy, mm, dd = text.split("-")
    if yyyy.isdigit() and mm.isdigit() and dd.isdigit():
        return text
    return None


def _short_date_label(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%d %b")


def _parse_market(value: str) -> str | None:
    text = str(value or "").strip().upper()
    if text in {"1X2", "OU", "BTTS"}:
        return text
    return None


def _filter_sql(
    row_alias: str,
    date_filter: str | None,
    market_filter: str | None,
) -> tuple[str, tuple[str, ...]]:
    clauses: list[str] = []
    params: list[str] = []
    if date_filter:
        clauses.append("substr(COALESCE(f.kickoff_utc, f.kickoff_local), 1, 10) = ?")
        params.append(date_filter)
    if market_filter == "OU":
        clauses.append(f"{row_alias}.market LIKE 'OU%'")
    elif market_filter:
        clauses.append(f"{row_alias}.market = ?")
        params.append(market_filter)
    if not clauses:
        return "", ()
    return "AND " + "\n          AND ".join(clauses), tuple(params)


def _query_url(data: DashboardData, *, latest: bool = False) -> str:
    params: dict[str, str] = {}
    if data.selected_run_id is not None and not latest:
        params["run_id"] = str(data.selected_run_id)
    if data.date_filter:
        params["date"] = data.date_filter
    if data.market_filter:
        params["market"] = data.market_filter
    query = urlencode(params)
    return "/audit" if not query else f"/audit?{query}"


def _max_timestamp(*values: Any) -> str | None:
    clean = [str(value) for value in values if value]
    return max(clean) if clean else None


def _is_today(value: Any) -> bool:
    text = str(value or "")
    if len(text) < 10:
        return False
    return text[:10] == datetime.now(UTC).date().isoformat()


def _forecast_css() -> str:
    return """
:root {
  color-scheme: light;
  --primary: #34CF72;
  --primary-dark: #13934D;
  --primary-soft: #E8FFF1;
  --secondary: #1C331D;
  --secondary-soft: #EEF6EF;
  --bg: #f2f7f3;
  --surface: #ffffff;
  --surface-2: #edf7f0;
  --ink: #132315;
  --muted: #607066;
  --line: #cfe1d4;
  --green: #13934D;
  --blue: #1C331D;
  --amber: #b7791f;
  --red: #b42318;
  --purple: #6d28d9;
}
* { box-sizing: border-box; }
body.forecast-page {
  margin: 0;
  background:
    radial-gradient(circle at 9% 4%, rgba(52, 207, 114, 0.30), transparent 30%),
    radial-gradient(circle at 92% 10%, rgba(28, 51, 29, 0.20), transparent 34%),
    linear-gradient(135deg, #effcf3 0%, #f7fbf7 38%, #e2f3e5 100%);
  color: var(--ink);
  font: 15px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  min-height: 100vh;
}
body.landing-page {
  display: grid;
  place-items: center;
  padding: 28px 14px;
  background:
    radial-gradient(circle at 16% 20%, rgba(52, 207, 114, 0.36), transparent 28%),
    radial-gradient(circle at 84% 70%, rgba(28, 51, 29, 0.26), transparent 34%),
    linear-gradient(145deg, #dff8e7 0%, #f7fbf7 48%, #d6eddb 100%);
}
.forecast-nav {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  padding: 14px 24px;
  background: rgba(255, 255, 255, 0.92);
  border-bottom: 1px solid rgba(28, 51, 29, 0.12);
  backdrop-filter: blur(14px);
  position: sticky;
  top: 0;
  z-index: 20;
}
.forecast-nav nav { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.forecast-nav a, .forecast-nav button, .btn {
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 9px 13px;
  min-height: 38px;
  color: var(--ink);
  background: var(--surface);
  text-decoration: none;
  font-weight: 700;
  font: inherit;
  cursor: pointer;
}
.forecast-nav a:hover, .btn:hover,
.forecast-nav button:hover,
.forecast-nav a.active, .forecast-nav button.active, .btn.primary {
  border-color: var(--primary);
  color: var(--secondary);
  background: var(--primary-soft);
}
.nav-dropdown, .hero-dropdown {
  position: relative;
  display: inline-flex;
}
.nav-dropdown-menu {
  display: none;
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  min-width: 220px;
  padding: 8px;
  border: 1px solid rgba(28, 51, 29, 0.14);
  border-radius: 12px;
  background: rgba(255,255,255,0.98);
  box-shadow: 0 18px 45px rgba(28, 51, 29, 0.16);
  z-index: 50;
}
.hero-dropdown .nav-dropdown-menu {
  right: auto;
  left: 50%;
  transform: translateX(-50%);
}
.nav-dropdown:hover .nav-dropdown-menu,
.nav-dropdown:focus-within .nav-dropdown-menu,
.hero-dropdown:hover .nav-dropdown-menu,
.hero-dropdown:focus-within .nav-dropdown-menu {
  display: grid;
  gap: 6px;
}
.nav-dropdown-menu a {
  display: block;
  white-space: nowrap;
}
.btn.primary {
  background: var(--primary);
  border-color: var(--primary);
  color: var(--secondary);
}
.brand { font-size: 24px; font-weight: 900; letter-spacing: 0; color: var(--secondary); }
.subline { color: var(--muted); font-size: 13px; margin-top: 2px; }
.forecast-shell { width: min(1180px, 100%); margin: 0 auto; padding: 18px; }
.landing-shell {
  width: min(1180px, 100%);
  margin: 0 auto;
}
.home-hero, .health-hero {
  display: grid;
  grid-template-columns: minmax(280px, 1fr) auto;
  gap: 18px;
  align-items: end;
  margin: 4px 0 16px;
  padding: 24px;
  border: 1px solid rgba(28, 51, 29, 0.12);
  border-radius: 18px;
  background:
    radial-gradient(circle at 7% 0%, rgba(52, 207, 114, 0.24), transparent 36%),
    linear-gradient(135deg, rgba(52, 207, 114, 0.18), rgba(255,255,255,0.96) 42%),
    var(--surface);
  box-shadow: 0 18px 45px rgba(28, 51, 29, 0.08);
}
.landing-hero {
  min-height: min(520px, calc(100vh - 150px));
  align-items: center;
  text-align: center;
  grid-template-columns: 1fr;
  place-items: center;
  padding: clamp(28px, 7vw, 72px);
  border-color: rgba(28, 51, 29, 0.20);
  background:
    radial-gradient(circle at 16% 14%, rgba(52, 207, 114, 0.30), transparent 34%),
    radial-gradient(circle at 88% 78%, rgba(28, 51, 29, 0.22), transparent 38%),
    linear-gradient(145deg, rgba(255,255,255,0.95), rgba(232,255,241,0.92));
  box-shadow: 0 28px 80px rgba(28, 51, 29, 0.16);
}
.health-hero { align-items: center; }
.hero-copy span, .health-hero span {
  color: var(--primary-dark);
  font-size: 12px;
  font-weight: 850;
  text-transform: uppercase;
  letter-spacing: 0;
}
.hero-copy h1, .health-hero h1 {
  margin: 4px 0 6px;
  font-size: clamp(34px, 6vw, 58px);
  line-height: 1;
  color: var(--secondary);
}
.landing-hero .hero-copy h1 {
  font-size: clamp(54px, 11vw, 112px);
}
.health-hero h1 { font-size: clamp(28px, 4vw, 42px); }
.hero-copy p, .health-hero p {
  max-width: 720px;
  color: #405645;
  margin: 0;
}
.landing-hero .hero-copy p {
  margin: 0 auto;
  font-size: clamp(16px, 2vw, 20px);
}
.repo-link {
  display: inline-flex;
  margin-top: 14px;
  color: var(--secondary);
  font-weight: 800;
  text-decoration: none;
  border-bottom: 2px solid rgba(52, 207, 114, 0.65);
}
.repo-link:hover { color: var(--primary-dark); }
.hero-actions {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.landing-hero .hero-actions {
  justify-content: center;
  max-width: 900px;
}
.btn.secondary {
  background: var(--secondary);
  border-color: var(--secondary);
  color: #f3fff6;
}
.btn.secondary:hover {
  background: #294c2b;
  color: #ffffff;
}
.forecast-alert {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 16px;
  align-items: center;
  padding: 12px 14px;
  background: var(--primary-soft);
  border: 1px solid rgba(52, 207, 114, 0.38);
  border-left: 6px solid var(--green);
  border-radius: 10px;
}
.forecast-alert strong { color: var(--green); }
.forecast-alert span { color: #1f3d31; }
.quality-panel {
  display: grid;
  grid-template-columns: repeat(4, minmax(145px, 1fr));
  gap: 10px;
  margin-top: 12px;
}
.quality-item {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px;
}
.quality-item span, .quality-item small {
  display: block;
  color: var(--muted);
  font-size: 12px;
}
.quality-item strong {
  display: block;
  margin: 5px 0;
  font-size: 16px;
}
.quality-ok { border-left: 5px solid var(--green); }
.quality-warn { border-left: 5px solid var(--amber); }
.quality-not-tested { border-left: 5px solid var(--muted); }
.quality-info { border-left: 5px solid var(--blue); }
.system-health-panel .panel-title {
  grid-column: 1 / -1;
  color: var(--muted);
  font-size: 12px;
  font-weight: 850;
  text-transform: uppercase;
}
.legacy-alert {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
  align-items: center;
  margin: 14px 0;
  padding: 12px 14px;
  border: 1px solid #f2c6c6;
  border-left: 6px solid var(--red);
  border-radius: 8px;
  background: #fff5f5;
  color: #7f1d1d;
}
.legacy-alert strong { font-weight: 850; }
.forecast-stats {
  display: grid;
  grid-template-columns: repeat(6, minmax(130px, 1fr));
  gap: 10px;
  margin: 14px 0;
}
.stat {
  padding: 12px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 10px;
  min-height: 74px;
}
.stat span { display: block; color: var(--muted); font-size: 12px; }
.stat strong { display: block; margin-top: 8px; font-size: 20px; }
.forecast-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin: 12px 0;
}
.date-toolbar {
  display: grid;
  gap: 10px;
  margin: 14px 0;
  padding: 12px;
  border: 1px solid rgba(28, 51, 29, 0.14);
  border-radius: 14px;
  background: rgba(255,255,255,0.82);
  box-shadow: 0 10px 30px rgba(28, 51, 29, 0.06);
}
.date-toolbar-main {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.date-chip-row {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding-bottom: 2px;
}
.date-chip {
  display: grid;
  gap: 2px;
  min-width: 106px;
  padding: 9px 11px;
  border: 1px solid rgba(28, 51, 29, 0.14);
  border-radius: 12px;
  color: var(--secondary);
  background: linear-gradient(180deg, #ffffff, #effbf3);
  text-decoration: none;
}
.date-chip strong { font-size: 14px; }
.date-chip span { color: var(--muted); font-size: 12px; font-weight: 700; }
.date-chip.active {
  border-color: var(--primary);
  background: var(--secondary);
  color: #f2fff5;
}
.date-chip.active span { color: #c7f7d8; }
.league-banner, .upcoming-panel {
  margin: 12px 0;
  padding: 12px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-left: 5px solid var(--blue);
  border-radius: 8px;
}
.league-banner h2 { margin: 0 0 4px; color: var(--secondary); }
.league-banner div { color: var(--muted); font-size: 13px; }
.league-match-group {
  margin: 22px 0 28px;
}
.league-group-title {
  margin: 0 0 12px;
  padding: 0 0 8px;
  color: var(--secondary);
  font-size: clamp(20px, 2.5vw, 26px);
  border-bottom: 1px solid rgba(28, 51, 29, 0.16);
}
.section-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
}
.section-head h2 { margin: 0; }
.section-head span { color: var(--muted); font-size: 13px; font-weight: 750; }
.upcoming-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(260px, 1fr));
  gap: 8px;
}
.upcoming-item {
  display: grid;
  grid-template-columns: 96px 1fr;
  gap: 10px;
  align-items: center;
  padding: 9px;
  background: var(--surface-2);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.upcoming-item a { color: var(--ink); text-decoration: none; font-weight: 800; }
.upcoming-item small { display: block; color: var(--muted); font-size: 12px; }
.upcoming-date {
  display: grid;
  gap: 2px;
  color: var(--blue) !important;
}
.upcoming-date span { color: var(--muted); font-weight: 700; }
.history-hero {
  align-items: center;
}
.history-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin: 14px 0;
}
.history-panel {
  border-left-color: var(--secondary);
}
.history-list {
  display: grid;
  gap: 8px;
}
.history-item {
  display: grid;
  grid-template-columns: 96px 1fr auto auto;
  gap: 12px;
  align-items: center;
  padding: 10px;
  background: var(--surface-2);
  border: 1px solid var(--line);
  border-radius: 10px;
}
.history-item a {
  color: var(--ink);
  font-weight: 850;
  text-decoration: none;
}
.history-item small { display: block; color: var(--muted); font-size: 12px; }
.score-pill {
  padding: 6px 10px;
  border-radius: 999px;
  background: var(--secondary);
  color: #f3fff6;
  white-space: nowrap;
}
.history-prediction-state {
  color: var(--muted);
  font-size: 12px;
  font-weight: 800;
  white-space: nowrap;
}
.date-form { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.date-form label { color: var(--muted); font-size: 13px; }
.date-form input {
  min-height: 38px;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 7px 10px;
}
.btn { cursor: pointer; }
.ghost { color: var(--blue); }
.badge, .state {
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  padding: 5px 9px;
  font-size: 12px;
  font-weight: 800;
  border: 1px solid var(--line);
  white-space: nowrap;
}
.badge.forecast { color: var(--green); border-color: #b7dfc7; background: #ecfdf5; }
.match-grid { display: grid; grid-template-columns: repeat(2, minmax(280px, 1fr)); gap: 14px; }
.health-summary-panel {
  margin-top: 14px;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface);
}
.health-summary-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(150px, 1fr));
  gap: 10px;
}
.health-summary-item {
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: #fbfffc;
}
.health-summary-item span {
  display: block;
  color: var(--muted);
  font-size: 12px;
}
.health-summary-item strong {
  display: block;
  margin-top: 6px;
  overflow-wrap: anywhere;
}
.top-signals-panel {
  margin: 14px 0;
  padding: 14px;
  border: 1px solid rgba(28, 51, 29, 0.14);
  border-radius: 14px;
  background:
    linear-gradient(135deg, rgba(255,255,255,0.92), rgba(232,255,241,0.78)),
    var(--surface);
  box-shadow: 0 12px 34px rgba(28, 51, 29, 0.08);
}
.top-signal-list {
  display: grid;
  gap: 10px;
}
.top-signal-item {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 12px;
  align-items: center;
  padding: 12px;
  border: 1px solid var(--line);
  border-left: 5px solid var(--green);
  border-radius: 12px;
  background: rgba(255,255,255,0.86);
}
.top-signal-item.signal-no-signal { border-left-color: var(--muted); }
.top-signal-item.signal-lean { border-left-color: #8bc79e; }
.top-signal-item.signal-moderate { border-left-color: var(--primary); }
.top-signal-item.signal-strong { border-left-color: var(--secondary); }
.top-signal-item span,
.top-signal-item small {
  display: block;
  color: var(--muted);
  font-size: 12px;
}
.top-signal-item strong {
  display: block;
  color: var(--secondary);
  font-size: 17px;
}
.performance-stats {
  grid-template-columns: repeat(3, minmax(150px, 1fr));
}
.market-explainer p {
  color: #405645;
  margin: 8px 0;
}
.market-status-panel .panel-title {
  grid-column: 1 / -1;
  color: var(--secondary);
  font-weight: 900;
}
.match-modal[hidden] { display: none; }
.match-modal {
  position: fixed;
  inset: 0;
  z-index: 80;
  display: grid;
  place-items: center;
  padding: 24px;
}
.match-modal-backdrop {
  position: absolute;
  inset: 0;
  background: rgba(19, 35, 21, 0.45);
  backdrop-filter: blur(6px);
}
.match-modal-panel {
  position: relative;
  width: min(1040px, 100%);
  max-height: min(86vh, 980px);
  overflow: auto;
  border: 1px solid rgba(52, 207, 114, 0.38);
  border-radius: 18px;
  background: var(--surface);
  box-shadow: 0 30px 80px rgba(28, 51, 29, 0.28);
  padding: 16px;
}
.modal-close {
  position: sticky;
  top: 0;
  float: right;
  z-index: 2;
  display: inline-grid;
  place-items: center;
  width: 38px;
  height: 38px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--secondary);
  color: white;
  font-weight: 900;
  cursor: pointer;
}
.modal-loading {
  padding: 28px;
  color: var(--muted);
  font-weight: 800;
}
body.modal-open .forecast-shell,
body.modal-open .forecast-nav {
  filter: blur(4px);
}
.match-card, .match-detail, .empty-forecast {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 14px;
}
.snapshot-panel {
  margin-top: 14px;
  padding: 12px;
  border: 1px solid var(--line);
  border-left: 5px solid var(--blue);
  border-radius: 8px;
  background: #fbfcff;
}
.snapshot-panel h2 { font-size: 18px; margin: 0 0 4px; }
.explain-panel, .evidence-panel {
  margin-top: 14px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
}
.explain-panel { border-left: 5px solid var(--green); }
.evidence-panel { border-left: 5px solid var(--blue); }
.explain-panel h2, .evidence-panel h2 { font-size: 18px; margin: 0 0 4px; }
.explain-panel ul { margin: 10px 0 0; padding-left: 20px; }
.quality-summary {
  display: grid;
  grid-template-columns: repeat(2, minmax(160px, 1fr));
  gap: 10px;
  margin-top: 10px;
}
.quality-summary div {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px;
  background: #fbfcfd;
}
.quality-summary span, .quality-summary small {
  display: block;
  color: var(--muted);
  font-size: 12px;
}
.quality-summary strong {
  display: block;
  font-size: 22px;
  margin: 4px 0;
}
.evidence-table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 12px;
}
.evidence-table th, .evidence-table td {
  border-bottom: 1px solid var(--line);
  padding: 9px;
  text-align: left;
}
.evidence-table th {
  width: 190px;
  color: var(--muted);
  font-size: 12px;
  background: #fbfcfd;
}
.match-card { min-height: 260px; display: flex; flex-direction: column; gap: 12px; }
.match-card.forecast { border-left: 6px solid var(--green); }
.match-card.quiet { border-left: 6px solid var(--blue); }
.match-card.pending { border-left: 6px solid var(--amber); }
.match-card.bad { border-left: 6px solid var(--red); }
.match-card.stale, .match-card.nodata { border-left: 6px solid var(--muted); }
.match-card-head, .detail-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
}
.kickoff { color: var(--muted); font-weight: 750; font-size: 13px; }
h1, h2 { margin: 4px 0; letter-spacing: 0; }
h1 { font-size: 28px; }
h2 { font-size: 21px; }
h1 span, h2 span { color: var(--muted); font-weight: 600; }
p { color: var(--muted); margin: 4px 0 0; }
.state.forecast { color: var(--green); background: #ecfdf5; border-color: #b7dfc7; }
.state.quiet { color: var(--blue); background: #eff6ff; border-color: #bfd1ff; }
.state.pending { color: var(--amber); background: #fff8ed; border-color: #f1c27d; }
.state.bad { color: var(--red); background: #fff4f3; border-color: #f2aaa4; }
.state.stale, .state.nodata { color: var(--muted); background: #f7f8fa; }
.primary-forecast {
  padding: 12px;
  border-radius: 10px;
  border: 1px solid #b7dfc7;
  background: #f4fbf7;
}
.primary-forecast span { color: var(--muted); display: block; font-size: 12px; }
.primary-forecast strong { display: block; font-size: 22px; margin-top: 5px; }
.primary-forecast em {
  display: block;
  font-style: normal;
  color: var(--green);
  font-weight: 850;
  font-size: 18px;
  margin-top: 2px;
}
.primary-forecast.empty { color: var(--muted); border-color: var(--line); background: #fafafa; }
.primary-title {
  color: var(--muted);
  font-size: 12px;
  font-weight: 800;
  text-transform: uppercase;
}
.primary-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(190px, 1fr));
  gap: 10px;
  margin-top: 8px;
}
.primary-pick {
  background: var(--surface);
  border: 1px solid #d6eadc;
  border-radius: 8px;
  padding: 10px;
}
.primary-pick.signal-no-signal {
  border-color: var(--line);
  background: #fafafa;
}
.primary-pick.signal-no-signal strong,
.primary-pick.signal-no-signal em {
  color: var(--muted);
}
.primary-pick.signal-lean { border-color: #bfd1ff; }
.primary-pick.signal-moderate { border-color: #f1c27d; }
.primary-pick.signal-strong { border-color: #b7dfc7; }
.primary-pick small {
  display: block;
  color: var(--muted);
  font-size: 12px;
  margin-top: 7px;
}
.result-estimate-card {
  display: grid;
  grid-template-columns: minmax(180px, 1.1fr) minmax(220px, 1fr);
  gap: 12px;
  padding: 12px;
  border: 1px solid #cfe5d6;
  border-radius: 8px;
  background: linear-gradient(135deg, #fbfffd, #eefbf3);
}
.result-estimate-card span,
.result-estimate-card small {
  color: var(--muted);
  display: block;
  font-size: 12px;
}
.result-estimate-card strong {
  display: block;
  margin: 4px 0;
  color: var(--secondary);
  font-size: 18px;
}
.result-estimate-card ul {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 6px;
}
.result-estimate-card li {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  align-items: center;
  padding: 7px 9px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fff;
}
.result-estimate-card li strong {
  margin: 0;
  font-size: 15px;
}
.result-detail-panel {
  border: 1px solid #cfe5d6;
  border-radius: 8px;
  background: #fbfffd;
  padding: 16px;
  margin-bottom: 16px;
}
.result-detail-panel h2,
.result-detail-panel h3 {
  color: var(--secondary);
}
.result-detail-panel p {
  color: var(--muted);
  margin-top: 0;
}
.result-detail-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}
.prediction-insight {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  padding: 10px;
}
.insight-score {
  display: grid;
  grid-template-columns: minmax(150px, 1fr) minmax(120px, 170px);
  gap: 10px;
  align-items: center;
}
.insight-score span, .insight-score small {
  display: block;
  color: var(--muted);
  font-size: 12px;
}
.insight-score strong {
  display: block;
  font-size: 22px;
}
.quality-meter {
  height: 10px;
  border-radius: 999px;
  background: var(--surface-2);
  overflow: hidden;
}
.quality-meter span {
  display: block;
  height: 100%;
  background: var(--green);
}
.quality-score-good .quality-meter span { background: var(--blue); }
.quality-score-limited .quality-meter span { background: var(--amber); }
.quality-score-low .quality-meter span,
.quality-score-empty .quality-meter span { background: var(--muted); }
.insight-factors {
  margin: 8px 0 0;
  padding-left: 18px;
  color: var(--muted);
  font-size: 13px;
}
.no-prediction-reasons {
  margin-top: 8px;
  color: var(--muted);
  font-size: 13px;
}
.no-prediction-reasons strong {
  color: var(--ink);
  font-size: 13px;
}
.no-prediction-reasons ul { margin: 4px 0 0; padding-left: 18px; }
.market-chips { display: flex; gap: 7px; flex-wrap: wrap; }
.chip {
  display: inline-flex;
  gap: 7px;
  border: 1px solid var(--line);
  background: var(--surface-2);
  border-radius: 999px;
  padding: 6px 9px;
  font-size: 13px;
}
.chip b { font-weight: 750; }
.card-actions { margin-top: auto; }
.forecast-table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 14px;
}
.forecast-table th, .forecast-table td {
  border-bottom: 1px solid var(--line);
  padding: 10px;
  text-align: left;
}
.forecast-table th { color: var(--muted); font-size: 12px; background: #fbfcfd; }
.model-price-note {
  color: var(--muted);
  font-size: 13px;
  margin: 12px 0 0;
}
.odds-coverage-panel {
  margin-top: 18px;
  padding: 12px;
  border: 1px solid var(--line);
  border-left: 5px solid var(--purple);
  border-radius: 8px;
  background: #fbfcff;
}
.odds-coverage-panel h2 { font-size: 18px; margin: 0 0 4px; }
.odds-coverage-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(240px, 1fr));
  gap: 10px;
  margin-top: 10px;
}
.odds-coverage-card {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  padding: 10px;
}
.odds-coverage-card.odds-coverage-ok { border-left: 5px solid var(--green); }
.odds-coverage-card.odds-stale-odds { border-left: 5px solid var(--amber); }
.odds-coverage-card.odds-incomplete-market { border-left: 5px solid var(--red); }
.odds-coverage-card.odds-no-odds { border-left: 5px solid var(--muted); }
.odds-coverage-head {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  align-items: baseline;
}
.odds-coverage-head h3 { margin: 0; font-size: 17px; }
.odds-coverage-head strong {
  font-size: 12px;
  color: var(--muted);
}
.odds-coverage-card dl {
  display: grid;
  grid-template-columns: repeat(2, minmax(120px, 1fr));
  gap: 8px;
  margin: 10px 0;
}
.odds-coverage-card dl div {
  padding: 7px;
  background: var(--surface-2);
  border-radius: 6px;
}
.odds-coverage-card dt {
  color: var(--muted);
  font-size: 12px;
}
.odds-coverage-card dd { margin: 2px 0 0; font-weight: 750; }
.odds-coverage-card ul {
  list-style: none;
  padding: 0;
  margin: 0;
  display: grid;
  gap: 6px;
}
.odds-coverage-card li {
  display: grid;
  grid-template-columns: 1fr auto auto;
  gap: 8px;
  align-items: center;
}
.odds-coverage-card li small { color: var(--muted); }
.odds-horizon-panel {
  margin-top: 18px;
  padding: 12px;
  border: 1px solid var(--line);
  border-left: 5px solid var(--blue);
  border-radius: 8px;
  background: #fbfcff;
}
.odds-horizon-panel h2 { font-size: 18px; margin: 0 0 4px; }
.odds-horizon-table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 10px;
}
.odds-horizon-table th,
.odds-horizon-table td {
  border: 1px solid var(--line);
  padding: 8px;
  text-align: left;
  vertical-align: top;
}
.odds-horizon-table thead th {
  color: var(--muted);
  background: #fbfcfd;
  font-size: 12px;
}
.odds-horizon-cell strong,
.odds-horizon-cell span,
.odds-horizon-cell small {
  display: block;
}
.odds-horizon-cell strong { font-size: 12px; }
.odds-horizon-cell span,
.odds-horizon-cell small {
  color: var(--muted);
  font-size: 12px;
  margin-top: 3px;
}
.odds-horizon-cell.odds-coverage-ok { background: #f4fbf7; }
.odds-horizon-cell.odds-incomplete-market { background: #fff4f3; }
.odds-horizon-cell.odds-no-odds { background: #f7f8fa; }
.empty-forecast { text-align: center; padding: 36px 18px; }
.empty-forecast h1 { margin: 0 0 8px; }
.muted, .empty { color: var(--muted); }
@media (max-width: 900px) {
  .forecast-nav, .match-card-head, .detail-head { flex-direction: column; }
  .home-hero, .health-hero { grid-template-columns: 1fr; align-items: start; }
  .landing-hero { min-height: auto; text-align: left; place-items: start; }
  .hero-actions { justify-content: flex-start; }
  .landing-hero .hero-actions { justify-content: flex-start; }
  .date-toolbar-main { align-items: stretch; }
  .date-toolbar-main .btn, .date-form { width: 100%; }
  .date-form input, .date-form button { flex: 1; min-width: 135px; }
  .quality-panel { grid-template-columns: repeat(2, minmax(130px, 1fr)); }
  .forecast-stats { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
  .health-summary-grid { grid-template-columns: 1fr; }
  .match-grid { grid-template-columns: 1fr; }
  .primary-list { grid-template-columns: 1fr; }
  .result-estimate-card { grid-template-columns: 1fr; }
  .result-detail-grid { grid-template-columns: 1fr; }
  .odds-coverage-grid { grid-template-columns: 1fr; }
  .upcoming-list { grid-template-columns: 1fr; }
  .history-item { grid-template-columns: 1fr; align-items: start; }
  .score-pill, .history-prediction-state { width: fit-content; }
  .nav-dropdown-menu { left: 0; right: auto; }
  .hero-dropdown .nav-dropdown-menu { left: 0; transform: none; }
  .match-modal { padding: 10px; }
  .match-modal-panel { max-height: 92vh; padding: 10px; }
  .quality-summary { grid-template-columns: 1fr; }
}
"""


def _css() -> str:
    return """
:root {
  color-scheme: light;
  --bg: #f6f7f9;
  --surface: #ffffff;
  --ink: #172026;
  --muted: #66717a;
  --line: #d9dee3;
  --teal: #0f766e;
  --amber: #b45309;
  --red: #b42318;
  --green: #157347;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 16px 22px;
  background: var(--surface);
  border-bottom: 1px solid var(--line);
  position: sticky;
  top: 0;
  z-index: 2;
}
.brand { font-size: 20px; font-weight: 800; letter-spacing: 0; }
.subline { color: var(--muted); font-size: 12px; margin-top: 2px; }
.run-form { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
label { color: var(--muted); font-size: 12px; }
select, input, button, .button {
  min-height: 34px;
  border: 1px solid var(--line);
  background: var(--surface);
  color: var(--ink);
  border-radius: 6px;
  padding: 6px 10px;
  text-decoration: none;
}
input { width: 122px; }
button, .button { cursor: pointer; font-weight: 650; }
.ghost { color: var(--teal); }
main { width: min(1500px, 100%); margin: 0 auto; padding: 16px; }
.status {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 14px;
  border: 1px solid var(--line);
  background: var(--surface);
  border-left: 5px solid var(--green);
  border-radius: 8px;
}
.status.warn { border-left-color: var(--amber); }
.status.stale { border-left-color: var(--amber); }
.status.nodata { border-left-color: var(--red); }
.status div:first-child { display: flex; gap: 12px; flex-wrap: wrap; }
.status-label { letter-spacing: 0; }
.timestamp { color: var(--muted); font-size: 12px; }
.kpis {
  display: grid;
  grid-template-columns: repeat(6, minmax(124px, 1fr));
  gap: 10px;
  margin: 14px 0;
}
.kpi {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  min-height: 70px;
  padding: 10px 12px;
}
.kpi span { display: block; color: var(--muted); font-size: 12px; }
.kpi strong { display: block; font-size: 20px; margin-top: 8px; }
.section {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  margin-top: 14px;
}
.section-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  padding: 13px 14px;
  border-bottom: 1px solid var(--line);
}
h1, h2 { margin: 0; font-size: 16px; letter-spacing: 0; }
.section-head p {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 12px;
}
.danger-note { color: var(--red) !important; font-weight: 700; }
.badge {
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 4px 8px;
  font-size: 12px;
  color: var(--muted);
}
.badge.good { color: var(--green); border-color: #b7dfc7; }
.badge.warn { color: var(--amber); border-color: #f1c27d; }
.badge.bad { color: var(--red); border-color: #f2aaa4; }
.table-wrap { overflow-x: auto; }
table {
  width: 100%;
  border-collapse: collapse;
  min-width: 1180px;
}
th, td {
  text-align: left;
  border-bottom: 1px solid var(--line);
  padding: 8px 10px;
  vertical-align: top;
  white-space: nowrap;
}
th {
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  background: #fbfcfd;
}
tr:hover td { background: #f9fbfb; }
tr.pick-clean td:first-child { border-left: 4px solid var(--green); }
tr.pick-warn td:first-child { border-left: 4px solid var(--amber); }
tr.pick-bad td:first-child,
tr.audit-row td:first-child { border-left: 4px solid var(--red); }
tr.audit-row td { background: #fffafa; }
.muted { color: var(--muted); }
.pos { color: var(--green); font-weight: 700; }
.neg { color: var(--red); font-weight: 700; }
.bad { color: var(--red); font-weight: 800; }
.pill {
  display: inline-block;
  margin: 0 3px 3px 0;
  border-radius: 999px;
  padding: 2px 7px;
  font-size: 12px;
  border: 1px solid var(--line);
}
.pill.ok { color: var(--green); border-color: #b7dfc7; background: #f4fbf7; }
.pill.warn { color: var(--amber); border-color: #f1c27d; background: #fff8ed; }
.pill.bad { color: var(--red); border-color: #f2aaa4; background: #fff4f3; }
.empty { color: var(--muted); text-align: center; padding: 24px; }
.error { padding: 16px; }
pre { white-space: pre-wrap; }
@media (max-width: 950px) {
  .topbar { align-items: flex-start; flex-direction: column; }
  .kpis { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
  .status { align-items: flex-start; flex-direction: column; }
}
"""
