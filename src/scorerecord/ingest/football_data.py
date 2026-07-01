"""Ingest Football-Data historical results and 1X2 odds CSVs."""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from scorerecord.evaluate.historical_odds import HistoricalEvent, HistoricalOddsQuote
from scorerecord.runs import ingest_run, record_quality
from scorerecord.sources import fetch_text


FOOTBALL_DATA_BASE_URL = "https://www.football-data.co.uk/mmz4281"
FOOTBALL_DATA_SOURCE = "football-data"

FOOTBALL_DATA_LEAGUES = {
    "EPL": "E0",
    "CHAMPIONSHIP": "E1",
    "LEAGUE_ONE": "E2",
    "LEAGUE_TWO": "E3",
    "LA_LIGA": "SP1",
    "SEGUNDA": "SP2",
    "BUNDESLIGA": "D1",
    "BUNDESLIGA_2": "D2",
    "SERIE_A": "I1",
    "SERIE_B": "I2",
    "LIGUE_1": "F1",
    "LIGUE_2": "F2",
    "EREDIVISIE": "N1",
    "BELGIUM": "B1",
    "PORTUGAL": "P1",
    "TURKEY": "T1",
    "GREECE": "G1",
}

SELECTION_COLUMNS = {
    "home": "H",
    "draw": "D",
    "away": "A",
}
OU25_LINE = 2.5
OU25_COLUMNS = {
    "over": (">2.5", "O2.5"),
    "under": ("<2.5", "U2.5"),
}

ODDS_SPECS = (
    ("Avg", "Avg", "avg_open", "AvgC", "avg_close"),
    ("Bet365", "B365", "bet365_open", "B365C", "bet365_close"),
    ("Max", "Max", "max_open", "MaxC", "max_close"),
)
OU25_SPECS = (
    ("Avg", ("Avg", "BbAv"), "avg_preclose", ("AvgC", "BbAvC"), "avg_close"),
    ("Bet365", ("B365",), "bet365_preclose", ("B365C",), "bet365_close"),
    ("Max", ("Max", "BbMx"), "max_preclose", ("MaxC", "BbMxC"), "max_close"),
)


@dataclass(frozen=True)
class FootballDataParseResult:
    events: list[HistoricalEvent]
    quotes: list[HistoricalOddsQuote]
    rows_seen: int
    rows_loaded: int
    missing_results: int
    missing_open_odds: int
    missing_closing_odds: int
    invalid_odds: int
    duplicate_events: int
    league_code: str
    season: str
    url: str


@dataclass(frozen=True)
class FootballDataIngestSummary:
    run_id: int | None
    rows_seen: int
    events_upserted: int
    odds_upserted: int
    missing_results: int
    missing_open_odds: int
    missing_closing_odds: int
    invalid_odds: int
    duplicate_events: int
    dry_run: bool

    def as_row(self) -> str:
        run = "na" if self.run_id is None else str(self.run_id)
        return (
            f"run_id={run} rows_seen={self.rows_seen} "
            f"events_upserted={self.events_upserted} odds_upserted={self.odds_upserted} "
            f"missing_results={self.missing_results} "
            f"missing_open_odds={self.missing_open_odds} "
            f"missing_closing_odds={self.missing_closing_odds} "
            f"invalid_odds={self.invalid_odds} duplicate_events={self.duplicate_events} "
            f"dry_run={self.dry_run}"
        )


@dataclass(frozen=True)
class FootballDataCoverageRow:
    source: str
    league_code: str
    season: str
    market: str
    matches: int
    odds_rows: int
    avg_open_rows: int
    avg_close_rows: int
    avg_open_coverage: float
    avg_close_coverage: float
    avg_entry_odds_type: str = "avg_open"
    avg_closing_odds_type: str = "avg_close"

    def as_row(self) -> str:
        return (
            f"{self.source} {self.league_code} {self.season} {self.market} "
            f"matches={self.matches} odds_rows={self.odds_rows} "
            f"{self.avg_entry_odds_type}_rows={self.avg_open_rows} "
            f"{self.avg_entry_odds_type}_coverage={self.avg_open_coverage:.1%} "
            f"{self.avg_closing_odds_type}_rows={self.avg_close_rows} "
            f"{self.avg_closing_odds_type}_coverage={self.avg_close_coverage:.1%}"
        )


def ingest_football_data(
    conn: sqlite3.Connection,
    raw_dir: Path,
    *,
    league: str,
    season: str,
    refresh: bool = False,
    dry_run: bool = False,
    cwd: Path | None = None,
) -> FootballDataIngestSummary:
    parsed = load_football_data(
        raw_dir,
        league=league,
        season=season,
        refresh=refresh,
    )
    if dry_run:
        return FootballDataIngestSummary(
            run_id=None,
            rows_seen=parsed.rows_seen,
            events_upserted=0,
            odds_upserted=0,
            missing_results=parsed.missing_results,
            missing_open_odds=parsed.missing_open_odds,
            missing_closing_odds=parsed.missing_closing_odds,
            invalid_odds=parsed.invalid_odds,
            duplicate_events=parsed.duplicate_events,
            dry_run=True,
        )

    with ingest_run(conn, "ingest-football-data", source=FOOTBALL_DATA_SOURCE, cwd=cwd) as run_id:
        event_count = _upsert_events(conn, parsed.events)
        quote_count = _upsert_quotes(conn, parsed.quotes)
        _record_football_data_quality(conn, run_id, parsed, event_count, quote_count)
        conn.commit()
    return FootballDataIngestSummary(
        run_id=run_id,
        rows_seen=parsed.rows_seen,
        events_upserted=event_count,
        odds_upserted=quote_count,
        missing_results=parsed.missing_results,
        missing_open_odds=parsed.missing_open_odds,
        missing_closing_odds=parsed.missing_closing_odds,
        invalid_odds=parsed.invalid_odds,
        duplicate_events=parsed.duplicate_events,
        dry_run=False,
    )


def football_data_season_range(from_season: str, to_season: str) -> list[str]:
    start = normalize_season(from_season)
    end = normalize_season(to_season)
    start_year = int(start[:4])
    end_year = int(end[:4])
    if end_year < start_year:
        raise ValueError("--to-season must be greater than or equal to --from-season")
    return [f"{year:04d}-{year + 1:04d}" for year in range(start_year, end_year + 1)]


def football_data_coverage_report(
    conn: sqlite3.Connection,
    *,
    source: str = FOOTBALL_DATA_SOURCE,
    league: str | None = None,
    market: str = "1X2",
) -> list[FootballDataCoverageRow]:
    league_code = resolve_league_code(league) if league else None
    market = normalize_market(market)
    avg_entry_type, avg_close_type, expected_selections = _coverage_market_spec(market)
    params: list[object] = [avg_entry_type, avg_close_type, market, source]
    league_filter = ""
    if league_code is not None:
        league_filter = "AND hm.league_code = ?"
        params.append(league_code)
    rows = conn.execute(
        f"""
        SELECT
          hm.source,
          hm.league_code,
          hm.season,
          COUNT(DISTINCT hm.source_event_id) AS matches,
          COUNT(ho.id) AS odds_rows,
          SUM(CASE WHEN ho.entry_odds_type = ? THEN 1 ELSE 0 END) AS avg_open_rows,
          SUM(CASE WHEN ho.closing_odds_type = ? THEN 1 ELSE 0 END) AS avg_close_rows
        FROM historical_match hm
        LEFT JOIN historical_odds ho
          ON ho.source = hm.source
         AND ho.source_event_id = hm.source_event_id
         AND ho.market = ?
        WHERE hm.source = ?
          {league_filter}
        GROUP BY hm.source, hm.league_code, hm.season
        ORDER BY hm.league_code, hm.season
        """,
        params,
    ).fetchall()
    output: list[FootballDataCoverageRow] = []
    for row in rows:
        matches = int(row["matches"])
        expected_selection_rows = matches * expected_selections
        avg_open_rows = int(row["avg_open_rows"] or 0)
        avg_close_rows = int(row["avg_close_rows"] or 0)
        output.append(
            FootballDataCoverageRow(
                source=str(row["source"]),
                league_code=str(row["league_code"]),
                season=str(row["season"]),
                market=market,
                matches=matches,
                odds_rows=int(row["odds_rows"] or 0),
                avg_open_rows=avg_open_rows,
                avg_close_rows=avg_close_rows,
                avg_open_coverage=(
                    avg_open_rows / expected_selection_rows if expected_selection_rows else 0.0
                ),
                avg_close_coverage=(
                    avg_close_rows / expected_selection_rows if expected_selection_rows else 0.0
                ),
                avg_entry_odds_type=avg_entry_type,
                avg_closing_odds_type=avg_close_type,
            )
        )
    return output


def load_football_data(
    raw_dir: Path,
    *,
    league: str,
    season: str,
    refresh: bool = False,
) -> FootballDataParseResult:
    league_code = resolve_league_code(league)
    normalized_season = normalize_season(season)
    season_code = season_to_football_data_code(normalized_season)
    url = football_data_url(league_code=league_code, season_code=season_code)
    text = fetch_text(url, raw_dir / "football-data", refresh=refresh)
    return parse_football_data_csv(
        text,
        league_code=league_code,
        season=normalized_season,
        url=url,
    )


def parse_football_data_csv(
    text: str,
    *,
    league_code: str,
    season: str,
    url: str = "",
) -> FootballDataParseResult:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    events: list[HistoricalEvent] = []
    quotes: list[HistoricalOddsQuote] = []
    rows_seen = 0
    rows_loaded = 0
    missing_results = 0
    missing_open_odds = 0
    missing_closing_odds = 0
    invalid_odds = 0
    duplicate_events = 0
    seen_events: set[str] = set()

    for row in reader:
        rows_seen += 1
        if not _row_has_match_identity(row):
            continue
        match_date = parse_football_data_date(str(row.get("Date") or ""))
        if match_date is None:
            continue
        home_team = str(row.get("HomeTeam") or "").strip()
        away_team = str(row.get("AwayTeam") or "").strip()
        home_goals = _parse_int(row.get("FTHG"))
        away_goals = _parse_int(row.get("FTAG"))
        result = _parse_result(row.get("FTR"), home_goals=home_goals, away_goals=away_goals)
        if home_goals is None or away_goals is None or result is None:
            missing_results += 1

        source_event_id = build_source_event_id(
            league_code=league_code,
            season=season,
            match_date=match_date,
            home_team=home_team,
            away_team=away_team,
        )
        if source_event_id in seen_events:
            duplicate_events += 1
        seen_events.add(source_event_id)

        events.append(
            HistoricalEvent(
                source=FOOTBALL_DATA_SOURCE,
                source_event_id=source_event_id,
                league_code=league_code,
                season=season,
                match_date=match_date,
                home_team=home_team,
                away_team=away_team,
                home_goals=home_goals,
                away_goals=away_goals,
                result_1x2=result,
                raw_json=json.dumps(row, sort_keys=True),
            )
        )
        rows_loaded += 1

        row_quotes, row_missing_open, row_missing_close, row_invalid = extract_1x2_quotes(
            row,
            source_event_id=source_event_id,
        )
        quotes.extend(row_quotes)
        missing_open_odds += row_missing_open
        missing_closing_odds += row_missing_close
        invalid_odds += row_invalid
        row_quotes, row_missing_open, row_missing_close, row_invalid = extract_ou25_quotes(
            row,
            source_event_id=source_event_id,
        )
        quotes.extend(row_quotes)
        missing_open_odds += row_missing_open
        missing_closing_odds += row_missing_close
        invalid_odds += row_invalid

    return FootballDataParseResult(
        events=events,
        quotes=quotes,
        rows_seen=rows_seen,
        rows_loaded=rows_loaded,
        missing_results=missing_results,
        missing_open_odds=missing_open_odds,
        missing_closing_odds=missing_closing_odds,
        invalid_odds=invalid_odds,
        duplicate_events=duplicate_events,
        league_code=league_code,
        season=season,
        url=url,
    )


def extract_1x2_quotes(
    row: dict[str, str],
    *,
    source_event_id: str,
) -> tuple[list[HistoricalOddsQuote], int, int, int]:
    quotes: list[HistoricalOddsQuote] = []
    missing_open = 0
    missing_close = 0
    invalid = 0
    for bookmaker, open_prefix, open_type, close_prefix, close_type in ODDS_SPECS:
        open_odds = {
            selection: _parse_odds(row.get(f"{open_prefix}{suffix}"))
            for selection, suffix in SELECTION_COLUMNS.items()
        }
        close_odds = {
            selection: _parse_odds(row.get(f"{close_prefix}{suffix}"))
            for selection, suffix in SELECTION_COLUMNS.items()
        }
        open_present = [value is not None for value in open_odds.values()]
        close_present = [value is not None for value in close_odds.values()]
        invalid_open = any(
            _is_invalid_raw_odds(row.get(f"{open_prefix}{suffix}"))
            for suffix in SELECTION_COLUMNS.values()
        )
        invalid_close = any(
            _is_invalid_raw_odds(row.get(f"{close_prefix}{suffix}"))
            for suffix in SELECTION_COLUMNS.values()
        )
        if invalid_open:
            invalid += 1
        if invalid_close:
            invalid += 1
        if not all(open_present):
            if any(open_present):
                invalid += 1
            else:
                missing_open += 1
            continue
        closing_type = close_type if all(close_present) else None
        if not all(close_present):
            missing_close += 1
        for selection in SELECTION_COLUMNS:
            quotes.append(
                HistoricalOddsQuote(
                    source=FOOTBALL_DATA_SOURCE,
                    source_event_id=source_event_id,
                    bookmaker=bookmaker,
                    market="1X2",
                    selection=selection,
                    line=None,
                    entry_odds=open_odds[selection],
                    entry_odds_type=open_type,
                    closing_odds=close_odds[selection] if closing_type else None,
                    closing_odds_type=closing_type,
                    is_real_historical_odds=True,
                    is_timestamp_exact=False,
                )
            )
    return quotes, missing_open, missing_close, invalid


def extract_ou25_quotes(
    row: dict[str, str],
    *,
    source_event_id: str,
) -> tuple[list[HistoricalOddsQuote], int, int, int]:
    quotes: list[HistoricalOddsQuote] = []
    missing_open = 0
    missing_close = 0
    invalid = 0
    for bookmaker, open_prefixes, open_type, close_prefixes, close_type in OU25_SPECS:
        if not _any_ou25_columns_present(row, open_prefixes, close_prefixes):
            continue
        open_odds = {
            selection: _parse_odds_from_aliases(row, open_prefixes, suffixes)
            for selection, suffixes in OU25_COLUMNS.items()
        }
        close_odds = {
            selection: _parse_odds_from_aliases(row, close_prefixes, suffixes)
            for selection, suffixes in OU25_COLUMNS.items()
        }
        open_present = [value is not None for value in open_odds.values()]
        close_present = [value is not None for value in close_odds.values()]
        invalid_open = any(
            _is_invalid_raw_odds_from_aliases(row, open_prefixes, suffixes)
            for suffixes in OU25_COLUMNS.values()
        )
        invalid_close = any(
            _is_invalid_raw_odds_from_aliases(row, close_prefixes, suffixes)
            for suffixes in OU25_COLUMNS.values()
        )
        if invalid_open:
            invalid += 1
        if invalid_close:
            invalid += 1
        if not all(open_present):
            if any(open_present):
                invalid += 1
            else:
                missing_open += 1
            continue
        closing_type = close_type if all(close_present) else None
        if not all(close_present):
            missing_close += 1
        for selection in ("over", "under"):
            quotes.append(
                HistoricalOddsQuote(
                    source=FOOTBALL_DATA_SOURCE,
                    source_event_id=source_event_id,
                    bookmaker=bookmaker,
                    market="OU2.5",
                    selection=selection,
                    line=OU25_LINE,
                    entry_odds=open_odds[selection],
                    entry_odds_type=open_type,
                    closing_odds=close_odds[selection] if closing_type else None,
                    closing_odds_type=closing_type,
                    is_real_historical_odds=True,
                    is_timestamp_exact=False,
                )
            )
    return quotes, missing_open, missing_close, invalid


def football_data_url(*, league_code: str, season_code: str) -> str:
    return f"{FOOTBALL_DATA_BASE_URL}/{season_code}/{league_code}.csv"


def resolve_league_code(value: str) -> str:
    key = value.strip().upper().replace("-", "_").replace(" ", "_")
    if key in FOOTBALL_DATA_LEAGUES:
        return FOOTBALL_DATA_LEAGUES[key]
    if re.fullmatch(r"[A-Z]{1,3}\d?", key):
        return key
    supported = ", ".join(sorted(FOOTBALL_DATA_LEAGUES))
    raise ValueError(f"Unsupported Football-Data league '{value}'. Supported aliases: {supported}")


def normalize_market(value: str) -> str:
    raw = value.strip().upper()
    if raw in {"1X2", "H2H"}:
        return "1X2"
    if raw in {"OU2.5", "O/U2.5", "TOTALS2.5", "TOTALS_2.5"}:
        return "OU2.5"
    raise ValueError("Supported historical odds coverage markets: 1X2, OU2.5")


def normalize_season(value: str) -> str:
    raw = value.strip()
    if re.fullmatch(r"\d{4}", raw):
        start = 2000 + int(raw[:2])
        end = 2000 + int(raw[2:])
        if end < start:
            end += 100
        return f"{start:04d}-{end:04d}"
    match = re.fullmatch(r"(\d{4})[-/](\d{2}|\d{4})", raw)
    if not match:
        raise ValueError("Season must look like 2023-2024, 2023/24, or 2324")
    start = int(match.group(1))
    end_raw = match.group(2)
    end = int(end_raw) if len(end_raw) == 4 else (start // 100) * 100 + int(end_raw)
    if end < start:
        end += 100
    return f"{start:04d}-{end:04d}"


def season_to_football_data_code(season: str) -> str:
    start, end = season.split("-", 1)
    return f"{start[-2:]}{end[-2:]}"


def parse_football_data_date(value: str) -> date | None:
    raw = value.strip()
    if not raw:
        return None
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    return None


def build_source_event_id(
    *,
    league_code: str,
    season: str,
    match_date: date,
    home_team: str,
    away_team: str,
) -> str:
    return (
        f"{FOOTBALL_DATA_SOURCE}:{league_code}:{season}:{match_date.isoformat()}:"
        f"{normalize_identifier(home_team)}:{normalize_identifier(away_team)}"
    )


def normalize_identifier(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_text.casefold()
    collapsed = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return re.sub(r"-+", "-", collapsed)


def _row_has_match_identity(row: dict[str, str]) -> bool:
    return bool(str(row.get("Date") or "").strip()) and bool(
        str(row.get("HomeTeam") or "").strip()
    ) and bool(str(row.get("AwayTeam") or "").strip())


def _parse_int(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _parse_result(
    value: object,
    *,
    home_goals: int | None,
    away_goals: int | None,
) -> str | None:
    text = str(value or "").strip().upper()
    if text in {"H", "D", "A"}:
        return {"H": "home", "D": "draw", "A": "away"}[text]
    if home_goals is None or away_goals is None:
        return None
    if home_goals > away_goals:
        return "home"
    if home_goals == away_goals:
        return "draw"
    return "away"


def _parse_odds(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        odds = float(text)
    except ValueError:
        return None
    if odds <= 1.0:
        return None
    return odds


def _parse_odds_from_aliases(
    row: dict[str, str],
    prefixes: tuple[str, ...],
    suffixes: tuple[str, ...],
) -> float | None:
    for prefix in prefixes:
        for suffix in suffixes:
            value = row.get(f"{prefix}{suffix}")
            parsed = _parse_odds(value)
            if parsed is not None:
                return parsed
    return None


def _is_invalid_raw_odds(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return _parse_odds(text) is None


def _is_invalid_raw_odds_from_aliases(
    row: dict[str, str],
    prefixes: tuple[str, ...],
    suffixes: tuple[str, ...],
) -> bool:
    return any(
        _is_invalid_raw_odds(row.get(f"{prefix}{suffix}"))
        for prefix in prefixes
        for suffix in suffixes
    )


def _any_ou25_columns_present(
    row: dict[str, str],
    open_prefixes: tuple[str, ...],
    close_prefixes: tuple[str, ...],
) -> bool:
    prefixes = open_prefixes + close_prefixes
    return any(
        f"{prefix}{suffix}" in row
        for prefix in prefixes
        for suffixes in OU25_COLUMNS.values()
        for suffix in suffixes
    )


def _coverage_market_spec(market: str) -> tuple[str, str, int]:
    if market == "1X2":
        return "avg_open", "avg_close", 3
    if market == "OU2.5":
        return "avg_preclose", "avg_close", 2
    raise ValueError("Supported historical odds coverage markets: 1X2, OU2.5")


def _upsert_events(conn: sqlite3.Connection, events: list[HistoricalEvent]) -> int:
    count = 0
    for event in events:
        cursor = conn.execute(
            """
            INSERT INTO historical_match (
              source, source_event_id, league_code, season, match_date,
              home_team, away_team, home_goals, away_goals, result_1x2, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, source_event_id) DO UPDATE SET
              league_code = excluded.league_code,
              season = excluded.season,
              match_date = excluded.match_date,
              home_team = excluded.home_team,
              away_team = excluded.away_team,
              home_goals = excluded.home_goals,
              away_goals = excluded.away_goals,
              result_1x2 = excluded.result_1x2,
              raw_json = excluded.raw_json
            """,
            (
                event.source,
                event.source_event_id,
                event.league_code,
                event.season,
                event.match_date.isoformat(),
                event.home_team,
                event.away_team,
                event.home_goals,
                event.away_goals,
                event.result_1x2,
                event.raw_json,
            ),
        )
        count += cursor.rowcount
    return count


def _upsert_quotes(conn: sqlite3.Connection, quotes: list[HistoricalOddsQuote]) -> int:
    count = 0
    for quote in quotes:
        cursor = conn.execute(
            """
            INSERT OR REPLACE INTO historical_odds (
              source, source_event_id, bookmaker, market, selection, line,
              entry_odds, entry_odds_type, closing_odds, closing_odds_type,
              is_real_historical_odds, is_timestamp_exact
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                quote.source,
                quote.source_event_id,
                quote.bookmaker,
                quote.market,
                quote.selection,
                quote.line,
                quote.entry_odds,
                quote.entry_odds_type,
                quote.closing_odds,
                quote.closing_odds_type,
                int(quote.is_real_historical_odds),
                int(quote.is_timestamp_exact),
            ),
        )
        count += cursor.rowcount
    return count


def _record_football_data_quality(
    conn: sqlite3.Connection,
    run_id: int,
    parsed: FootballDataParseResult,
    event_count: int,
    quote_count: int,
) -> None:
    details = {
        "source": FOOTBALL_DATA_SOURCE,
        "league_code": parsed.league_code,
        "season": parsed.season,
        "url": parsed.url,
        "rows_seen": parsed.rows_seen,
        "rows_loaded": parsed.rows_loaded,
        "events_upserted": event_count,
        "odds_upserted": quote_count,
        "decision_time_rule": "pre_match_open_proxy_or_preclose_totals",
        "is_real_historical_odds": True,
        "is_timestamp_exact": False,
        "markets": sorted({quote.market for quote in parsed.quotes}),
    }
    for metric_name, value in [
        ("football_data_rows", parsed.rows_seen),
        ("football_data_rows_loaded", parsed.rows_loaded),
        ("football_data_events_upserted", event_count),
        ("football_data_odds_upserted", quote_count),
        ("football_data_missing_results", parsed.missing_results),
        ("football_data_missing_open_odds", parsed.missing_open_odds),
        ("football_data_missing_closing_odds", parsed.missing_closing_odds),
        ("football_data_invalid_odds", parsed.invalid_odds),
        ("football_data_duplicate_events", parsed.duplicate_events),
    ]:
        record_quality(
            conn,
            run_id,
            "football_data_ingest",
            metric_name,
            float(value),
            json.dumps(details, sort_keys=True),
        )
