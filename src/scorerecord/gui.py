"""Read-only audit dashboard for ScoreRecord paper recommendations."""

from __future__ import annotations

import html
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scorerecord.db import connect, init_db
from scorerecord.time_utils import parse_kickoff_utc

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MIN_EDGE = 0.05
DEFAULT_MAX_EDGE = 0.25
DEFAULT_MIN_PROB = 0.15
DEFAULT_MAX_ODDS_AGE_MINUTES = 180.0
DEFAULT_MIN_BOOKS = 2
DEFAULT_DISPLAY_TZ = "America/Mexico_City"
FORECAST_MARKETS = ("OU1.5", "OU2.5", "OU3.5", "BTTS")
FORECAST_MIN_PROB = 0.60


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
    home: str
    away: str
    is_resolved: bool
    is_started: bool
    state: str
    state_label: str
    primary: ForecastMarket | None
    markets: list[ForecastMarket]
    artifact_id: str | None
    model_version: str | None
    market_validation_status: str
    ou_monotonicity_ok: bool


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
    empty_state: str | None


def serve_dashboard(
    db_path: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    refresh_seconds: int = 60,
) -> None:
    with connect(db_path) as conn:
        init_db(conn)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                self._write("ok\n", content_type="text/plain; charset=utf-8")
                return

            params = parse_qs(parsed.query)
            mode = str(params.get("mode", [""])[0]).strip().lower()
            if parsed.path in {"/", "/index.html"} and mode != "audit":
                self._render_today(params)
                return
            if parsed.path == "/today":
                self._render_today(params)
                return
            if parsed.path.startswith("/match/"):
                fixture_id = parsed.path.removeprefix("/match/").strip("/")
                self._render_match(fixture_id)
                return
            if parsed.path == "/audit" or mode == "audit":
                self._render_audit(params)
                return
            self.send_error(404)

        def _render_today(self, params: dict[str, list[str]]) -> None:
            date_filter = _parse_date(params.get("date", [""])[0])
            try:
                with connect(db_path) as conn:
                    data = load_forecast_data(conn, target_date=date_filter)
                body = render_today_forecast(data, refresh_seconds=refresh_seconds)
            except Exception as exc:  # pragma: no cover - defensive web path
                body = render_error(exc)
            self._write(body)

        def _render_match(self, fixture_id: str) -> None:
            try:
                with connect(db_path) as conn:
                    data = load_forecast_data(conn, fixture_id=fixture_id)
                body = render_match_forecast(data, fixture_id)
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
        ) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer((host, port), Handler)
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
) -> ForecastData:
    display_tz = _display_tz(display_tz_name)
    now = now_utc or datetime.now(UTC)
    now_local = now.astimezone(display_tz)
    selected_date = target_date or now_local.date().isoformat()
    previous_date = (date.fromisoformat(selected_date) - timedelta(days=1)).isoformat()
    next_date = (date.fromisoformat(selected_date) + timedelta(days=1)).isoformat()
    latest_run_id = _latest_prediction_run_id(conn)
    prediction_rows = _forecast_prediction_rows(conn, latest_run_id)
    predictions_by_fixture = _group_forecast_predictions(prediction_rows)
    rows = _forecast_fixture_rows(conn, fixture_id=fixture_id)
    fixtures: list[ForecastFixture] = []
    for row in rows:
        fixture = _forecast_fixture_from_row(
            row,
            predictions_by_fixture.get(str(row["fixture_id"]), []),
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
        empty_state = "SIN PARTIDOS"
    elif not any(fixture.markets for fixture in fixtures):
        empty_state = "SIN PREDICCIONES"
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
        empty_state=empty_state,
    )


def render_today_forecast(data: ForecastData, *, refresh_seconds: int) -> str:
    cards = "".join(_forecast_card(fixture) for fixture in data.fixtures)
    if not cards:
        cards = _empty_forecast_state(data.empty_state or "SIN PARTIDOS")
    return f"""<!doctype html>
<html lang="es" data-theme="scorerecord">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="{int(refresh_seconds)}">
  <title>ScoreRecord Forecast</title>
  <style>{_forecast_css()}</style>
</head>
<body class="forecast-page">
  {_forecast_nav(data)}
  <main class="forecast-shell">
    {_forecast_warning_banner(data)}
    {_forecast_stats(data)}
    <section class="forecast-toolbar">
      <a class="btn ghost" href="/today?date={_e(data.previous_date)}">Anterior</a>
      <form method="get" action="/today" class="date-form">
        <label for="date">Fecha</label>
        <input id="date" name="date" type="date" value="{_e(data.target_date)}">
        <button class="btn" type="submit">Ver</button>
      </form>
      <a class="btn ghost" href="/today?date={_e(data.next_date)}">Siguiente</a>
      <a class="btn ghost" href="/audit">Auditoria</a>
    </section>
    <section class="match-grid">{cards}</section>
  </main>
</body>
</html>"""


def render_match_forecast(data: ForecastData, fixture_id: str) -> str:
    fixture = next((item for item in data.fixtures if item.fixture_id == fixture_id), None)
    if fixture is None:
        content = _empty_forecast_state("PARTIDO NO ENCONTRADO")
    else:
        content = _match_detail(fixture)
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
  <meta http-equiv="refresh" content="{int(refresh_seconds)}">
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
    <section class="section">
      <div class="section-head">
        <div>
          <h1>Actionable Recommendations</h1>
          <p>Paper-only picks ranked by recommendation score.</p>
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


def _latest_prediction_run_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        """
        SELECT ir.run_id
        FROM ingest_run ir
        JOIN prediction p ON p.run_id = ir.run_id
        WHERE ir.job_name = 'predict-fixtures'
          AND ir.status = 'succeeded'
        GROUP BY ir.run_id
        ORDER BY ir.run_id DESC
        LIMIT 1
        """
    ).fetchone()
    if row:
        return int(row["run_id"])
    fallback = conn.execute(
        "SELECT MAX(run_id) AS run_id FROM prediction WHERE run_id IS NOT NULL"
    ).fetchone()
    return int(fallback["run_id"]) if fallback and fallback["run_id"] is not None else None


def _forecast_prediction_rows(
    conn: sqlite3.Connection,
    run_id: int | None,
) -> list[sqlite3.Row]:
    if run_id is None:
        return []
    placeholders = ",".join("?" for _ in FORECAST_MARKETS)
    return conn.execute(
        f"""
        SELECT
          p.fixture_id,
          p.artifact_id,
          p.market,
          p.selection,
          p.prob,
          p.fair_odds,
          p.model_version,
          COALESCE(ma.market_validation_status, 'NOT_TESTED') AS market_validation_status
        FROM prediction p
        LEFT JOIN model_artifact ma ON ma.artifact_id = p.artifact_id
        WHERE p.run_id = ?
          AND p.market IN ({placeholders})
        ORDER BY p.fixture_id, p.market, p.selection
        """,
        (run_id, *FORECAST_MARKETS),
    ).fetchall()


def _forecast_fixture_rows(
    conn: sqlite3.Connection,
    *,
    fixture_id: str | None,
) -> list[sqlite3.Row]:
    fixture_filter = "AND f.fixture_id = ?" if fixture_id else ""
    params = (fixture_id,) if fixture_id else ()
    return conn.execute(
        f"""
        SELECT
          f.fixture_id,
          f.kickoff_local,
          f.kickoff_utc,
          f.stage,
          f.group_name,
          f.ground,
          f.home_id,
          f.away_id,
          f.home_placeholder,
          f.away_placeholder,
          ht.canonical AS home,
          at.canonical AS away
        FROM fixture f
        LEFT JOIN team ht ON ht.team_id = f.home_id
        LEFT JOIN team at ON at.team_id = f.away_id
        WHERE f.home_score IS NULL
          AND f.away_score IS NULL
          {fixture_filter}
        ORDER BY COALESCE(f.kickoff_utc, f.kickoff_local), f.fixture_id
        """,
        params,
    ).fetchall()


def _group_forecast_predictions(
    rows: list[sqlite3.Row],
) -> dict[str, list[ForecastMarket]]:
    grouped: dict[str, list[ForecastMarket]] = {}
    for row in rows:
        fixture_id = str(row["fixture_id"])
        grouped.setdefault(fixture_id, []).append(
            ForecastMarket(
                market=str(row["market"]),
                selection=str(row["selection"]),
                prob=float(row["prob"]),
                fair_odds=float(row["fair_odds"]),
                model_version=str(row["model_version"]),
                artifact_id=row["artifact_id"],
                market_validation_status=str(row["market_validation_status"] or "NOT_TESTED"),
            )
        )
    return grouped


def _forecast_fixture_from_row(
    row: sqlite3.Row,
    predictions: list[ForecastMarket],
    *,
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
    primary = None if is_started else _select_primary_forecast(visible_markets)
    state, state_label = _forecast_state(
        is_resolved=is_resolved,
        is_started=is_started,
        has_predictions=bool(predictions),
        monotonicity_ok=monotonicity_ok,
        primary=primary,
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
        home=str(row["home"] or row["home_placeholder"] or "Por definir"),
        away=str(row["away"] or row["away_placeholder"] or "Por definir"),
        is_resolved=is_resolved,
        is_started=is_started,
        state=state,
        state_label=state_label,
        primary=primary,
        markets=visible_markets,
        artifact_id=visible_markets[0].artifact_id if visible_markets else None,
        model_version=visible_markets[0].model_version if visible_markets else None,
        market_validation_status=(
            visible_markets[0].market_validation_status if visible_markets else "NOT_TESTED"
        ),
        ou_monotonicity_ok=monotonicity_ok,
    )


def _ou_monotonicity_ok(markets: list[ForecastMarket]) -> bool:
    over = {
        market.market: market.prob
        for market in markets
        if market.selection == "over" and market.market in {"OU1.5", "OU2.5", "OU3.5"}
    }
    if not {"OU1.5", "OU2.5", "OU3.5"}.issubset(over):
        return True
    return over["OU1.5"] + 1e-9 >= over["OU2.5"] >= over["OU3.5"] - 1e-9


def _select_primary_forecast(markets: list[ForecastMarket]) -> ForecastMarket | None:
    candidates = [market for market in markets if market.prob >= FORECAST_MIN_PROB]
    if not candidates:
        return None
    return max(candidates, key=lambda market: (market.prob, _market_rank(market.market)))


def _forecast_state(
    *,
    is_resolved: bool,
    is_started: bool,
    has_predictions: bool,
    monotonicity_ok: bool,
    primary: ForecastMarket | None,
) -> tuple[str, str]:
    if not is_resolved:
        return "pending", "POR DEFINIR"
    if not has_predictions:
        return "nodata", "SIN DATOS"
    if not monotonicity_ok:
        return "bad", "DATOS INCONSISTENTES"
    if is_started:
        return "stale", "KICKOFF PASADO"
    if primary is None:
        return "quiet", "SIN PRONOSTICO"
    return "forecast", "FORECAST_ONLY"


def _forecast_nav(data: ForecastData) -> str:
    return f"""
    <header class="forecast-nav">
      <div>
        <div class="brand">ScoreRecord</div>
        <div class="subline">Mundial / selecciones - pronostico estadistico</div>
      </div>
      <nav>
        <a href="/today?date={_e(data.target_date)}">Hoy</a>
        <a href="/audit">Auditoria</a>
        <span class="badge forecast">FORECAST_ONLY</span>
      </nav>
    </header>
    """


def _forecast_warning_banner(data: ForecastData) -> str:
    artifact = _short_id(data.artifact_id)
    return f"""
    <section class="forecast-alert">
      <strong>FORECAST_ONLY</strong>
      <span>No es recomendacion de apuesta. No usa EV, Kelly ni edge.</span>
      <span>Market validation: {_e(data.market_validation_status or 'NOT_TESTED')}</span>
      <span>Modelo: {_e(data.model_version or 'sin prediccion')}</span>
      <span>Artifact: {_e(artifact or 'n/a')}</span>
      <span>TZ: {_e(data.display_tz)}</span>
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


def _forecast_card(fixture: ForecastFixture) -> str:
    primary = (
        f"""
        <div class="primary-forecast">
          <span>Pronostico principal</span>
          <strong>{_forecast_label(fixture.primary)}</strong>
          <em>{_pct(fixture.primary.prob)}</em>
        </div>
        """
        if fixture.primary
        else f'<div class="primary-forecast empty">{_e(fixture.state_label)}</div>'
    )
    markets = _forecast_market_chips(fixture.markets[:8])
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
      <div class="market-chips">{markets}</div>
      <div class="card-actions">
        <a class="btn" href="/match/{_e(fixture.fixture_id)}">Detalle</a>
      </div>
    </article>
    """


def _match_detail(fixture: ForecastFixture) -> str:
    rows = "".join(
        f"""
        <tr>
          <td>{_e(market.market)}</td>
          <td>{_e(_forecast_label(market))}</td>
          <td>{_pct(market.prob)}</td>
          <td>{_num(market.fair_odds)}</td>
          <td>{_e(_short_id(market.artifact_id) or 'n/a')}</td>
        </tr>
        """
        for market in fixture.markets
    )
    if not rows:
        rows = '<tr><td colspan="5" class="empty">No hay mercados de goles para mostrar.</td></tr>'
    return f"""
    <section class="match-detail">
      <a class="btn ghost" href="/today?date={_e(fixture.display_date or '')}">Volver</a>
      <div class="detail-head">
        <div>
          <div class="kickoff">{_e(fixture.display_time)} - {_e(fixture.display_date or '')}</div>
          <h1>{_e(fixture.home)} <span>vs</span> {_e(fixture.away)}</h1>
          <p>{_e(_fixture_context(fixture))}</p>
        </div>
        <span class="state {fixture.state}">{_e(fixture.state_label)}</span>
      </div>
      <table class="forecast-table">
        <thead>
          <tr>
            <th>Mercado</th><th>Seleccion</th><th>Probabilidad</th>
            <th>Cuota justa</th><th>Artifact</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </section>
    """


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
      ingest-worldcup y predict-fixtures antes de interpretar la pantalla.</p>
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


def _fixture_context(fixture: ForecastFixture) -> str:
    parts = [part for part in [fixture.stage, fixture.group_name, fixture.ground] if part]
    return " - ".join(str(part) for part in parts) or "Fixture mundialista"


def _market_rank(market: str) -> int:
    order = {market_name: index for index, market_name in enumerate(FORECAST_MARKETS)}
    return -order.get(market, 999)


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
  --bg: #f4f6f8;
  --surface: #ffffff;
  --surface-2: #eef2f6;
  --ink: #101820;
  --muted: #64717d;
  --line: #d8e0e7;
  --green: #0f7a55;
  --blue: #2563eb;
  --amber: #b7791f;
  --red: #b42318;
  --purple: #6d28d9;
}
* { box-sizing: border-box; }
body.forecast-page {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font: 15px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.forecast-nav {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  padding: 16px 22px;
  background: var(--surface);
  border-bottom: 1px solid var(--line);
  position: sticky;
  top: 0;
  z-index: 5;
}
.forecast-nav nav { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.forecast-nav a, .btn {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 8px 11px;
  min-height: 38px;
  color: var(--ink);
  background: var(--surface);
  text-decoration: none;
  font-weight: 700;
}
.forecast-nav a:hover, .btn:hover { border-color: var(--blue); color: var(--blue); }
.brand { font-size: 22px; font-weight: 850; letter-spacing: 0; }
.subline { color: var(--muted); font-size: 13px; margin-top: 2px; }
.forecast-shell { width: min(1180px, 100%); margin: 0 auto; padding: 18px; }
.forecast-alert {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 16px;
  align-items: center;
  padding: 12px 14px;
  background: #ecfdf5;
  border: 1px solid #b7dfc7;
  border-left: 6px solid var(--green);
  border-radius: 10px;
}
.forecast-alert strong { color: var(--green); }
.forecast-alert span { color: #1f3d31; }
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
.match-card, .match-detail, .empty-forecast {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 14px;
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
.empty-forecast { text-align: center; padding: 36px 18px; }
.empty-forecast h1 { margin: 0 0 8px; }
.muted, .empty { color: var(--muted); }
@media (max-width: 900px) {
  .forecast-nav, .match-card-head, .detail-head { flex-direction: column; }
  .forecast-stats { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
  .match-grid { grid-template-columns: 1fr; }
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
