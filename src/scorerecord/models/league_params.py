"""Per-league model settings chosen by ``model-compare``.

The settings live in ``league_params.json`` next to this module so they are
versioned with the code and recorded in every model artifact. A league without
an entry uses ``DEFAULT_CONFIG``, which reproduces the model in production
before per-league tuning (three-year half-life, ridge 1, Platt calibration of
goal markets on all out-of-sample history, uncalibrated 1X2).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

PARAMS_PATH = Path(__file__).with_name("league_params.json")
DEFAULT_MARKET_WEIGHT = 0.7


@dataclass(frozen=True)
class ModelConfig:
    half_life_years: float = 3.0
    l2: float = 1.0
    # None: calibrate goal markets on every out-of-sample prediction available;
    # a number: only on the last N days, so the calibrator follows scoring trends.
    goal_calibration_days: int | None = None
    calibrate_result: bool = False
    result_calibration_days: int = 730
    market_weight: float = DEFAULT_MARKET_WEIGHT
    # Set by model-compare from the test window: does the model beat the
    # league average in these markets? The dashboard only highlights a goal
    # label when it does.
    ou_beats_base: bool = True
    btts_beats_base: bool = True
    # Other competitions trained together with this league (for example its
    # second division), each with its own goal level, so promoted teams start
    # from their previous rating instead of the league average.
    pool_with: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        cal = "all" if self.goal_calibration_days is None else f"{self.goal_calibration_days}d"
        res = f"cal{self.result_calibration_days}d" if self.calibrate_result else "raw"
        pool = f"_pool{'+'.join(self.pool_with)}" if self.pool_with else ""
        return f"hl{self.half_life_years:g}_l2{self.l2:g}_goals{cal}_1x2{res}{pool}"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


DEFAULT_CONFIG = ModelConfig()


@lru_cache(maxsize=1)
def _load(path: str) -> dict[str, object]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def league_config(league: str, *, path: Path = PARAMS_PATH) -> ModelConfig:
    raw = _load(str(path)).get("leagues", {})
    entry = raw.get(league) if isinstance(raw, dict) else None
    if not isinstance(entry, dict):
        return DEFAULT_CONFIG
    allowed = set(ModelConfig.__dataclass_fields__)
    values = {key: value for key, value in entry.items() if key in allowed}
    if "pool_with" in values:
        values["pool_with"] = tuple(values["pool_with"])
    return ModelConfig(**values)


def params_source(*, path: Path = PARAMS_PATH) -> dict[str, object]:
    data = _load(str(path))
    return {key: data.get(key) for key in ("generated_utc", "source", "report")}


def write_params(
    leagues: dict[str, ModelConfig],
    *,
    source: str,
    report: str,
    generated_utc: str,
    path: Path = PARAMS_PATH,
) -> None:
    payload = {
        "generated_utc": generated_utc,
        "source": source,
        "report": report,
        "leagues": {league: config.as_dict() for league, config in sorted(leagues.items())},
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    _load.cache_clear()
