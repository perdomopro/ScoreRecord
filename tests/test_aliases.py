import sqlite3

from scorerecord.aliases import get_or_create_team, normalize_key, seed_team_aliases
from scorerecord.db import init_db


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_normalize_key_folds_case_and_accents() -> None:
    assert normalize_key(" C\u00f4te d'Ivoire ") == "cotedivoire"
    assert normalize_key("T\u00dcRKIYE") == "turkiye"
    assert normalize_key("Korea Republic") == "korearepublic"


def test_seed_aliases_reuses_existing_alias_group() -> None:
    conn = _conn()

    ivory_id = get_or_create_team(conn, "Ivory Coast")
    seed_team_aliases(conn)

    cote_id = get_or_create_team(conn, "Cote d'Ivoire")
    assert cote_id == ivory_id


def test_get_or_create_prefers_seeded_canonical_team_on_alias_tie() -> None:
    conn = _conn()

    duplicate_id = get_or_create_team(conn, "Bosnia & Herzegovina")
    historical_id = get_or_create_team(conn, "Bosnia and Herzegovina")
    conn.execute(
        """
        INSERT INTO match_history (
          source_match_id, date, home_id, away_id, home_score, away_score
        )
        VALUES ('m1', '2024-01-01', ?, ?, 1, 0)
        """,
        (historical_id, duplicate_id),
    )
    seed_team_aliases(conn)

    resolved_id = get_or_create_team(conn, "Bosnia & Herzegovina")

    assert resolved_id == historical_id
