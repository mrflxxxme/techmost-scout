"""Оснастка проб scripts/probe.py — тесты на подменах, без сети (З-4).

Инвариант №7 для исполнителя: сам скрипт исполнитель не запускает (платные
вызовы делает только оркестратор). Здесь проверяется код скрипта: разбор
аргументов, отсутствие сетевых вызовов на импорте, итоговая строка запроса
после сужения — всё на respx-подменах, без единого настоящего вызова.
"""

from __future__ import annotations

import base64
import importlib.util
import json
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


def _mock_search_ok(url: str = "https://a.example.org/p", title: str = "Заголовок") -> None:
    respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(
        return_value=httpx.Response(200, json=_operation_done(_xml(url=url, title=title)))
    )


def _empty_xml() -> str:
    """Ответ без единого `<doc>` — та же форма пустой выдачи, что у реального
    источника, когда он честно ничего не нашёл (критерий 4 задачи З-1)."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<yandexsearch version='1.0'><response><results><grouping></grouping></results></response></yandexsearch>"
    )


def _mock_search_empty() -> None:
    respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(return_value=httpx.Response(200, json=_operation_done(_empty_xml())))


#: Ответ extractor: один конкурсный кандидат. `provider` в ответе намеренно
#: чужой — правило 9 промпта модель нарушает регулярно, а код обязан навязать
#: своё значение (см. `extract_candidates`).
_CONTEST_EXTRACT_JSON = json.dumps(
    {
        "candidates": [
            {
                "source_type": "contest",
                "title": "Победители хакатона по сенсорам износа",
                "url": "https://university.example.org/news/winners",
                "org": "Технический университет",
                "authors": [],
                "year": 2025,
                "snippet": "Команда представила прототип датчика износа режущего инструмента.",
                "raw_id": None,
                "provider": "модель-выдумщица",
            }
        ],
        "dropped_no_url": 0,
    },
    ensure_ascii=False,
)

#: Ответ scorer: пять разных оценок и дословный `notes` — предмет пробы
#: (РЕШ-133): именно из notes видно, снижена ли оценка по мотиву «нет
#: методики и испытаний» или «тема мимо».
_CONTEST_SCORE_JSON = json.dumps(
    {
        "scores": [
            {
                "index": 0,
                "relevance": 2,
                "maturity": 3,
                "accessibility": 4,
                "freshness": 5,
                "completeness": 6,
                "notes": "нет методики и данных испытаний, тема по адресу",
            }
        ]
    },
    ensure_ascii=False,
)


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
    assert "--contest" in result.stdout


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


# ── Критерии 2-5: режим --contest идёт путём продукта ───────────────────────
async def test_режим_контеста_печатает_пять_оценок_notes_и_сводку(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_provider_factory
) -> None:
    """Критерий 2 (счётчики стадий 1 и 1), критерий 3 (пять оценок и notes
    дословно), критерий 4 (сводка при непустой выдаче) и критерий 5 (ключ не
    встречается в перехваченном выводе) — одним сквозным прогоном путём
    продукта: адаптер → extractor → scorer, все три под подменами."""
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "unit-test-key")
    get_settings.cache_clear()
    with respx.mock:
        _mock_search_ok(url="https://university.example.org/news/winners", title="Победители хакатона")

        extractor_provider = fake_provider_factory("gigachat", [_CONTEST_EXTRACT_JSON])
        scorer_provider = fake_provider_factory("deepseek", [_CONTEST_SCORE_JSON])

        from app.llm.router import LLMRouter as RealLLMRouter

        def _router_factory(settings=None, providers=None):
            return RealLLMRouter(settings, providers={"gigachat": extractor_provider, "deepseek": scorer_provider})

        monkeypatch.setattr("app.llm.LLMRouter", _router_factory)

        import app.pipeline.stages as stages_module

        original_extract = stages_module.extract_candidates
        original_score = stages_module.score_candidates
        calls = {"extract": 0, "score": 0}

        async def _counted_extract(*args: object, **kwargs: object):
            calls["extract"] += 1
            return await original_extract(*args, **kwargs)

        async def _counted_score(*args: object, **kwargs: object):
            calls["score"] += 1
            return await original_score(*args, **kwargs)

        monkeypatch.setattr("app.pipeline.stages.extract_candidates", _counted_extract)
        monkeypatch.setattr("app.pipeline.stages.score_candidates", _counted_score)

        module = _load_probe()
        code = await module.probe_contest("победители хакатона сенсоры износа")

    out = capsys.readouterr().out
    get_settings.cache_clear()

    assert code == 0
    # Критерий 2: ровно один вызов каждой стадии.
    assert calls["extract"] == 1
    assert calls["score"] == 1
    # Критерий 3: все пять оценок и дословный notes.
    assert "relevance=2" in out
    assert "maturity=3" in out
    assert "accessibility=4" in out
    assert "freshness=5" in out
    assert "completeness=6" in out
    assert "нет методики и данных испытаний, тема по адресу" in out
    # Критерий 4: сводка присутствует и несёт числа, а не заглушки.
    assert "сводка:" in out
    assert "кандидатов: 1" in out
    assert "relevance min/медиана/max: 2/2/2" in out
    # Критерий 5: фиктивный ключ не просочился в вывод ни в одном режиме.
    assert "unit-test-key" not in out


async def test_режим_контеста_сводка_всегда_печатается_для_пустой_выдачи(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Критерий 4: источник ответил честно и пусто — сводка обязана
    появиться, а числа обязаны нести явную пометку отсутствия оценок, а не
    молчаливый ноль."""
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "unit-test-key")
    get_settings.cache_clear()
    with respx.mock:
        _mock_search_empty()

        module = _load_probe()
        code = await module.probe_contest("формулировка без результатов")

    out = capsys.readouterr().out
    get_settings.cache_clear()

    assert code == 0
    assert "кандидатов после нормализации: 0" in out
    assert "сводка:" in out
    assert "кандидатов: 0" in out
    assert "нет данных (кандидатов нет)" in out
    assert "unit-test-key" not in out


async def test_режим_контеста_сводка_при_отказавшем_скоринге(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Критерий 4: extractor нашёл кандидата, а скоринг отказал целиком
    (стоп-кран по расходу) — сводка обязана появиться с явной пометкой
    отсутствия оценок, а не с нулями, которые выглядели бы как настоящий
    результат."""
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "unit-test-key")
    get_settings.cache_clear()

    async def _extract_one(*args: object, **kwargs: object):
        from app.schemas import Candidate, SourceType

        return [
            Candidate(
                source_type=SourceType.CONTEST,
                title="Кандидат",
                url="https://university.example.org/news/winners",
                provider="yandex_search",
            )
        ]

    async def _score_fails(*args: object, **kwargs: object):
        from app.llm import CostLimitExceededError

        raise CostLimitExceededError("запрос израсходовал больше лимита")

    monkeypatch.setattr("app.pipeline.stages.extract_candidates", _extract_one)
    monkeypatch.setattr("app.pipeline.stages.score_candidates", _score_fails)

    with respx.mock:
        _mock_search_ok(url="https://university.example.org/news/winners")

        module = _load_probe()
        code = await module.probe_contest("формулировка")

    out = capsys.readouterr().out
    get_settings.cache_clear()

    assert code == 0
    assert "кандидатов после нормализации: 1" in out
    assert "сводка:" in out
    assert "нет данных (скоринг отказал)" in out
    assert "unit-test-key" not in out


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
