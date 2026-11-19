"""Page contexts: turn data-layer objects into what each template needs."""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from datetime import date, datetime, timedelta

from scorerecord.health import db_health_checks
from scorerecord.league_config import GUI_LEAGUE_ORDER, league_label
from scorerecord.web import data
from scorerecord.web.data import MatchView

RESULT_WINDOWS = (7, 14, 30, 90)


def league_tabs(selected: str | None) -> list[dict[str, object]]:
    tabs = [{"code": None, "label": "Todas", "active": selected is None}]
    tabs += [
        {"code": code, "label": league_label(code), "active": selected == code}
        for code in GUI_LEAGUE_ORDER
    ]
    return tabs


def group_by_league(matches: list[MatchView]) -> list[tuple[str, list[MatchView]]]:
    groups: OrderedDict[str, list[MatchView]] = OrderedDict()
    order = {code: index for index, code in enumerate(GUI_LEAGUE_ORDER)}
    for match in sorted(matches, key=lambda m: (order.get(m.league_code, 99), m.kickoff_utc)):
        groups.setdefault(match.league_code, []).append(match)
    return [(league_label(code), rows) for code, rows in groups.items()]


def home(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    requested_day: date | None,
    league: str | None,
) -> dict[str, object]:
    today = now.astimezone(data.display_tz()).date()
    days = data.match_days(
        conn, league=league, start_utc=now - timedelta(days=2), end_utc=now + timedelta(days=21)
    )
    selected = requested_day
    if selected is None:
        upcoming = [day for day, _ in days if day >= today]
        selected = upcoming[0] if upcoming else today
    start, end = data.local_day_bounds(selected)
    matches = data.load_matches(conn, start_utc=start, end_utc=end, league=league, now_utc=now)
    strip = [
        {"day": day, "count": count, "active": day == selected, "past": day < today}
        for day, count in days
        if day >= today - timedelta(days=1)
    ]
    if not any(item["active"] for item in strip):
        strip.append(
            {"day": selected, "count": len(matches), "active": True, "past": selected < today}
        )
        strip.sort(key=lambda item: item["day"])
    return {
        "page": "home",
        "selected_day": selected,
        "today": today,
        "strip": strip[:10],
        "groups": group_by_league(matches),
        "count": len(matches),
        "with_market": sum(1 for m in matches if m.uses_market),
        "league": league,
        "tabs": league_tabs(league),
        "previous_day": selected - timedelta(days=1),
        "next_day": selected + timedelta(days=1),
    }


def match(conn: sqlite3.Connection, *, fixture_id: str, now: datetime) -> dict[str, object] | None:
    rows = data.load_matches(conn, fixture_id=fixture_id, now_utc=now)
    if not rows:
        return None
    view = rows[0]
    ref = view.kickoff_utc.date()
    return {
        "page": "match",
        "m": view,
        "form_home": data.team_form(conn, view.home_id, view.league_code, ref),
        "form_away": data.team_form(conn, view.away_id, view.league_code, ref),
        "h2h": data.head_to_head(conn, view.home_id, view.away_id, ref),
        "elo_home": data.elo_history(conn, view.home_id),
        "elo_away": data.elo_history(conn, view.away_id),
        "provenance": data.model_provenance(conn, view.artifact_id),
        "market_weight": data.MARKET_WEIGHT,
    }


def results(
    conn: sqlite3.Connection, *, now: datetime, days: int, league: str | None
) -> dict[str, object]:
    matches = data.load_matches(
        conn, start_utc=now - timedelta(days=days), end_utc=now, league=league, now_utc=now
    )
    finished = [m for m in matches if m.finished and m.model is not None]
    finished.sort(key=lambda m: m.kickoff_utc, reverse=True)
    pending = [m for m in matches if not m.finished and m.status == "pending_result"]
    summary = []
    for market, label in (
        ("1X2", "Resultado"),
        ("OU2.5", "Más/Menos 2.5"),
        ("BTTS", "Ambos anotan"),
    ):
        verdicts = [v for m in finished for v in m.verdicts if v.market == market]
        hits = sum(1 for v in verdicts if v.correct)
        summary.append(
            {
                "label": label,
                "n": len(verdicts),
                "hits": hits,
                "rate": hits / len(verdicts) if verdicts else None,
            }
        )
    days_grouped: OrderedDict[date, list[MatchView]] = OrderedDict()
    for item in finished:
        days_grouped.setdefault(item.kickoff_local.date(), []).append(item)
    return {
        "page": "results",
        "days": days,
        "windows": RESULT_WINDOWS,
        "league": league,
        "tabs": league_tabs(league),
        "summary": summary,
        "by_day": list(days_grouped.items()),
        "count": len(finished),
        "pending": len(pending),
        "without_forecast": sum(1 for m in matches if m.finished and m.model is None),
    }


def performance(
    conn: sqlite3.Connection, *, now: datetime, league: str | None
) -> dict[str, object]:
    matches = data.load_matches(conn, end_utc=now, league=league, finished=True, now_utc=now)
    perf = data.performance(matches)
    return {
        "page": "performance",
        "league": league,
        "tabs": league_tabs(league),
        "perf": perf,
        "market_weight": data.MARKET_WEIGHT,
        "base_days": data.BASE_RATE_DAYS,
        "leagues": [
            (league_label(code), perf.by_league[code])
            for code in GUI_LEAGUE_ORDER
            if code in perf.by_league
        ],
    }


STATUS_LABELS = {
    "sqlite_integrity": "Integridad de la base",
    "foreign_keys": "Llaves foráneas",
    "schema_version": "Versión del esquema",
    "fixture_kickoff_utc": "Partidos con hora",
    "prediction_artifact_id": "Predicciones con modelo trazable",
    "prediction_generated_before_kickoff": "Predicciones antes del partido",
    "raw_odds_closing_flags": "Marcas de cierre de cuotas",
    "odds_snapshots": "Capturas de cuotas",
    "recent_failed_runs": "Corridas fallidas (7 días)",
    "results_freshness": "Resultados al día",
    "history_freshness": "Historial de entrenamiento",
    "training_freshness": "Modelo al día",
    "league_horizon": "Ligas con partidos próximos",
    "forecast_snapshots": "Pronósticos congelados",
    "club_odds_capture": "Cuotas de próximos partidos",
    "backup_age": "Respaldo reciente",
}

JOBS = (
    ("backup-db", "Respaldo"),
    ("ingest-results", "Resultados"),
    ("build-elo", "Elo"),
    ("predict-fixtures", "Predicciones"),
    ("freeze-forecast-snapshot", "Congelar pronósticos"),
    ("settle-fixtures", "Liquidación"),
    ("fetch-odds", "Cuotas"),
)


def status(conn: sqlite3.Connection) -> dict[str, object]:
    checks = [
        {
            "name": STATUS_LABELS.get(check.name, check.name),
            "status": check.status,
            "value": check.value,
            "details": check.details,
        }
        for check in db_health_checks(conn)
    ]
    runs = []
    for job, label in JOBS:
        row = conn.execute(
            """
            SELECT status, started_utc, finished_utc, message
            FROM ingest_run WHERE job_name = ?
            ORDER BY run_id DESC LIMIT 1
            """,
            (job,),
        ).fetchone()
        runs.append({"job": job, "label": label, **(dict(row) if row else {})})
    worst = "OK"
    if any(c["status"] == "FAIL" for c in checks):
        worst = "FAIL"
    elif any(c["status"] == "WARNING" for c in checks):
        worst = "WARNING"
    return {"page": "status", "checks": checks, "runs": runs, "overall": worst}
