"""Frozen model manifest for the current production candidate."""

from __future__ import annotations

import hashlib
import json
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
MARKET_VALIDATION_STATUS = "NOT_TESTED"


@dataclass(frozen=True)
class ModelTrainingPolicy:
    policy_id: str
    tournament: str
    trained_until: str
    cutoff_rule: str

    def as_dict(self) -> dict[str, str]:
        return {
            "policy_id": self.policy_id,
            "tournament": self.tournament,
            "trained_until": self.trained_until,
            "cutoff_rule": self.cutoff_rule,
        }


TRAINING_POLICY = ModelTrainingPolicy(
    policy_id="worldcup-2026-pre-tournament-v1",
    tournament="FIFA World Cup 2026",
    trained_until="2026-06-11",
    cutoff_rule="match_history.date < trained_until",
)
TRAINED_UNTIL = TRAINING_POLICY.trained_until


@dataclass(frozen=True)
class ModelArtifact:
    artifact_id: str
    payload: dict[str, object]


@dataclass(frozen=True)
class ModelManifest:
    model_version: str
    base_model: str
    half_life_years: float
    xi: float
    markets: tuple[str, ...]
    calibration: dict[str, object]
    predictive_validation_run_id: int | None
    prediction_run_id: int | None
    market_validation_status: str
    trained_until: str
    training_policy: dict[str, str]
    training_rows: int
    model_artifact_id: str
    code_git_sha: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "model_version": self.model_version,
            "base_model": self.base_model,
            "half_life_years": self.half_life_years,
            "xi": self.xi,
            "markets": list(self.markets),
            "calibration": self.calibration,
            "run_id_scope": "sqlite_database_local",
            "predictive_validation_run_id": self.predictive_validation_run_id,
            "prediction_run_id": self.prediction_run_id,
            "market_validation_status": self.market_validation_status,
            "trained_until": self.trained_until,
            "training_policy": self.training_policy,
            "training_rows": self.training_rows,
            "model_artifact_id": self.model_artifact_id,
            "code_git_sha": self.code_git_sha,
        }


def build_model_manifest(
    conn: sqlite3.Connection,
    *,
    prediction_run_id: int | None = None,
) -> ModelManifest:
    latest_artifact = _latest_model_artifact(conn, prediction_run_id=prediction_run_id)
    if latest_artifact:
        payload = json.loads(str(latest_artifact["details_json"] or "{}"))
        params = json.loads(str(latest_artifact["params_json"] or "{}"))
        calibration = json.loads(str(latest_artifact["calibration_json"] or "{}"))
        return ModelManifest(
            model_version=str(latest_artifact["model_version"]),
            base_model=str(params.get("base_model", BASE_MODEL)),
            half_life_years=float(params.get("half_life_years", HALF_LIFE_YEARS)),
            xi=float(params.get("xi", round(_DEFAULT_XI, 9))),
            markets=tuple(calibration.get("markets", GOAL_MARKETS)),
            calibration=calibration,
            predictive_validation_run_id=_maybe_int(
                latest_artifact["predictive_validation_run_id"]
            ),
            prediction_run_id=_maybe_int(payload.get("prediction_run_id"))
            or prediction_run_id
            or _latest_prediction_run(conn),
            market_validation_status=str(
                latest_artifact["market_validation_status"] or MARKET_VALIDATION_STATUS
            ),
            trained_until=str(latest_artifact["trained_until"]),
            training_policy=_artifact_training_policy(payload),
            training_rows=int(latest_artifact["training_rows"]),
            model_artifact_id=str(latest_artifact["artifact_id"]),
            code_git_sha=_maybe_str(latest_artifact["code_git_sha"]),
        )

    artifact = build_model_artifact(conn, prediction_run_id=prediction_run_id)
    payload = artifact.payload
    return ModelManifest(
        model_version=MODEL_VERSION,
        base_model=BASE_MODEL,
        half_life_years=HALF_LIFE_YEARS,
        xi=round(_DEFAULT_XI, 9),
        markets=GOAL_MARKETS,
        calibration=_calibration_payload(),
        predictive_validation_run_id=_maybe_int(payload.get("predictive_validation_run_id")),
        prediction_run_id=prediction_run_id or _latest_prediction_run(conn),
        market_validation_status=MARKET_VALIDATION_STATUS,
        trained_until=TRAINED_UNTIL,
        training_policy=_training_policy_payload(),
        training_rows=int(payload["training_rows"]),
        model_artifact_id=artifact.artifact_id,
        code_git_sha=_maybe_str(payload.get("code_git_sha")),
    )


def build_model_artifact(
    conn: sqlite3.Connection,
    *,
    prediction_run_id: int | None = None,
    calibration_coefficients: dict[str, dict[str, object]] | None = None,
) -> ModelArtifact:
    validation_run_id = _latest_passed_predictive_market_run(conn)
    payload = {
        "model_version": MODEL_VERSION,
        "base_model": BASE_MODEL,
        "params": _params_payload(),
        "calibration": _calibration_payload(calibration_coefficients),
        "predictive_validation_run_id": validation_run_id,
        "market_validation_status": MARKET_VALIDATION_STATUS,
        "trained_until": TRAINED_UNTIL,
        "training_policy": _training_policy_payload(),
        "training_rows": _training_rows(conn),
        "code_git_sha": _run_git_sha(conn, prediction_run_id),
        "run_id_scope": "sqlite_database_local",
    }
    artifact_id = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ModelArtifact(artifact_id=artifact_id, payload=payload)


def persist_model_artifact(
    conn: sqlite3.Connection,
    *,
    artifact: ModelArtifact,
    prediction_run_id: int,
) -> None:
    payload = dict(artifact.payload)
    payload["prediction_run_id"] = prediction_run_id
    params = payload["params"]
    calibration = payload["calibration"]
    conn.execute(
        """
        INSERT OR REPLACE INTO model_artifact (
          artifact_id, model_version, predictive_validation_run_id,
          market_validation_status, trained_until, training_rows, params_json,
          calibration_json, code_git_sha, details_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artifact.artifact_id,
            MODEL_VERSION,
            payload["predictive_validation_run_id"],
            payload["market_validation_status"],
            payload["trained_until"],
            payload["training_rows"],
            json.dumps(params, sort_keys=True),
            json.dumps(calibration, sort_keys=True),
            payload["code_git_sha"],
            json.dumps(payload, sort_keys=True),
        ),
    )


def _params_payload() -> dict[str, object]:
    return {
        "base_model": BASE_MODEL,
        "half_life_years": HALF_LIFE_YEARS,
        "xi": round(_DEFAULT_XI, 9),
        "l2": 1.0,
        "training_policy_id": TRAINING_POLICY.policy_id,
    }


def _training_policy_payload() -> dict[str, str]:
    return TRAINING_POLICY.as_dict()


def _artifact_training_policy(payload: dict[str, object]) -> dict[str, str]:
    policy = payload.get("training_policy")
    if isinstance(policy, dict):
        return {str(key): str(value) for key, value in policy.items()}
    return _training_policy_payload()


def _calibration_payload(
    coefficients: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": CALIBRATION_TYPE,
        "mode": CALIBRATION_MODE,
        "version": CALIBRATION_VERSION,
        "markets": list(GOAL_MARKETS),
    }
    if coefficients:
        payload["coefficients"] = coefficients
    return payload


def _latest_passed_predictive_market_run(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        """
        SELECT dq.run_id
        FROM data_quality dq
        JOIN ingest_run ir ON ir.run_id = dq.run_id
        WHERE dq.check_name IN ('predictive_baseline_gate', 'goal_market_gate')
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


def _latest_model_artifact(
    conn: sqlite3.Connection,
    *,
    prediction_run_id: int | None,
) -> sqlite3.Row | None:
    filter_sql = ""
    params: tuple[object, ...] = (MODEL_VERSION,)
    if prediction_run_id is not None:
        filter_sql = """
          AND EXISTS (
            SELECT 1
            FROM prediction p
            WHERE p.artifact_id = ma.artifact_id
              AND p.run_id = ?
          )
        """
        params = (MODEL_VERSION, prediction_run_id)
    return conn.execute(
        f"""
        SELECT *
        FROM model_artifact ma
        WHERE ma.model_version = ?
          {filter_sql}
        ORDER BY ma.created_utc DESC, ma.artifact_id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()


def _training_rows(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM match_history
            WHERE date < ?
            """,
            (TRAINING_POLICY.trained_until,),
        ).fetchone()[0]
    )


def _run_git_sha(conn: sqlite3.Connection, run_id: int | None) -> str | None:
    if run_id is None:
        return None
    row = conn.execute(
        """
        SELECT code_git_sha
        FROM ingest_run
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    return _maybe_str(row["code_git_sha"]) if row else None


def _maybe_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _maybe_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
