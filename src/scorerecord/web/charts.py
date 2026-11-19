"""Small server-rendered charts (HTML and inline SVG).

Colors are CSS custom properties defined in static/app.css for light and dark
mode; every mark carries a ``data-tip`` tooltip and a visible label, so no
value is encoded by color alone.
"""

from __future__ import annotations

from html import escape

from markupsafe import Markup

HEAT_STEPS = 7


def pct(value: float | None, digits: int = 0) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.{digits}f}%"


def result_bar(probs: dict[str, float], home: str, away: str, *, compact: bool = False) -> Markup:
    """Three-segment bar for local / empate / visitante with direct labels."""
    parts = []
    labels = []
    for key, name, css in (
        ("home", home, "seg-home"),
        ("draw", "Empate", "seg-draw"),
        ("away", away, "seg-away"),
    ):
        value = probs[key]
        tip = escape(f"{name}: {pct(value, 1)}")
        parts.append(
            f'<span class="seg {css}" style="flex-grow:{max(value, 0.001):.4f}" '
            f'data-tip="{tip}"></span>'
        )
        labels.append(
            f'<span class="seg-label {css}-label" style="flex-grow:{max(value, 0.001):.4f}">'
            f"<b>{pct(value)}</b>{'' if compact else ' ' + escape(_short(name))}</span>"
        )
    return Markup(
        '<div class="result-bar" role="img" aria-label="'
        + escape(
            f"Local {pct(probs['home'])}, empate {pct(probs['draw'])}, visitante {pct(probs['away'])}"
        )
        + '"><div class="segs">'
        + "".join(parts)
        + '</div><div class="seg-labels">'
        + "".join(labels)
        + "</div></div>"
    )


def _short(name: str, limit: int = 14) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "…"


def meter(prob: float, base: float | None, label: str) -> Markup:
    """Probability bar with a tick at the league base rate."""
    tick = ""
    if base is not None:
        tick = (
            f'<span class="meter-tick" style="left:{base * 100:.1f}%" '
            f'data-tip="{escape(f"Promedio de la liga: {pct(base, 1)}")}"></span>'
        )
    return Markup(
        f'<div class="meter" role="img" aria-label="{escape(label)} {pct(prob)}">'
        f'<span class="meter-fill" style="width:{prob * 100:.1f}%" '
        f'data-tip="{escape(f"{label}: {pct(prob, 1)}")}"></span>{tick}</div>'
    )


def score_heatmap(grid: list[list[float]], home: str, away: str) -> Markup:
    """Score matrix: rows are home goals, columns away goals."""
    top = max((value for row in grid for value in row), default=0.0) or 1.0
    head = "".join(f'<th scope="col">{a}</th>' for a in range(len(grid[0])))
    body = []
    for h, row in enumerate(grid):
        cells = []
        for a, value in enumerate(row):
            step = min(HEAT_STEPS - 1, int(value / top * (HEAT_STEPS - 1) + 0.5))
            tip = escape(f"{home} {h} - {a} {away}: {pct(value, 1)}")
            text = f"{value * 100:.0f}" if value >= 0.005 else ""
            cells.append(f'<td class="heat-{step}" data-tip="{tip}">{text}</td>')
        body.append(f'<tr><th scope="row">{h}</th>{"".join(cells)}</tr>')
    return Markup(
        '<table class="heatmap"><caption>Probabilidad de cada marcador (%) · filas: goles de '
        f"{escape(home)} · columnas: goles de {escape(away)}</caption>"
        f"<thead><tr><th></th>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def sparkline(values: list[float], *, width: int = 140, height: int = 36) -> Markup:
    if len(values) < 2:
        return Markup("")
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    step = (width - 8) / (len(values) - 1)
    points = [
        (4 + i * step, 4 + (height - 8) * (1 - (v - lo) / span)) for i, v in enumerate(values)
    ]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    end_x, end_y = points[-1]
    return Markup(
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img" aria-label="Elo de {lo:.0f} a {hi:.0f}">'
        f'<polyline points="{path}" fill="none" stroke="currentColor" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{end_x:.1f}" cy="{end_y:.1f}" r="3" fill="currentColor"/></svg>'
    )


def calibration_chart(points: list[dict[str, float]], title: str) -> Markup:
    """Forecast vs observed frequency per probability bin."""
    size, pad = 220, 30
    inner = size - pad - 10

    def sx(p: float) -> float:
        return pad + p * inner

    def sy(p: float) -> float:
        return 10 + (1 - p) * inner

    grid = "".join(
        f'<line class="grid" x1="{sx(t):.1f}" y1="{sy(0):.1f}" x2="{sx(t):.1f}" y2="{sy(1):.1f}"/>'
        f'<line class="grid" x1="{sx(0):.1f}" y1="{sy(t):.1f}" x2="{sx(1):.1f}" y2="{sy(t):.1f}"/>'
        f'<text class="axis" x="{sx(t):.1f}" y="{size - 8}" text-anchor="middle">{int(t * 100)}</text>'
        f'<text class="axis" x="{pad - 6}" y="{sy(t) + 4:.1f}" text-anchor="end">{int(t * 100)}</text>'
        for t in (0, 0.5, 1)
    )
    dots = []
    total = sum(point["n"] for point in points) or 1
    for point in points:
        radius = 4 + 6 * (point["n"] / total) ** 0.5
        tip = escape(
            f"Pronosticado {pct(point['mean_prob'], 0)} → ocurrió {pct(point['observed'], 0)} "
            f"({int(point['n'])} casos)"
        )
        cx, cy = sx(point["mean_prob"]), sy(point["observed"])
        dots.append(
            f'<circle class="dot" cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}"/>'
            f'<circle class="hit" cx="{cx:.1f}" cy="{cy:.1f}" r="{radius + 8:.1f}" data-tip="{tip}"/>'
        )
    return Markup(
        f'<figure class="chart"><figcaption>{escape(title)}</figcaption>'
        f'<svg viewBox="0 0 {size} {size}" role="img" aria-label="{escape(title)}: calibración">'
        f"{grid}"
        f'<line class="diag" x1="{sx(0)}" y1="{sy(0)}" x2="{sx(1)}" y2="{sy(1)}"/>'
        f"{''.join(dots)}</svg></figure>"
    )


SERIES = (
    ("1X2", "Resultado", "s1"),
    ("OU2.5", "Más/Menos 2.5", "s2"),
    ("BTTS", "Ambos anotan", "s3"),
)


def skill_chart(monthly: list[dict[str, object]], *, min_n: int = 15) -> Markup:
    """Monthly log-loss gain over the league base rate, one line per market."""
    months = [row for row in monthly if any((row[m]["n"] or 0) >= min_n for m, _, _ in SERIES)]
    if len(months) < 2:
        return Markup(
            '<p class="muted">Aún no hay suficientes meses para dibujar la tendencia.</p>'
        )
    width, height, left, right, top, bottom = 640, 240, 44, 110, 14, 30
    values = [
        row[m]["skill"]
        for row in months
        for m, _, _ in SERIES
        if row[m]["skill"] is not None and row[m]["n"] >= min_n
    ]
    lo = min(min(values), -0.02)
    hi = max(max(values), 0.02)
    inner_w = width - left - right
    inner_h = height - top - bottom

    def sx(i: int) -> float:
        return left + inner_w * (i / (len(months) - 1))

    def sy(v: float) -> float:
        return top + inner_h * (hi - v) / (hi - lo)

    parts = [
        f'<line class="zero" x1="{left}" y1="{sy(0):.1f}" x2="{width - right}" y2="{sy(0):.1f}"/>',
        f'<text class="axis" x="{left - 6}" y="{sy(0) + 4:.1f}" text-anchor="end">0</text>',
        f'<text class="axis" x="{left - 6}" y="{sy(hi) + 4:.1f}" text-anchor="end">{hi:+.2f}</text>',
        f'<text class="axis" x="{left - 6}" y="{sy(lo) + 4:.1f}" text-anchor="end">{lo:+.2f}</text>',
    ]
    for i, row in enumerate(months):
        parts.append(
            f'<text class="axis" x="{sx(i):.1f}" y="{height - 8}" text-anchor="middle">'
            f"{escape(str(row['month'])[2:])}</text>"
        )
    for market, name, css in SERIES:
        pts = [
            (sx(i), sy(row[market]["skill"]), row)
            for i, row in enumerate(months)
            if row[market]["skill"] is not None and row[market]["n"] >= min_n
        ]
        if not pts:
            continue
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in pts)
        parts.append(f'<polyline class="line {css}" points="{path}"/>')
        for x, y, row in pts:
            tip = escape(
                f"{name} · {row['month']}: {row[market]['skill']:+.3f} "
                f"({row[market]['n']} partidos)"
            )
            parts.append(f'<circle class="pt {css}" cx="{x:.1f}" cy="{y:.1f}" r="4"/>')
            parts.append(f'<circle class="hit" cx="{x:.1f}" cy="{y:.1f}" r="12" data-tip="{tip}"/>')
        end_x, end_y, _ = pts[-1]
        parts.append(
            f'<text class="end-label" x="{end_x + 8:.1f}" y="{end_y + 4:.1f}">{escape(name)}</text>'
        )
    legend = "".join(
        f'<span class="legend-item"><i class="swatch {css}"></i>{escape(name)}</span>'
        for _, name, css in SERIES
    )
    return Markup(
        f'<figure class="chart wide"><div class="legend">{legend}</div>'
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Ventaja mensual sobre el promedio de la liga">{"".join(parts)}</svg>'
        "<figcaption>Encima de 0: el modelo pronosticó mejor que el promedio de la liga ese mes."
        "</figcaption></figure>"
    )
