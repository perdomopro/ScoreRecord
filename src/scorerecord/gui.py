"""Read-only audit dashboard for ScoreRecord paper recommendations."""

from __future__ import annotations

import html
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from scorerecord.db import connect, init_db


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MIN_EDGE = 0.05
DEFAULT_MAX_EDGE = 0.25
DEFAULT_MIN_PROB = 0.15
DEFAULT_MAX_ODDS_AGE_MINUTES = 180.0
DEFAULT_MIN_BOOKS = 2


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
            if parsed.path not in {"/", "/index.html"}:
                self.send_error(404)
                return

            params = parse_qs(parsed.query)
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
        generated_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def render_dashboard(data: DashboardData, *, refresh_seconds: int) -> str:
    title = "ScoreRecord"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="{int(refresh_seconds)}">
  <title>{title}</title>
  <style>{_css()}</style>
</head>
<body>
  <header class="topbar">
    <div>
      <div class="brand">ScoreRecord</div>
      <div class="subline">Read-only paper / audit mode</div>
    </div>
    <form class="run-form" method="get" action="/">
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
          ea.elo_diff
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
        ORDER BY ea.edge_ev DESC, ea.recommendation_score DESC
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
    status_label, status_message, status_class = _pipeline_status(data)
    high_rows = int(data.edge_sanity.get("high_edge_rows") or 0)
    return f"""
    <section class="status {status_class}">
      <div>
        <strong>Pipeline status: <span class="status-label">{_e(status_label)}</span></strong>
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


def _pipeline_status(data: DashboardData) -> tuple[str, str, str]:
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
        "Stake",
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
    return "/" if not query else f"/?{query}"


def _max_timestamp(*values: Any) -> str | None:
    clean = [str(value) for value in values if value]
    return max(clean) if clean else None


def _is_today(value: Any) -> bool:
    text = str(value or "")
    if len(text) < 10:
        return False
    return text[:10] == datetime.now(timezone.utc).date().isoformat()


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
