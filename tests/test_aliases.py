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

def test_alias_gap_report() -> None:
    conn = _conn()
    get_or_create_team(conn, "Arsenal FC")
    get_or_create_team(conn, "Manchester City")
    
    from scorerecord.aliases import alias_gap_report
    
    res = alias_gap_report(conn, ["Arsenal FC", "Manchester City", "Unknown FC"])
    
    assert len(res) == 3
    
    arsenal = next(r for r in res if r["external_name"] == "Arsenal FC")
    assert arsenal["exact_alias_match"] is True
    assert arsenal["missing_team"] is False
    
    unknown = next(r for r in res if r["external_name"] == "Unknown FC")
    assert unknown["exact_alias_match"] is False
    assert unknown["missing_team"] is True


def test_argentina_provider_aliases_resolve_to_seeded_clubs() -> None:
    conn = _conn()

    seed_team_aliases(conn)

    expected = {
        "CA Aldosivi": "Aldosivi",
        "Argentinos Juniors": "Argentinos Jrs",
        "Atletico Tucuman": "Atl. Tucuman",
        "CA Banfield": "Banfield",
        "CA Barracas Central": "Barracas Central",
        "CA Belgrano de Cordoba": "Belgrano",
        "CA Central Cordoba SE": "Central Cordoba",
        "Deportivo Riestra AFBC": "Dep. Riestra",
        "Estudiantes de La Plata": "Estudiantes L.P.",
        "Gimnasia y Esgrima La Plata": "Gimnasia L.P.",
        "Gimnasia y Esgrima Mendoza": "Gimnasia Mendoza",
        "CA Huracan": "Huracan",
        "Independiente Rivadavia": "Ind. Rivadavia",
        "CA Independiente Avellaneda": "Independiente",
        "Instituto AC Cordoba": "Instituto",
        "CA Lanus": "Lanus",
        "CA Platense": "Platense",
        "Racing Club Avellaneda": "Racing Club",
        "CA River Plate (ARG)": "River Plate",
        "CA Rosario Central": "Rosario Central",
        "CA San Lorenzo de Almagro": "San Lorenzo",
        "CA Sarmiento Junin": "Sarmiento Junin",
        "CA Talleres de Cordoba": "Talleres Cordoba",
        "CA Tigre": "Tigre",
    }

    for provider_name, canonical in expected.items():
        team_id = get_or_create_team(conn, provider_name)
        row = conn.execute("SELECT canonical FROM team WHERE team_id = ?", (team_id,)).fetchone()
        assert row["canonical"] == canonical


def test_brasileirao_provider_aliases_resolve_to_seeded_clubs() -> None:
    conn = _conn()

    seed_team_aliases(conn)

    expected = {
        "Atletico Mineiro MG": "Atletico-MG",
        "Botafogo FR RJ": "Botafogo RJ",
        "CA Paranaense PR": "Athletico-PR",
        "CR Flamengo RJ": "Flamengo RJ",
        "CR Vasco da Gama RJ": "Vasco",
        "Clube do Remo PA": "Remo",
        "Coritiba FC PR": "Coritiba",
        "Cruzeiro EC MG": "Cruzeiro",
        "EC Bahia BA": "Bahia",
        "EC Vitoria BA": "Vitoria",
        "Fluminense FC RJ": "Fluminense",
        "Gremio FB Porto Alegrense RS": "Gremio",
        "Mirassol FC SP": "Mirassol",
        "Red Bull Bragantino SP": "Bragantino",
        "SC Corinthians SP": "Corinthians",
        "SC Internacional RS": "Internacional",
        "SE Palmeiras SP": "Palmeiras",
        "Santos FC SP": "Santos",
        "Sao Paulo FC SP": "Sao Paulo",
    }

    for provider_name, canonical in expected.items():
        team_id = get_or_create_team(conn, provider_name)
        row = conn.execute("SELECT canonical FROM team WHERE team_id = ?", (team_id,)).fetchone()
        assert row["canonical"] == canonical
