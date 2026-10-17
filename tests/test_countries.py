"""Справочник перечня 430-р и три состояния страны (Э6-3, РЕШ-241/259/260)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app import countries
from app.countries import CountryStatus, country_status, load_reference

_YAML = Path(countries.__file__).parent / "reference" / "countries_gov.yaml"
_CODE_RE = re.compile(r"^[A-Z]{2}$")
_EU = (
    *("AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI"),
    *("FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU"),
    *("MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"),
)


@pytest.mark.parametrize("code", ["US", "TW", "CN", "RU", "NO"])
def test_business_mode_is_main_for_every_country(code: str) -> None:
    assert country_status(code, "business") is CountryStatus.MAIN


def test_gov_mode_country_outside_list_is_main() -> None:
    assert country_status("CN", "gov") is CountryStatus.MAIN


@pytest.mark.parametrize("code", ["US", "DE", "JP", "KR", "GB", "NO"])
def test_gov_mode_listed_country_is_footnote(code: str) -> None:
    assert country_status(code, "gov") is CountryStatus.FOOTNOTE


def test_gov_mode_taiwan_is_excluded_though_listed_in_430r() -> None:
    assert "TW" in countries.unfriendly_codes()
    assert country_status("TW", "gov") is CountryStatus.EXCLUDED


@pytest.mark.parametrize("code", ["us", "USA", "", "U", "U1", "УС"])
def test_bad_code_raises_value_error(code: str) -> None:
    with pytest.raises(ValueError):
        country_status(code, "gov")
    with pytest.raises(ValueError):
        country_status(code, "business")


@pytest.mark.parametrize("mode", ["world", "", "Gov", "BUSINESS"])
def test_unknown_mode_raises_value_error(mode: str) -> None:
    with pytest.raises(ValueError):
        country_status("US", mode)


def test_non_string_code_raises_value_error() -> None:
    with pytest.raises(ValueError):
        country_status(False, "gov")  # type: ignore[arg-type]


def test_every_code_in_reference_is_two_letter_string() -> None:
    data = load_reference()
    for section in ("unfriendly_430r", "gov_excluded"):
        for entry in data[section]:
            assert isinstance(entry["code"], str), entry
            assert _CODE_RE.fullmatch(entry["code"]), entry


def test_yaml_codes_are_quoted_in_source_text() -> None:
    """Ловушка YAML: `NO` без кавычек читается как `false` (инвариант №10)."""
    text = _YAML.read_text(encoding="utf-8")
    code_fields = re.findall(r"code:\s*([^,}\s]+)", text)
    assert code_fields
    assert all(re.fullmatch(r'"[A-Z]{2}"', value) for value in code_fields), code_fields
    assert "NO" in countries.unfriendly_codes()


def test_unquoted_no_would_break_loader(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Загрузчик отвергает код-булево, а не молча теряет Норвегию."""
    broken = tmp_path / "broken.yaml"
    broken.write_text("unfriendly_430r:\n  - {code: NO, name_ru: x}\ngov_excluded: []\n", encoding="utf-8")
    monkeypatch.setattr(countries, "_REFERENCE_PATH", broken)
    load_reference.cache_clear()
    try:
        with pytest.raises(ValueError):
            load_reference()
    finally:
        monkeypatch.undo()
        load_reference.cache_clear()


def test_no_duplicate_codes() -> None:
    data = load_reference()
    for section in ("unfriendly_430r", "gov_excluded"):
        codes = [entry["code"] for entry in data[section]]
        assert len(codes) == len(set(codes)), section


def test_all_eu_member_states_are_in_list() -> None:
    assert len(_EU) == 27
    assert set(_EU) <= countries.unfriendly_codes()


def test_reference_carries_source_edition_and_check_date() -> None:
    data = yaml.safe_load(_YAML.read_text(encoding="utf-8"))
    assert data["source_url"].startswith("http")
    assert data["edition"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", data["checked_at"])
    assert data["gov_excluded"] == [{"code": "TW", "reason": "РЕШ-241, РЕШ-260"}]
    assert all(entry["name_ru"] for entry in data["unfriendly_430r"])


def test_load_reference_is_cached() -> None:
    assert load_reference() is load_reference()
