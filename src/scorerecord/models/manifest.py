"""Frozen model manifest for the current production candidate."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from scorerecord.models.dixon_coles import _DEFAULT_XI


MODEL_VERSION = "dixon_coles:hl3y:platt-goals-v1"
BASE_MODEL = "dixon_coles"
HALF_LIFE_YEARS = 3.0
GOAL_MARKETS = ("BTTS", "OU1.5", "OU2.5", "OU3.5")
CALIBRATION_TYPE = "platt"
CALIBRATION_MODE = "prequential"
CALIBRATION_VERSION = "v1"


@dataclass(frozen=True)
class ModelManifest:
    model_version: str
    base_model: str
    half_life_years: float
    xi: float
    markets: tuple[str, ...]
    calibration: dict[str, str]
    validated_run_id: int | None
    prediction_run_id: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "model_version": self.model_version,
            "base_model": self.base_model,
            "half_life_years": self.half_life_years,
            "xi": self.xi,
            "markets": list(self.markets),
            "calibration": self.calibration,
            "run_id_scope": "sqlite_database_local",
            "validated_run_id": self.validated_run_id,
            "prediction_run_id": self.prediction_run_id,
        }


def build_model_manifest(
    conn: sqlite3.Connection,
    *,
    prediction_run_id: int | None = None,
) -> ModelManifest:
    return ModelManifest(
        model_version=MODEL_VERSION,
        base_model=BASE_MODEL,
        half_life_years=HALF_LIFE_YEARS,
        xi=round(_DEFAULT_XI, 9),
        markets=GOAL_MARKETS,
        calibration={
            "type": CALIBRATION_TYPE,
            "mode": CALIBRATION_MODE,
            "version": CALIBRATION_VERSION,
        },
        validated_run_id=_latest_passed_goal_market_run(conn),
        prediction_run_id=prediction_run_id or _latest_prediction_run(conn),
    )


def _latest_passed_goal_market_run(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        """
        SELECT dq.run_id
        FROM data_quality dq
        JOIN ingest_run ir ON ir.run_id = dq.run_id
        WHERE dq.check_name = 'goal_market_gate'
          AND dq.metric_name = 'all_markets_passed'
          AND dq.metric_value = 1.0
          AND ir.status = 'succeeded'
        ORDER BY dq.run_id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["run_id"]) if row else None


def _latest_prediction_run(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        """
        SELECT p.run_id
        FROM prediction p
        JOIN ingest_run ir ON ir.run_id = p.run_id
        WHERE p.model_version = ?
          AND ir.status = 'succeeded'
        ORDER BY p.run_id DESC
        LIMIT 1
        """,
        (MODEL_VERSION,),
    ).fetchone()
    return int(row["run_id"]) if row else None
