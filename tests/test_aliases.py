import sqlite3

from scorerecord.aliases import get_or_create_team, normalize_key, seed_team_aliases
from scorerecord.db import init_db


def test_normalize_key_folds_case_and_accents() -> None:
    assert normalize_key(" C\u00f4te d'Ivoire ") == "cotedivoire"
    assert normalize_key("T\u00dcRKIYE") == "turkiye"
    assert normalize_key("Korea Republic") == "korearepublic"


def test_seed_aliases_reuses_existing_alias_group() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)

    ivory_id = get_or_create_team(conn, "Ivory Coast")
    seed_team_aliases(conn)

    cote_id = get_or_create_team(conn, "Côte d'Ivoire")
    assert cote_id == ivory_id
