"""Согласованность `.env.example` с настройками приложения (Т-25).

Пример окружения — единственная инструкция, по которой владелец заполняет `.env`.
Переменная, которую читает `Settings`, но которой нет в примере, молча остаётся
со значением по умолчанию: источник не включается, порог не работает, и понять
это можно только чтением кода. Обратное так же плохо — заполненная по примеру
переменная, которой в `Settings` уже нет, создаёт ложное ощущение настройки.
Поэтому множества имён сверяются в обе стороны.

Второй сюжет — кодировка. Файл дважды прошёл через cp1251 и стал нечитаемым:
русские комментарии превратились в «РїСѓС‚СЊ». Здесь проверяется, что файл
читается как UTF-8 без BOM и следов повторного кодирования в нём нет.

Третий — инвариант №3: в примере окружения значений секретов не бывает.
"""

from __future__ import annotations

import re

import pytest

from app.config import REPO_ROOT, Settings

ENV_EXAMPLE = REPO_ROOT / ".env.example"

#: Строка вида `NAME=` — с решёткой в начале строки или без неё. Закомментированные
#: переменные (`# DATABASE_URL=...`) — тоже часть инструкции: они показывают формат
#: значения, которое по умолчанию не задают.
_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=")

#: Следы двойного кодирования UTF-8 → cp1251 → UTF-8. Кириллический текст в такой
#: файл попадает кусками «Р…», «С…», «в”…»; в нормальном русском тексте эти пары
#: подряд не встречаются.
_MOJIBAKE_MARKERS = ("РїСѓ", "СЂРµ", "Р°Р№", "в”Ђ", "Ð¿Ñ")


def _env_example_text() -> str:
    return ENV_EXAMPLE.read_bytes().decode("utf-8")


def _example_names() -> set[str]:
    """Имена переменных, объявленных в `.env.example`."""
    names: set[str] = set()
    for line in _env_example_text().splitlines():
        payload = line.strip()
        if payload.startswith("#"):
            payload = payload.lstrip("#").strip()
        match = _ASSIGNMENT.match(payload)
        if match:
            names.add(match.group(1))
    return names


def _settings_names() -> set[str]:
    """Имена переменных окружения, которые читает `Settings`.

    У части полей задан `validation_alias` — тогда именем окружения служит он,
    иначе имя поля в верхнем регистре (`case_sensitive=False`).
    """
    names: set[str] = set()
    for name, field in Settings.model_fields.items():
        alias = field.validation_alias
        names.add(alias if isinstance(alias, str) else name.upper())
    return names


def test_пример_окружения_читается_как_utf8_без_bom():
    """Файл открывается как UTF-8 без BOM: с BOM первая переменная получила бы
    в имени невидимый символ, и `.env` из такого примера не заработал бы."""
    raw = ENV_EXAMPLE.read_bytes()

    assert not raw.startswith(b"\xef\xbb\xbf"), "в .env.example появился BOM"
    text = raw.decode("utf-8")
    assert "ТехМост Скаут" in text


def test_в_примере_окружения_нет_следов_двойного_кодирования():
    """Русские комментарии читаются человеком, а не выглядят как «РїСѓС‚СЊ»."""
    text = _env_example_text()

    found = [marker for marker in _MOJIBAKE_MARKERS if marker in text]
    assert found == [], f"следы повторного кодирования: {found}"


#: Переменные JSON-API (РЕШ-250) и экспресс-скана (Э6-6а), которые владелец
#: дописывает в `.env.example` сам: шаблон окружения правит только он. API
#: описан в `docs/deploy.md` §3, скан опишет Э6-6в.
#: Список закрытый: как только имя появится в примере, тест ниже потребует
#: убрать его отсюда, — исключение не переживёт правку владельца молча.
_AWAITING_OWNER_IN_EXAMPLE = frozenset(
    {
        "SCOUT_API_KEYS",
        "API_RATE_LIMIT_RUNS_PER_HOUR",
        "API_RATE_LIMIT_READS_PER_MINUTE",
        "PUBLIC_SCAN_HOST",
        "SCAN_COST_LIMIT_RUB",
        "SCAN_DAILY_BUDGET_RUB",
        "SCAN_LIMIT_PER_IP_PER_DAY",
        "SCAN_ORDER_EMAIL",
        "SCAN_ORDER_TELEGRAM_URL",
        "YANDEX_SEARCH_RUB_PER_QUERY",
    }
)


def test_каждая_переменная_settings_объявлена_в_примере():
    """Настройка, которой нет в примере, до `.env` владельца не доедет."""
    missing = sorted(_settings_names() - _example_names() - _AWAITING_OWNER_IN_EXAMPLE)

    assert missing == [], f"нет в .env.example: {missing}"


def test_исключения_ждущие_владельца_ещё_не_в_примере_и_есть_в_settings():
    """Исключение выше — только для настоящих имён `Settings`, ещё не внесённых в пример."""
    assert _settings_names() >= _AWAITING_OWNER_IN_EXAMPLE
    already_there = sorted(_AWAITING_OWNER_IN_EXAMPLE & _example_names())
    assert already_there == [], f"уже в .env.example, уберите из _AWAITING_OWNER_IN_EXAMPLE: {already_there}"


def test_в_примере_нет_переменных_которых_settings_не_читает():
    """Обратная сторона: переменная-призрак обещает настройку, которой нет."""
    extra = sorted(_example_names() - _settings_names())

    assert extra == [], f"нет в Settings: {extra}"


@pytest.mark.parametrize("marker", ["sk-", "Api-Key", "Bearer "])
def test_в_примере_окружения_нет_похожего_на_ключ(marker: str):
    """Инвариант №3: пример хранит имена переменных, а не их значения.

    Слов `deepseek` и `yandex` в списке маркеров нет намеренно: это имена
    провайдеров, и они обязаны стоять в `LLM_STAGE_ROUTES` и
    `LLM_FALLBACK_ORDER` — иначе пример не объясняет, как маршрутизируются
    стадии. Секретом провайдера не делает ни одно вхождение; секрет — это
    значение, а значений здесь нет (тест ниже).
    """
    assert marker not in _env_example_text()


def test_переменные_с_ключами_и_токенами_объявлены_пустыми():
    """У ключа, токена и пароля-секрета в примере нет значения — даже
    «демонстрационного»: скопированный в `.env` пример не должен включить
    платный источник и не должен выглядеть заполненным."""
    with_values = {}
    for line in _env_example_text().splitlines():
        match = _ASSIGNMENT.match(line.strip())
        if not match:
            continue
        name = match.group(1)
        value = line.strip()[len(name) + 1 :]
        if name.endswith(("_KEY", "_TOKEN", "_SECRET")) and value:
            with_values[name] = value

    assert with_values == {}, f"значения секретов в .env.example: {sorted(with_values)}"


def test_настройки_набора_oai_киберленинки_больше_нет():
    """Т-16 (РЕШ-68) перевёл источник на собственный поисковый API, наборов у
    которого не существует. Настройка, которую не читает ни одна строка кода,
    выглядит рычагом, который ни на что не влияет."""
    assert "cyberleninka_oai_set" not in Settings.model_fields


def test_маршруты_по_умолчанию_все_стадии_на_deepseek_резерв_yandex():
    """РЕШ-171: без единой переменной окружения `Settings()` маршрутизирует
    все стадии на DeepSeek, а резерв — `deepseek,yandex` (второй провайдер
    выпилен вместе с бесплатным тарифом)."""
    settings = Settings(_env_file=None)

    routes = settings.stage_routes()
    assert routes, "маршруты стадий не заданы вовсе"
    assert set(routes.values()) == {"deepseek"}, f"не все стадии идут на DeepSeek: {routes}"
    assert settings.fallback_order() == ["deepseek", "yandex"]


def test_second_pass_relevance_max_умолчание_шесть():
    """РЕШ-103, долг ворот Э2-Ж (та же болезнь С-2, что и с `relevance_min`):
    без единой переменной окружения порог второго прохода — рабочее значение
    калибровки 6, а не 0. Прежде это число проверял только один тест
    (`test_pipeline_second_pass.py::test_при_выключенном_пороге_прогон_совпадает_с_прежним`,
    `assert settings.second_pass_relevance_max == 0`), который задача Д-3
    сознательно переписала под явный `threshold=0` — эта строка проверяла
    сценарий «порог выключен», а не сам факт умолчания, и после правки во
    всём наборе не осталось ни одной проверки конкретного числа. Регрессия
    умолчания обратно к 0 (случайный мердж, опечатка при рефакторинге
    `config.py`) проходила бы весь `pytest` незамеченной — этот тест закрывает
    дыру (находка ревью, цикл 1)."""
    settings = Settings(_env_file=None)

    assert settings.second_pass_relevance_max == 6


def test_reviewer_name_пусто_без_переменной_иначе_её_значение(monkeypatch: pytest.MonkeyPatch):
    """РЕШ-197: без `REVIEWER_NAME` поле пустое, с переменной окружения — её значение.

    Пустая строка — не отказ формы вердикта здесь (это делает `verdicts.py`),
    а просто умолчание настройки: форма без предзаполнения просит имя вручную.
    """
    monkeypatch.delenv("REVIEWER_NAME", raising=False)
    assert Settings(_env_file=None).reviewer_name == ""

    monkeypatch.setenv("REVIEWER_NAME", "Иван Иванов")
    assert Settings(_env_file=None).reviewer_name == "Иван Иванов"


def test_demo_duration_seconds_умолчание_150():
    """РЕШ-201: без переменной окружения демо воспроизводится за 150 секунд."""
    assert Settings(_env_file=None).demo_duration_seconds == 150.0
