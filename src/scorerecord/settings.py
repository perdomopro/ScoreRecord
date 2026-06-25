from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: Path
    raw_dir: Path
    odds_api_io_key: str | None = None
    odds_api_io_base_url: str = "https://api.odds-api.io/v3"
    odds_api_io_bookmakers: str = "Bet365,Unibet"


def load_settings() -> Settings:
    _load_dotenv(Path(".env"))
    db_path = Path(os.getenv("SCORERECORD_DB_PATH", "data/scorerecord.db"))
    raw_dir = Path(os.getenv("SCORERECORD_RAW_DIR", "data/raw"))
    return Settings(
        db_path=db_path,
        raw_dir=raw_dir,
        odds_api_io_key=os.getenv("ODDS_API_IO_KEY") or None,
        odds_api_io_base_url=os.getenv("ODDS_API_IO_BASE_URL", "https://api.odds-api.io/v3"),
        odds_api_io_bookmakers=os.getenv("ODDS_API_IO_BOOKMAKERS", "Bet365,Unibet"),
    )


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
