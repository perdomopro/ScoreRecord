import sqlite3
import json
from datetime import date
from scorerecord.evaluate.shots_process_benchmark import run_shots_process_benchmark

conn = sqlite3.connect(":memory:")
conn.execute(
    """CREATE TABLE historical_match (
        league_code TEXT,
        season TEXT,
        match_date TEXT,
        home_team TEXT,
        away_team TEXT,
        home_goals INTEGER,
        away_goals INTEGER,
        raw_json TEXT
    )"""
)

for i in range(1, 10):
    conn.execute(
        "INSERT INTO historical_match VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "EPL", "2020-2021", f"2020-01-0{i}T12:00:00Z",
            "TeamA", "TeamB",
            2, 0,
            json.dumps({"HS": 20, "AS": 5, "HST": 8, "AST": 1})
        )
    )

report = run_shots_process_benchmark(
    conn,
    from_date=date(2020, 1, 1),
    to_date=date(2020, 1, 31),
    leagues=("EPL",),
    min_train=2,
)

# Try the exact query
query = f"""
    SELECT match_date
    FROM historical_match
    WHERE league_code IN ('EPL')
      AND match_date >= ?
      AND match_date <= ?
"""
params = ["2020-01-01", "2020-01-31"]
rows2 = conn.execute(query, params).fetchall()
print(f"Query rows: {len(rows2)}")
