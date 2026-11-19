"""HTTP layer for the dashboard.

``WebApp.handle`` is a pure request -> response function (no sockets), so it
is tested directly; ``serve`` wraps it in the standard-library threaded server,
which is enough for a tailnet-only dashboard on a small homeserver.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import mimetypes
import sys
import threading
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

from jinja2 import Environment, PackageLoader, select_autoescape

from scorerecord.league_config import GUI_LEAGUE_ORDER, normalize_league_code
from scorerecord.web import charts, data, pages

STATIC_DIR = Path(__file__).parent / "static"
LEGACY_REDIRECTS = {
    "/index.html": "/",
    "/today": "/",
    "/signals": "/",
    "/history": "/resultados",
    "/performance": "/desempeno",
    "/market": "/desempeno",
    "/health": "/estado",
}
WEEKDAYS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
CACHE_LIMIT = 256


@dataclass
class Response:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


def day_label(value: date, today: date | None = None) -> str:
    if today is not None:
        delta = (value - today).days
        if delta == 0:
            return "Hoy"
        if delta == 1:
            return "Mañana"
        if delta == -1:
            return "Ayer"
    return f"{WEEKDAYS[value.weekday()]} {value.day} {MONTHS[value.month - 1]}"


def long_day(value: date) -> str:
    names = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")
    months = (
        "enero",
        "febrero",
        "marzo",
        "abril",
        "mayo",
        "junio",
        "julio",
        "agosto",
        "septiembre",
        "octubre",
        "noviembre",
        "diciembre",
    )
    return f"{names[value.weekday()]} {value.day} de {months[value.month - 1]}"


class WebApp:
    def __init__(self, db_path: Path, *, clock=None) -> None:
        self.db_path = Path(db_path)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.env = Environment(
            loader=PackageLoader("scorerecord.web", "templates"),
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.env.filters.update(
            pct=charts.pct,
            hhmm=lambda dt: dt.strftime("%H:%M"),
            day_label=day_label,
            long_day=long_day,
            signed=lambda v, d=3: "—" if v is None else f"{v:+.{d}f}",
            num=lambda v, d=3: "—" if v is None else f"{v:.{d}f}",
        )
        self.env.globals.update(
            charts=charts,
            static_version=self._static_version(),
            qs=self._qs,
        )
        self._cache: dict[str, tuple[str, Response]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _qs(path: str, **params: object) -> str:
        clean = {k: v for k, v in params.items() if v not in (None, "", False)}
        return path + ("?" + urlencode(clean, quote_via=quote) if clean else "")

    @staticmethod
    def _static_version() -> str:
        digest = hashlib.sha1()
        for item in sorted(STATIC_DIR.glob("*")):
            digest.update(item.read_bytes())
        return digest.hexdigest()[:10]

    def _db_fingerprint(self) -> str:
        parts = []
        for suffix in ("", "-wal"):
            target = Path(str(self.db_path) + suffix)
            try:
                stat = target.stat()
            except OSError:
                parts.append("-")
                continue
            parts.append(f"{stat.st_mtime_ns}:{stat.st_size}")
        # Coarse minute bucket: "Hoy", live status and relative labels move with time.
        parts.append(self.clock().strftime("%Y%m%d%H%M"))
        return "|".join(parts)

    def _render(self, template: str, context: dict[str, object], status: int = 200) -> Response:
        now = self.clock()
        body = self.env.get_template(template).render(
            {"now": now, "today": now.astimezone(data.display_tz()).date(), **context}
        )
        return Response(status, body.encode("utf-8"), {"Content-Type": "text/html; charset=utf-8"})

    @staticmethod
    def _json(payload: object, status: int = 200) -> Response:
        body = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
        return Response(status, body.encode("utf-8"), {"Content-Type": "application/json"})

    @staticmethod
    def _redirect(location: str) -> Response:
        return Response(301, b"", {"Location": location})

    # ------------------------------------------------------------ dispatch

    def handle(self, method: str, raw_path: str, headers: dict[str, str] | None = None) -> Response:
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        if method not in ("GET", "HEAD"):
            return Response(405, b"", {"Allow": "GET, HEAD"})
        parsed = urlparse(raw_path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/healthz":
            return Response(200, b"ok\n", {"Content-Type": "text/plain"})
        if path.startswith("/static/"):
            return self._static(path.removeprefix("/static/"))
        if path in LEGACY_REDIRECTS:
            return self._redirect(LEGACY_REDIRECTS[path])
        if path.startswith("/match/"):
            return self._redirect("/partido/" + path.removeprefix("/match/"))

        key = raw_path
        fingerprint = self._db_fingerprint()
        etag = '"' + hashlib.sha1(f"{fingerprint}|{key}".encode()).hexdigest()[:16] + '"'
        if headers.get("if-none-match") == etag:
            return Response(304, b"", {"ETag": etag})
        with self._lock:
            cached = self._cache.get(key)
        if cached and cached[0] == fingerprint:
            response = cached[1]
        else:
            response = self._route(path, parse_qs(parsed.query))
            if response.status == 200:
                with self._lock:
                    if len(self._cache) >= CACHE_LIMIT:
                        self._cache.clear()
                    self._cache[key] = (fingerprint, response)
        response = Response(response.status, response.body, dict(response.headers))
        if response.status == 200:
            response.headers["ETag"] = etag
            response.headers["Cache-Control"] = "no-cache"
        return self._compress(response, headers)

    def _route(self, path: str, query: dict[str, list[str]]) -> Response:
        def arg(name: str) -> str | None:
            values = query.get(name)
            return values[0].strip() if values and values[0].strip() else None

        league = self._league(arg("liga"))
        now = self.clock()
        try:
            with closing(data.open_readonly(self.db_path)) as conn:
                if path == "/":
                    day = self._date(arg("fecha"))
                    return self._render(
                        "home.html", pages.home(conn, now=now, requested_day=day, league=league)
                    )
                if path.startswith("/partido/"):
                    context = pages.match(conn, fixture_id=path.removeprefix("/partido/"), now=now)
                    if context is None:
                        return self._render(
                            "error.html", {"message": "No encontramos ese partido."}, 404
                        )
                    return self._render("match.html", context)
                if path == "/resultados":
                    days = self._int(arg("dias"), default=14, allowed=pages.RESULT_WINDOWS)
                    return self._render(
                        "results.html", pages.results(conn, now=now, days=days, league=league)
                    )
                if path == "/desempeno":
                    return self._render(
                        "performance.html", pages.performance(conn, now=now, league=league)
                    )
                if path == "/estado":
                    return self._render("status.html", pages.status(conn))
                if path == "/api/partidos":
                    day = self._date(arg("fecha")) or now.astimezone(data.display_tz()).date()
                    start, end = data.local_day_bounds(day)
                    rows = data.load_matches(
                        conn, start_utc=start, end_utc=end, league=league, now_utc=now
                    )
                    return self._json(
                        {"date": day.isoformat(), "matches": [m.as_json() for m in rows]}
                    )
                if path.startswith("/api/partido/"):
                    rows = data.load_matches(
                        conn, fixture_id=path.removeprefix("/api/partido/"), now_utc=now
                    )
                    if not rows:
                        return self._json({"error": "not_found"}, 404)
                    return self._json(rows[0].as_json())
                if path == "/api/desempeno":
                    rows = data.load_matches(
                        conn, end_utc=now, league=league, finished=True, now_utc=now
                    )
                    return self._json(data.performance(rows).as_json())
        except Exception as exc:  # the visitor gets a generic page; details go to the log
            print(f"web error on {path}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            if path.startswith("/api/"):
                return self._json({"error": "internal"}, 500)
            return self._render(
                "error.html",
                {
                    "message": "No pudimos generar esta página. "
                    "El detalle quedó en el log del servicio."
                },
                500,
            )
        return self._render("error.html", {"message": "Esta página no existe."}, 404)

    @staticmethod
    def _league(value: str | None) -> str | None:
        if not value:
            return None
        code = normalize_league_code(value)
        return code if code in GUI_LEAGUE_ORDER else None

    @staticmethod
    def _date(value: str | None) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _int(value: str | None, *, default: int, allowed: tuple[int, ...]) -> int:
        try:
            parsed = int(value) if value else default
        except ValueError:
            return default
        return parsed if parsed in allowed else default

    def _static(self, name: str) -> Response:
        target = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            return Response(404, b"not found", {"Content-Type": "text/plain"})
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith("javascript"):
            content_type += "; charset=utf-8"
        return Response(
            200,
            target.read_bytes(),
            {"Content-Type": content_type, "Cache-Control": "public, max-age=604800, immutable"},
        )

    @staticmethod
    def _compress(response: Response, headers: dict[str, str]) -> Response:
        if (
            response.status == 200
            and len(response.body) > 1024
            and "gzip" in headers.get("accept-encoding", "")
        ):
            response.body = gzip.compress(response.body, compresslevel=6)
            response.headers["Content-Encoding"] = "gzip"
            response.headers["Vary"] = "Accept-Encoding"
        return response


def serve(db_path: Path, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    app = WebApp(db_path)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ScoreRecord"

        def _respond(self, include_body: bool) -> None:
            response = app.handle("GET", self.path, dict(self.headers.items()))
            try:
                self.send_response(response.status)
                for name, value in response.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(response.body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if include_body:
                    self.wfile.write(response.body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def do_GET(self) -> None:  # noqa: N802
            self._respond(True)

        def do_HEAD(self) -> None:  # noqa: N802
            self._respond(False)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f"ScoreRecord dashboard on http://{host}:{port}/", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
