from datetime import date

from scorerecord.ingest import provider_stats_probe
from scorerecord.ingest.provider_stats_probe import (
    ProviderProbeError,
    _provider_search_names,
    _sportmonks_league_id,
    _sportmonks_has_corners,
    _thestatsapi_has_corners,
    run_provider_stats_probe,
)


def test_provider_stats_probe_reports_missing_auth() -> None:
    reports = run_provider_stats_probe(
        leagues=("EPL",),
        providers=("sportmonks", "thestatsapi"),
        sportmonks_key=None,
        sportmonks_base_url="https://example.invalid",
        thestatsapi_key=None,
        thestatsapi_base_url="https://example.invalid",
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 10),
        sample_size=3,
    )

    assert [report.status for report in reports] == ["AUTH_MISSING", "AUTH_MISSING"]


def test_sportmonks_corners_detection_uses_type_id_34() -> None:
    assert _sportmonks_has_corners(
        {
            "statistics": [
                {"type_id": 86, "data": {"value": 5}},
                {"type_id": 34, "data": {"value": 7}},
            ]
        }
    )


def test_sportmonks_corners_detection_uses_type_code() -> None:
    assert _sportmonks_has_corners(
        {"statistics": [{"type": {"code": "corners"}, "data": {"value": 4}}]}
    )


def test_thestatsapi_corners_detection_scans_stats_payload() -> None:
    assert _thestatsapi_has_corners(
        {
            "data": {
                "overview": {
                    "corners": {
                        "home": 6,
                        "away": 3,
                    }
                }
            }
        }
    )


def test_provider_search_names_include_common_aliases() -> None:
    assert _provider_search_names("MLS")[0] == "Major League Soccer"
    assert "Liga BBVA MX" in _provider_search_names("LIGA_MX")
    assert "Liga Profesional Argentina" in _provider_search_names("ARGENTINA")


def test_sportmonks_league_id_reports_subscription_access(monkeypatch) -> None:
    def fake_fetch_json(url: str, *, headers: dict[str, str]):
        return {
            "data": [],
            "message": (
                "No result(s) found matching your request. Either the query did not return "
                "any results or you don't have access to it via your current subscription."
            ),
        }

    monkeypatch.setattr(provider_stats_probe, "_fetch_json", fake_fetch_json)

    try:
        _sportmonks_league_id("EPL", api_token="secret", base_url="https://example.test")
    except ProviderProbeError as exc:
        assert exc.status == "PROVIDER_ACCESS_MISSING"
    else:  # pragma: no cover - defensive assertion path
        raise AssertionError("expected ProviderProbeError")
