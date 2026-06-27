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
from urllib.parse import parse_qs, urlparse

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
            try:
                with connect(db_path) as conn:
                    data = load_dashboard_data(conn, selected_run_id=selected_run_id)
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
) -> DashboardData:
    run_options = _recommendation_runs(conn)
    selected = selected_run_id if _run_exists(run_options, selected_run_id) else None
    if selected is None and run_options:
        selected = int(run_options[0]["run_id"])
    edge_run_id = _edge_run_for_recommendation(conn, selected) if selected is not None else None
    picks = _recommendation_rows(conn, selected) if selected is not None else []
    audit_rows = _audit_rows(conn, edge_run_id) if edge_run_id is not None else []
    kpis = _kpis(conn, selected_run_id=selected, edge_run_id=edge_run_id, picks=picks)
    edge_sanity = _edge_sanity(conn, edge_run_id) if edge_run_id is not None else {}
    legacy_warning_count = _legacy_warning_count(conn, current_run_id=selected)
    return DashboardData(
        selected_run_id=selected,
        edge_run_id=edge_run_id,
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
      <div class="subline">Paper / audit mode</div>
    </div>
    <form class="run-form" method="get" action="/">
      <label for="run_id">Recommend Run</label>
      <select id="run_id" name="run_id">{_run_options(data)}</select>
      <button type="submit">Apply</button>
      <a class="button ghost" href="/">Latest</a>
    </form>
  </header>
  <main>
    {_status_band(data)}
    {_kpi_grid(data)}
    <section class="section">
      <div class="section-head">
        <h1>Actionable Paper Picks</h1>
        <span class="badge">{len(data.picks)} rows</span>
      </div>
      {_picks_table(data.picks)}
    </section>
    <section class="section">
      <div class="section-head">
        <h2>Audit Only</h2>
        <span class="badge warn">{len(data.audit_rows)} rows</span>
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


def _recommendation_rows(conn: sqlite3.Connection, run_id: int | None) -> list[sqlite3.Row]:
    if run_id is None:
        return []
    return conn.execute(
        """
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
        ORDER BY r.recommendation_score DESC, r.edge DESC
        """,
        (run_id,),
    ).fetchall()


def _audit_rows(conn: sqlite3.Connection, edge_run_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
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
        ORDER BY ea.edge_ev DESC, ea.recommendation_score DESC
        LIMIT 80
        """,
        (edge_run_id, DEFAULT_MAX_EDGE),
    ).fetchall()


def _kpis(
    conn: sqlite3.Connection,
    *,
    selected_run_id: int | None,
    edge_run_id: int | None,
    picks: list[sqlite3.Row],
) -> dict[str, Any]:
    if edge_run_id is None:
        return {}
    edge = conn.execute(
        """
        SELECT
          COUNT(*) AS rows_analyzed,
          SUM(CASE WHEN COALESCE(odds_quality_flags, '') = '' THEN 1 ELSE 0 END) AS clean_rows,
          SUM(CASE WHEN COALESCE(odds_quality_flags, '') <> '' THEN 1 ELSE 0 END) AS flagged_rows,
          SUM(CASE WHEN edge_ev > ? THEN 1 ELSE 0 END) AS high_edge_rows,
          MAX(edge_ev) AS max_edge,
          MAX(recommendation_score) AS max_score,
          MAX(odds_age_minutes) AS max_odds_age
        FROM edge_analysis
        WHERE run_id = ?
        """,
        (DEFAULT_MAX_EDGE, edge_run_id),
    ).fetchone()
    candidates = conn.execute(
        """
        SELECT COUNT(*) AS candidates
        FROM edge_analysis
        WHERE run_id = ?
          AND edge_ev >= ?
          AND edge_ev <= ?
          AND prob_model >= ?
          AND (odds_age_minutes IS NULL OR odds_age_minutes <= ?)
          AND (odds_books IS NULL OR odds_books >= ?)
          AND COALESCE(odds_quality_flags, '') = ''
        """,
        (
            edge_run_id,
            DEFAULT_MIN_EDGE,
            DEFAULT_MAX_EDGE,
            DEFAULT_MIN_PROB,
            DEFAULT_MAX_ODDS_AGE_MINUTES,
            DEFAULT_MIN_BOOKS,
        ),
    ).fetchone()
    legacy = _legacy_warning_count(conn, current_run_id=selected_run_id)
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


def _status_band(data: DashboardData) -> str:
    severity = str(data.edge_sanity.get("severity") or "ok")
    high_rows = int(data.edge_sanity.get("high_edge_rows") or 0)
    legacy = data.legacy_warning_count
    status_class = "good" if high_rows == 0 and legacy == 0 else "warn"
    return f"""
    <section class="status {status_class}">
      <div>
        <strong>Run #{_v(data.selected_run_id)}</strong>
        <span>Edge #{_v(data.edge_run_id)}</span>
        <span>Edge sanity: {_e(severity)}</span>
        <span>High-edge rows: {_v(high_rows)}</span>
        <span>Legacy audit rows: {_v(legacy)}</span>
      </div>
      <div class="timestamp">{_e(data.generated_utc)}</div>
    </section>
    """


def _kpi_grid(data: DashboardData) -> str:
    items = [
        ("Rows", data.kpis.get("rows_analyzed")),
        ("Candidates", data.kpis.get("candidates")),
        ("Picks", data.kpis.get("recommendations")),
        ("Clean", data.kpis.get("clean_rows")),
        ("Flagged", data.kpis.get("flagged_rows")),
        ("High Edge", data.kpis.get("high_edge_rows")),
        ("Max EV", _pct(data.kpis.get("max_edge"))),
        ("Max Score", _pct(data.kpis.get("max_score"))),
        ("Max Age", _minutes(data.kpis.get("max_odds_age"))),
    ]
    cells = "".join(
        f'<div class="kpi"><span>{_e(label)}</span><strong>{_v(value)}</strong></div>'
        for label, value in items
    )
    return f'<section class="kpis">{cells}</section>'


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
            "<tr>"
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
            f"<td>{_num(row['stake_units'])}</td>"
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
        "Market",
        "Pick",
        "Odds",
        "Model",
        "Fair Mkt",
        "EV",
        "Score",
        "Books",
        "Age",
        "Ratio",
        "Flags",
        "Reason",
    ]
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td>{_e(row['kickoff_local'])}</td>"
            f"<td>{_match(row)}</td>"
            f"<td>{_e(row['market'])}</td>"
            f"<td>{_e(row['selection'])}</td>"
            f"<td>{_num(row['odds_decimal'])}</td>"
            f"<td>{_pct(row['prob_model'])}</td>"
            f"<td>{_pct(row['prob_market_devig'])}</td>"
            f"<td class='{_edge_class(row['edge_ev'])}'>{_pct(row['edge_ev'])}</td>"
            f"<td>{_pct(row['recommendation_score'])}</td>"
            f"<td>{_v(row['odds_books'])}</td>"
            f"<td>{_minutes(row['odds_age_minutes'])}</td>"
            f"<td>{_num(row['odds_ratio_to_median'])}</td>"
            f"<td>{_pill(row['odds_quality_flags'])}</td>"
            f"<td>{_pill(row['high_edge_reason'])}</td>"
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


def _pill(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return '<span class="pill ok">clear</span>'
    parts = [part.strip() for part in text.split(",") if part.strip()]
    return "".join(f'<span class="pill warn">{_e(part)}</span>' for part in parts)


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
select, button, .button {
  min-height: 34px;
  border: 1px solid var(--line);
  background: var(--surface);
  color: var(--ink);
  border-radius: 6px;
  padding: 6px 10px;
  text-decoration: none;
}
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
.status div:first-child { display: flex; gap: 12px; flex-wrap: wrap; }
.timestamp { color: var(--muted); font-size: 12px; }
.kpis {
  display: grid;
  grid-template-columns: repeat(9, minmax(104px, 1fr));
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
.badge {
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 4px 8px;
  font-size: 12px;
  color: var(--muted);
}
.badge.warn { color: var(--amber); border-color: #f1c27d; }
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
.empty { color: var(--muted); text-align: center; padding: 24px; }
.error { padding: 16px; }
pre { white-space: pre-wrap; }
@media (max-width: 950px) {
  .topbar { align-items: flex-start; flex-direction: column; }
  .kpis { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
  .status { align-items: flex-start; flex-direction: column; }
}
"""
