from __future__ import annotations

import sqlite3
import unicodedata


SEED_ALIASES: dict[str, list[str]] = {
    "Argentina": ["ARG", "Argentine"],
    "Australia": ["AUS"],
    "Austria": ["AUT"],
    "Belgium": ["BEL"],
    "Bosnia and Herzegovina": ["Bosnia & Herzegovina", "Bosnia-Herzegovina", "BIH"],
    "Brazil": ["BRA", "Brasil"],
    "Canada": ["CAN"],
    "Chile": ["CHI"],
    "Colombia": ["COL"],
    "Costa Rica": ["CRC"],
    "Croatia": ["CRO"],
    "Czech Republic": ["Czechia", "CZE"],
    "China": ["China PR", "People's Republic of China", "CHN"],
    "Cote d'Ivoire": ["Côte d'Ivoire", "Ivory Coast", "CIV"],
    "Denmark": ["DEN"],
    "Ecuador": ["ECU"],
    "England": ["ENG"],
    "France": ["FRA"],
    "Germany": ["GER"],
    "Ghana": ["GHA"],
    "Iran": ["IR Iran", "IRN"],
    "Italy": ["ITA"],
    "Japan": ["JPN"],
    "Mexico": ["MEX"],
    "Morocco": ["MAR"],
    "Netherlands": ["Holland", "NED"],
    "Nigeria": ["NGA"],
    "Poland": ["POL"],
    "Portugal": ["POR"],
    "Qatar": ["QAT"],
    "Saudi Arabia": ["KSA", "Saudi"],
    "Scotland": ["SCO"],
    "Senegal": ["SEN"],
    "Serbia": ["SRB"],
    "South Africa": ["RSA", "South Africa"],
    "South Korea": ["Korea Republic", "Republic of Korea", "KOR"],
    "Spain": ["ESP"],
    "Switzerland": ["SUI"],
    "Tunisia": ["TUN"],
    "Turkey": ["Türkiye", "Turkiye", "TUR"],
    "Ukraine": ["UKR"],
    "United States": ["USA", "US", "United States of America"],
    "Uruguay": ["URU"],
    "Wales": ["WAL"],
}


def normalize_name(name: str | None) -> str | None:
    if name is None:
        return None
    cleaned = " ".join(name.strip().split())
    return cleaned or None


def normalize_key(name: str | None) -> str | None:
    cleaned = normalize_name(name)
    if cleaned is None:
        return None
    folded = unicodedata.normalize("NFKD", cleaned.casefold())
    ascii_only = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return "".join(ch for ch in ascii_only if ch.isalnum())


def get_or_create_team(conn: sqlite3.Connection, name: str) -> int:
    canonical = normalize_name(name)
    if canonical is None:
        raise ValueError("Team name cannot be empty")

    alias_key = normalize_key(canonical)
    seeded_team_id = _seed_alias_team_id(conn, alias_key)
    if seeded_team_id is not None:
        return seeded_team_id

    team_id = _best_existing_team_id(conn, {canonical})
    if team_id is not None:
        return team_id

    return _create_team(conn, canonical)


def seed_team_aliases(conn: sqlite3.Connection) -> None:
    for canonical, aliases in SEED_ALIASES.items():
        alias_set = {canonical, *aliases}
        team_id = _best_existing_team_id(conn, alias_set, preferred_names={canonical})
        if team_id is None:
            team_id = _create_team(conn, canonical)

        for alias in alias_set:
            normalized = normalize_name(alias)
            if normalized:
                alias_key = normalize_key(normalized)
                conn.execute(
                    "UPDATE team_alias SET team_id = ?, source = ? WHERE alias_key = ?",
                    (team_id, "seed", alias_key),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO team_alias (alias, alias_key, team_id, source)
                    VALUES (?, ?, ?, ?)
                    """,
                    (normalized, alias_key, team_id, "seed"),
                )
        _prune_unreferenced_seed_duplicates(conn, alias_set, keep_team_id=team_id)
    conn.commit()


def get_or_create_player(conn: sqlite3.Connection, name: str, team_id: int | None) -> int:
    canonical = normalize_name(name)
    if canonical is None:
        raise ValueError("Player name cannot be empty")

    row = conn.execute(
        "SELECT player_id FROM player WHERE canonical = ? AND team_id IS ?",
        (canonical, team_id),
    ).fetchone()
    if row:
        return int(row["player_id"])

    cur = conn.execute(
        "INSERT INTO player (canonical, team_id) VALUES (?, ?)",
        (canonical, team_id),
    )
    player_id = int(cur.lastrowid)
    conn.execute(
        "INSERT OR IGNORE INTO player_alias (alias, player_id, source) VALUES (?, ?, ?)",
        (canonical, player_id, "canonical"),
    )
    return player_id


def _create_team(conn: sqlite3.Connection, canonical: str) -> int:
    canonical_key = normalize_key(canonical)
    cur = conn.execute(
        "INSERT INTO team (canonical, canonical_key) VALUES (?, ?)",
        (canonical, canonical_key),
    )
    team_id = int(cur.lastrowid)
    conn.execute(
        "INSERT OR IGNORE INTO team_alias (alias, alias_key, team_id, source) VALUES (?, ?, ?, ?)",
        (canonical, canonical_key, team_id, "canonical"),
    )
    return team_id


def _seed_alias_team_id(conn: sqlite3.Connection, alias_key: str | None) -> int | None:
    if not alias_key:
        return None
    row = conn.execute(
        """
        SELECT team_id
        FROM team_alias
        WHERE alias_key = ? AND source = 'seed'
        """,
        (alias_key,),
    ).fetchone()
    if row is None:
        return None
    return int(row["team_id"])


def _best_existing_team_id(
    conn: sqlite3.Connection,
    names: set[str],
    *,
    preferred_names: set[str] | None = None,
) -> int | None:
    keys = [key for key in (normalize_key(name) for name in names) if key]
    if not keys:
        return None
    preferred_keys = {
        key for key in (normalize_key(name) for name in (preferred_names or set())) if key
    }
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"""
        SELECT DISTINCT team_id, canonical_key
        FROM (
          SELECT team_id, canonical_key FROM team WHERE canonical_key IN ({placeholders})
          UNION
          SELECT t.team_id, t.canonical_key
          FROM team_alias ta
          JOIN team t ON t.team_id = ta.team_id
          WHERE ta.alias_key IN ({placeholders})
        )
        """,
        (*keys, *keys),
    ).fetchall()
    if not rows:
        return None

    candidate_keys = {int(row["team_id"]): str(row["canonical_key"]) for row in rows}
    candidates = list(candidate_keys)
    return max(
        candidates,
        key=lambda team_id: (
            _team_reference_count(conn, team_id),
            int(candidate_keys[team_id] in preferred_keys),
            -team_id,
        ),
    )


def _team_reference_count(conn: sqlite3.Connection, team_id: int) -> int:
    checks = [
        ("match_history", "home_id"),
        ("match_history", "away_id"),
        ("fixture", "home_id"),
        ("fixture", "away_id"),
        ("player", "team_id"),
        ("team_rating", "team_id"),
        ("team_rating_history", "team_id"),
    ]
    total = 0
    for table_name, column_name in checks:
        total += int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table_name} WHERE {column_name} = ?",
                (team_id,),
            ).fetchone()[0]
        )
    return total


def _prune_unreferenced_seed_duplicates(
    conn: sqlite3.Connection,
    names: set[str],
    *,
    keep_team_id: int,
) -> None:
    keys = [key for key in (normalize_key(name) for name in names) if key]
    if not keys:
        return
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT team_id FROM team WHERE canonical_key IN ({placeholders})",
        keys,
    ).fetchall()
    for row in rows:
        team_id = int(row["team_id"])
        if team_id == keep_team_id or _team_reference_count(conn, team_id) > 0:
            continue
        conn.execute("DELETE FROM team_alias WHERE team_id = ?", (team_id,))
        conn.execute("DELETE FROM team WHERE team_id = ?", (team_id,))
