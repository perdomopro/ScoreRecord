import sys
from pathlib import Path

path = Path("tests/test_gui.py")
text = path.read_text(encoding="utf8")

text = text.replace(
    'render_today_forecast(data, refresh_seconds=60)',
    'render_today_forecast(data, settings=Settings(db_path=Path(""), raw_dir=Path("")), refresh_seconds=60)'
)
text = text.replace(
    'render_today_forecast(ok_data, refresh_seconds=60)',
    'render_today_forecast(ok_data, settings=Settings(db_path=Path(""), raw_dir=Path("")), refresh_seconds=60)'
)
text = text.replace(
    'render_match_forecast(data, "m-forecast")',
    'render_match_forecast(data, "m-forecast", settings=Settings(db_path=Path(""), raw_dir=Path("")))'
)

path.write_text(text, encoding="utf8")
