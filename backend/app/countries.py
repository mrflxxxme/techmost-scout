"""Три состояния страны в режиме подачи (РЕШ-241, РЕШ-259, РЕШ-260).

Единственный признак «страна допустима в режиме»: отбор аналогов, выгрузки и
вид экспресс-скана зовут `country_status` и не считают его сами (РЕШ-213).
Справочник — `reference/countries_gov.yaml` (перечень распоряжения Правительства
РФ № 430-р, дата сверки и ссылка на редакцию лежат в файле).

Чистые функции без сети и без базы (инвариант №7).
"""

from __future__ import annotations

import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_REFERENCE_PATH = Path(__file__).resolve().parent / "reference" / "countries_gov.yaml"
_CODE_RE = re.compile(r"^[A-Z]{2}$")
_MODES = ("business", "gov")


class CountryStatus(StrEnum):
    """Состояние страны в режиме: основная выдача, сноска или исключена."""

    MAIN = "main"
    FOOTNOTE = "footnote"
    EXCLUDED = "excluded"


def _check_code(code: object) -> str:
    """Вернуть код как есть или поднять `ValueError`, если это не `^[A-Z]{2}$`."""
    if not isinstance(code, str) or not _CODE_RE.fullmatch(code):
        raise ValueError(f"Код страны должен быть двумя заглавными латинскими буквами, получено: {code!r}")
    return code


def _codes(entries: Any, section: str) -> frozenset[str]:
    """Собрать коды секции справочника; дубль или нестрока — `ValueError`."""
    if not isinstance(entries, list):
        raise ValueError(f"Справочник стран: секция «{section}» должна быть списком")
    codes: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or "code" not in entry:
            raise ValueError(f"Справочник стран: запись без поля code в секции «{section}»")
        codes.append(_check_code(entry["code"]))
    if len(set(codes)) != len(codes):
        raise ValueError(f"Справочник стран: дубли кодов в секции «{section}»")
    return frozenset(codes)


@lru_cache(maxsize=1)
def load_reference() -> dict[str, Any]:
    """Прочитать и проверить справочник; результат кэшируется на процесс."""
    data = yaml.safe_load(_REFERENCE_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Справочник стран: корень файла должен быть словарём")
    _codes(data.get("unfriendly_430r"), "unfriendly_430r")
    _codes(data.get("gov_excluded"), "gov_excluded")
    return data


def unfriendly_codes() -> frozenset[str]:
    """Коды стран перечня № 430-р."""
    return _codes(load_reference()["unfriendly_430r"], "unfriendly_430r")


def excluded_codes() -> frozenset[str]:
    """Коды стран, исключённых из режима «Гос» полностью."""
    return _codes(load_reference()["gov_excluded"], "gov_excluded")


def country_status(code: str, mode: str) -> CountryStatus:
    """Состояние страны в режиме подачи.

    `business` — `main` для всех стран. `gov` — `excluded` для стран из
    `gov_excluded` (Тайвань), иначе `footnote` для стран перечня 430-р, иначе
    `main`: исключение сильнее сноски, потому что Тайвань есть и в перечне.
    Код не `^[A-Z]{2}$` или неизвестный режим — `ValueError`.
    """
    _check_code(code)
    if mode not in _MODES:
        raise ValueError(f"Неизвестный режим подачи: {mode!r}; допустимы {', '.join(_MODES)}")
    if mode == "business":
        return CountryStatus.MAIN
    if code in excluded_codes():
        return CountryStatus.EXCLUDED
    if code in unfriendly_codes():
        return CountryStatus.FOOTNOTE
    return CountryStatus.MAIN
