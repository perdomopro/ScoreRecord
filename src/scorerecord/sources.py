from __future__ import annotations

import hashlib
import time
import urllib.request
from pathlib import Path


OPENFOOTBALL_2026_URL = (
    "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json"
)

MARTJ42_RESULTS_URL = (
    "https://raw.githubusercontent.com/martj42/international_results/master/results.csv"
)
MARTJ42_GOALSCORERS_URL = (
    "https://raw.githubusercontent.com/martj42/international_results/master/goalscorers.csv"
)


def cache_path(raw_dir: Path, url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    name = url.rsplit("/", 1)[-1] or "download"
    return raw_dir / f"{digest}-{name}"


def fetch_text(url: str, raw_dir: Path, *, refresh: bool = False, timeout: int = 30) -> str:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = cache_path(raw_dir, url)
    if path.exists() and not refresh:
        return path.read_text(encoding="utf-8")

    request = urllib.request.Request(url, headers={"User-Agent": "ScoreRecord/0.1"})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read().decode("utf-8")
            path.write_text(text, encoding="utf-8")
            return text
        except Exception as exc:  # pragma: no cover - network dependent
            last_error = exc
            time.sleep(2**attempt)

    raise RuntimeError(f"Could not fetch {url}: {last_error}")
