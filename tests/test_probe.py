"""Оснастка проб scripts/probe.py — тесты на подменах, без сети (З-4).

Инвариант №7 для исполнителя: сам скрипт исполнитель не запускает (платные
вызовы делает только оркестратор). Здесь проверяется код скрипта: разбор
аргументов, отсутствие сетевых вызовов на импорте, итоговая строка запроса
после сужения — всё на respx-подменах, без единого настоящего вызова.
"""

from __future__ import annotations

import base64
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import respx

from app.config import get_settings
from app.search.yandex_search import OPERATION_URL, SEARCH_ASYNC_URL

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_PATH = REPO_ROOT / "scripts" / "probe.py"


def _load_probe() -> ModuleType:
    """Загрузить scripts/probe.py как модуль — он не пакет и не импортируется штатно."""
    spec = importlib.util.spec_from_file_location("scripts_probe_under_test", PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _xml(url: str = "https://a.example.org/p", title: str = "Заголовок") -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<yandexsearch version='1.0'><response><results><grouping><group>"
        f"<doc><url>{url}</url><domain>example.org</domain><title>{title}</title>"
        "<modtime>20240115T103000</modtime>"
        "<passages><passage>Фрагмент найденной страницы.</passage></passages></doc>"
        "</group></grouping></results></response></yandexsearch>"
    )


def _operation_done(xml: str) -> dict:
    return {"done": True, "response": {"rawData": base64.b64encode(xml.encode("utf-8")).decode("ascii")}}


def _mock_search_ok() -> None:
    respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(return_value=httpx.Response(200, json=_operation_done(_xml())))


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Убрать реальные паузы адаптера (poll_interval, throttle) — проба идёт мгновенно."""

    async def _instant(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("app.search.yandex_search.asyncio.sleep", _instant)


# ── Критерий 1: --help ──────────────────────────────────────────────────────
def test_help_код_возврата_ноль_и_называет_оба_режима() -> None:
    # Дочерний процесс запускается с принудительным UTF-8 на вводе-выводе:
    # без этого на Windows он наследует кодовую страницу консоли (cp1251),
    # текст справки на кириллице бьёт по ней, и чтение stdout падает молча.
    child_env = dict(os.environ)
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"

    result = subprocess.run(  # noqa: S603 — фиксированный путь к интерпретатору и скрипту, без ввода извне
        [sys.executable, str(PROBE_PATH), "--help"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        env=child_env,
    )
    assert result.returncode == 0
    assert "--intake" in result.stdout
    assert "--query" in result.stdout


# ── Критерий 2: импорт не трогает сеть и не создаёт клиента/роутера ────────
@respx.mock
def test_импорт_не_делает_сетевых_вызовов() -> None:
    """respx.mock() без единого зарегистрированного маршрута: любой реальный
    HTTP-вызов на импорте упал бы с ошибкой несопоставленного запроса."""
    module = _load_probe()
    assert callable(module.main)
    assert callable(module.probe_intake)
    assert callable(module.probe_query)


# ── Критерий 3: итоговая строка запроса печатается ПОСЛЕ сужения ───────────
@respx.mock
async def test_режим_поиска_печатает_суженный_запрос_для_конкурса(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "unit-test-key")
    get_settings.cache_clear()
    _mock_search_ok()

    module = _load_probe()
    code = await module.probe_query("инновационный конкурс сенсоры износа", "contest", "ru")

    out = capsys.readouterr().out
    get_settings.cache_clear()

    assert code == 0
    assert "итоговый запрос после сужения" in out
    assert "site:" in out


@respx.mock
async def test_режим_поиска_без_сужения_для_web_без_грантовых_маркеров(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "unit-test-key")
    get_settings.cache_clear()
    _mock_search_ok()

    module = _load_probe()
    code = await module.probe_query("датчик износа режущего инструмента", "web", "ru")

    out = capsys.readouterr().out
    get_settings.cache_clear()

    assert code == 0
    assert "итоговый запрос после сужения" in out
    assert "site:" not in out


@respx.mock
async def test_режим_поиска_без_ключа_не_падает_и_сообщает_причину(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """YANDEX_SEARCH_API_KEY в тестовом окружении не задан (см. conftest.TEST_ENV)."""
    get_settings.cache_clear()

    module = _load_probe()
    code = await module.probe_query("любая формулировка", "web", "ru")

    out = capsys.readouterr().out
    get_settings.cache_clear()

    assert code == 1
    assert "YANDEX_SEARCH_API_KEY" in out


# ── Критерий 4: продукт о скрипте не знает ──────────────────────────────────
def test_продукт_не_ссылается_на_скрипт_пробы() -> None:
    """Критерий формально задан как `grep -rn "scripts/probe\\|probe" backend/app`,
    но взятый буквально этот шаблон цепляет и посторонние строки: например,
    `rationale="health probe"` в `search/base.py` и `search/yandex_search.py` —
    это имя переменной health-check в коде, который существовал до этой
    задачи, и к scripts/probe.py отношения не имеет. Реальный предмет критерия
    — продукт не ссылается на САМ СКРИПТ, поэтому проверяем узнаваемые формы
    отсылки к нему, а не голое вхождение слова «probe»."""
    needles = ("scripts/probe", "scripts\\probe", "probe.py", "scout probe")
    hits: list[str] = []
    backend_app = REPO_ROOT / "backend" / "app"
    for path in backend_app.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(needle in text for needle in needles):
            hits.append(str(path))
    assert hits == []
