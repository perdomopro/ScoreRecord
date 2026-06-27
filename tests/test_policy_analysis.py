import json
import sqlite3

from scorerecord.db import init_db
from scorerecord.evaluate.policy_analysis import analyze_policy_grids


def _write_grid(tmp_path, run_id: int, league: str, roi: float, clv: float) -> str:
    grid_path = tmp_path / f"policy_grid_real_odds_{run_id}.csv"
    metadata_path = tmp_path / f"policy_grid_metadata_{run_id}.json"
    grid_path.write_text(
        "\n".join(
            [
                "min_score,min_edge,max_picks_per_day,market_shrink_alpha,ranking_strategy,"
                "num_bets,roi_capped_real,mean_clv_real,positive_clv_rate,clv_rows",
                f"0.075,0.05,3,0.75,recommendation_score,400,{roi},{clv},0.54,390",
            ]
        ),
        encoding="utf-8",
    )
    metadata_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "league_code": league,
                "from_date": "2021-01-01",
                "to_date": "2025-12-31",
            }
        ),
        encoding="utf-8",
    )
    return str(grid_path)


def test_analyze_policy_grids_aggregates_config_across_leagues(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    inputs = [
        _write_grid(tmp_path, 1, "E0", 0.02, 0.004),
        _write_grid(tmp_path, 2, "D1", 0.01, 0.002),
        _write_grid(tmp_path, 3, "SP1", 0.03, 0.003),
    ]

    summary = analyze_policy_grids(
        conn,
        inputs=inputs,
        out_path=tmp_path / "summary.csv",
    )

    text = summary.output_path.read_text(encoding="utf-8")
    assert summary.rows_written == 1
    assert summary.best_decision == "candidate"
    assert "market_shrink_alpha" in text
    assert "weighted_mean_clv_real" in text
    assert "E0" in text
    assert "D1" in text
    assert "SP1" in text
