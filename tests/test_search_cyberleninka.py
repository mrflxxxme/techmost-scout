"""Набор OAI КиберЛенинки выбирается по отрасли брифа (Т-4, РЕШ-51).

До этой задачи набор брался из единственной настройки `CYBERLENINKA_OAI_SET`
(по умолчанию `rsci`) и был одинаков для любого запроса. По докстрингу самого
адаптера это и есть причина нулевой выдачи: без предметного набора за прогон
просматриваются 30 случайных записей из ~2 млн.

Здесь проверяется вся цепочка: справочник `data/cyberleninka_sets.yaml`
читается `yaml.safe_load` и хранит значения в кавычках (инвариант №10);
отрасль сопоставляется нормализованно; неизвестная отрасль оставляет запасной
набор из настройки; выбранный набор действительно уходит в параметр `set`
запроса OAI (проверяется по перехваченному respx запросу); пустая выдача при
успешном ответе отказом не считается (инвариант №5); отрасль брифа доезжает от
`ScoutPipeline.run` до конструктора адаптера. Сети нет — respx (инвариант №7).
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
import respx
import yaml

from app.config import get_settings
from app.llm import LLMRouter
from app.pipeline import orchestrator as orchestrator_module
from app.pipeline import stages
from app.pipeline.orchestrator import ScoutPipeline
from app.schemas import SearchPlan, SearchQuery, SourceType
from app.search import build_adapters
from app.search.cyberleninka import BASE_URL, SETS_FILE_NAME, CyberLeninkaAdapter, load_industry_sets, resolve_oai_set

SETS_FILE = get_settings().data_dir / SETS_FILE_NAME

#: Наборы медицины: справочник вправе указывать любой из двух
MEDICINE_SETS = {"medline", "pubmed"}


def _query(text: str = "катализатор гидроочистки") -> SearchQuery:
    return SearchQuery(text=text, source_type=SourceType.PUBLICATION, lang="ru", rationale="тест")


def _oai_list_records(*, title: str = "Катализатор гидроочистки: обзор") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
        "<responseDate>2026-08-14T00:00:00Z</responseDate>"
        "<ListRecords><record>"
        "<header><identifier>oai:cyberleninka.ru:article/n/example</identifier></header>"
        "<metadata><oai_dc:dc "
        'xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{title}</dc:title>"
        "<dc:publisher>Журнал химии</dc:publisher>"
        "<dc:identifier>https://cyberleninka.ru/article/n/example</dc:identifier>"
        "</oai_dc:dc></metadata>"
        "</record></ListRecords>"
        "</OAI-PMH>"
    )


_OAI_NO_RECORDS_MATCH = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
    "<responseDate>2026-08-14T00:00:00Z</responseDate>"
    '<error code="noRecordsMatch">Записей не найдено</error>'
    "</OAI-PMH>"
)


# ── Справочник как файл ─────────────────────────────────────────────────────


def test_справочник_читается_yaml_safe_load():
    """Критерий 1: файл — корректный YAML со внятной структурой, а не текст,
    который «выглядит правильно»."""
    raw = yaml.safe_load(SETS_FILE.read_text(encoding="utf-8"))

    assert isinstance(raw, dict)
    assert raw["fallback"] == "rsci"
    assert isinstance(raw["sets"], list) and raw["sets"]
    for item in raw["sets"]:
        assert isinstance(item["set"], str) and item["set"].strip()
        assert isinstance(item["keywords"], list) and item["keywords"]
        assert all(isinstance(word, str) and word.strip() for word in item["keywords"])


def test_все_значения_справочника_в_кавычках():
    """Инвариант №10 и грабля §0.1 п. 5: незакавыченное значение с двоеточием
    ломает разбор, а глазами это не видно. Здесь ловится сам стиль файла, а не
    только текущее его содержимое: значения `name` с двоеточиями внутри
    существуют, и раскавычить их — рабочий способ сломать справочник."""
    key_value = re.compile(r"^(?P<key>[\w]+):\s*(?P<value>.*)$")

    for number, line in enumerate(SETS_FILE.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stripped = stripped.removeprefix("- ").strip()
        if (match := key_value.match(stripped)) is not None:
            value = match.group("value").strip()
            if not value:  # ключ без значения: `sets:`
                continue
        else:
            value = stripped  # элемент списка примет
        assert value.startswith('"') and value.endswith('"'), f"строка {number}: значение {value!r} без кавычек"


def test_справочник_разбирается_в_записи():
    catalogue = load_industry_sets(SETS_FILE)

    assert catalogue.fallback == "rsci"
    assert {entry.oai_set for entry in catalogue.entries} >= {"agris", "cas", "georef"}
    assert MEDICINE_SETS & {entry.oai_set for entry in catalogue.entries}


# ── Сопоставление отрасли ───────────────────────────────────────────────────


def test_агропромышленный_комплекс_даёт_agris():
    assert resolve_oai_set("агропромышленный комплекс", fallback="rsci") == "agris"


def test_фармацевтика_даёт_набор_медицины():
    assert resolve_oai_set("фармацевтика", fallback="rsci") in MEDICINE_SETS


def test_нефтепереработка_и_химия_дают_cas():
    """Отрасль трёх проверочных прогонов: нефтегаз уходит в химический набор,
    а не в геологический, — приметы georef намеренно узкие (про недра)."""
    assert resolve_oai_set("нефтепереработка", fallback="rsci") == "cas"
    assert resolve_oai_set("химическая промышленность", fallback="rsci") == "cas"
    assert resolve_oai_set("нефтегазодобыча", fallback="rsci") == "cas"


def test_геология_и_недропользование_дают_georef():
    assert resolve_oai_set("геологоразведка", fallback="rsci") == "georef"
    assert resolve_oai_set("горное дело", fallback="rsci") == "georef"


def test_склоняемые_формы_горного_дела_и_ископаемых_дают_georef():
    """П-1 аудита: приметы справочника обязаны быть основами, а не
    словосочетаниями в именительном падеже. Сравнение идёт вхождением, поэтому
    «горное дело» ловило только само себя, а «развитие горного дела» уходило в
    `rsci` — ровно тот набор, ради ухода от которого задача и делалась, — и в
    логе это выглядело как «отрасль в справочнике не нашлась». Проверяются
    именно склоняемые формы: на именительном падеже дефект не виден."""
    assert resolve_oai_set("развитие горного дела", fallback="rsci") == "georef"
    assert resolve_oai_set("предприятия горного дела", fallback="rsci") == "georef"
    assert resolve_oai_set("горная промышленность", fallback="rsci") == "georef"
    assert resolve_oai_set("добыча полезного ископаемого", fallback="rsci") == "georef"
    assert resolve_oai_set("добыча полезных ископаемых", fallback="rsci") == "georef"
    assert resolve_oai_set("переработка твёрдых ископаемых", fallback="rsci") == "georef"


def test_неизвестная_отрасль_даёт_запасной_набор_из_настройки():
    """Запасной — именно из настройки, а не зашитый `rsci`: иначе правка
    CYBERLENINKA_OAI_SET перестала бы что-либо значить."""
    assert resolve_oai_set("производство мебели", fallback="rsci") == "rsci"
    assert resolve_oai_set("производство мебели", fallback="journal_1426") == "journal_1426"


def test_запасной_набор_справочника_работает_когда_настройка_пуста():
    assert resolve_oai_set("производство мебели", fallback=None) == "rsci"


def test_регистр_ё_и_пунктуация_не_мешают(tmp_path: Path):
    """Нормализация отрасли: свой справочник, чтобы проверялось правило, а не
    удачное содержимое рабочего файла."""
    path = tmp_path / "sets.yaml"
    path.write_text(
        'fallback: "rsci"\nsets:\n  - set: "test_set"\n    keywords:\n      - "зеленая энергетика"\n',
        encoding="utf-8",
    )

    assert resolve_oai_set("Зелёная  энергетика!", fallback="rsci", sets_path=path) == "test_set"
    assert resolve_oai_set("ЗЕЛЁНАЯ ЭНЕРГЕТИКА", fallback="rsci", sets_path=path) == "test_set"
    assert resolve_oai_set("угольная энергетика", fallback="rsci", sets_path=path) == "rsci"


def test_без_отрасли_справочник_не_читается_и_набор_прежний(tmp_path: Path, caplog):
    """Прежнее поведение сохраняется целиком: нет отрасли — нет и обращения к
    справочнику (путь заведомо несуществующий, а предупреждения нет)."""
    missing = tmp_path / "нет-такого-файла.yaml"

    with caplog.at_level("WARNING", logger="app.search.cyberleninka"):
        assert resolve_oai_set(None, fallback="rsci", sets_path=missing) == "rsci"
        assert resolve_oai_set("   ", fallback="rsci", sets_path=missing) == "rsci"
        assert resolve_oai_set(None, fallback=None, sets_path=missing) is None

    assert caplog.records == []


def test_битый_справочник_не_роняет_поиск(tmp_path: Path, caplog):
    """Локальный файл не повод уронить прогон: набор берётся из настройки, но
    в логе остаётся предупреждение — иначе поломка справочника незаметна."""
    path = tmp_path / "sets.yaml"
    path.write_text('sets: [ "не закрытая скобка\n', encoding="utf-8")

    with caplog.at_level("WARNING", logger="app.search.cyberleninka"):
        assert resolve_oai_set("агропромышленный комплекс", fallback="rsci", sets_path=path) == "rsci"

    assert any("справочник наборов" in record.message for record in caplog.records)


async def test_справочник_в_чужой_кодировке_не_уносит_источник(tmp_path: Path, caplog):
    """П-2 аудита: справочник целиком кириллический, и пересохранение
    Windows-редактором в cp1251 — штатный операционный исход. UnicodeDecodeError
    — подкласс ValueError, в прежний перехват `(OSError, yaml.YAMLError)` он не
    попадал: исключение поднималось из конструктора адаптера и гасилось общим
    `except Exception` в `build_adapters`, после чего источник не попадал ни в
    сборку, ни в `sources_unavailable` — в отчёте он исчезал бесследно. Любая
    поломка локального файла обязана давать запасной набор и видимую строку в
    логе, а не уносить источник."""
    path = tmp_path / "sets.yaml"
    path.write_bytes(SETS_FILE.read_text(encoding="utf-8").encode("cp1251", errors="replace"))

    with caplog.at_level("WARNING", logger="app.search.cyberleninka"):
        assert resolve_oai_set("агропромышленный комплекс", fallback="rsci", sets_path=path) == "rsci"
        async with httpx.AsyncClient() as client:
            adapter = CyberLeninkaAdapter(client, oai_set="rsci", industry="агропромышленный комплекс", sets_path=path)

    assert adapter.oai_set == "rsci"
    assert any("справочник наборов" in record.message for record in caplog.records)


def test_строка_лога_показывает_отрасль_и_набор(caplog):
    """Критерий 3 ворот проверяется по логу прогона — значит строка обязана
    быть, и в ней обязаны быть оба значения."""
    with caplog.at_level("INFO", logger="app.search.cyberleninka"):
        resolve_oai_set("агропромышленный комплекс", fallback="rsci")

    line = next(record.getMessage() for record in caplog.records if "отрасль" in record.getMessage())
    assert "агропромышленный комплекс" in line
    assert "agris" in line


# ── Набор доезжает до запроса OAI ───────────────────────────────────────────


@respx.mock
async def test_выбранный_набор_уходит_в_параметр_set(tmp_path: Path):
    """Главная проверка задачи: в запрос уходит набор отрасли, а не настройка.
    Запасной набор здесь намеренно другой (`rsci`) — иначе тест не отличил бы
    сработавший справочник от нетронутой настройки."""
    route = respx.get(BASE_URL).mock(return_value=httpx.Response(200, text=_oai_list_records()))
    async with httpx.AsyncClient() as client:
        adapter = CyberLeninkaAdapter(
            client,
            oai_set="rsci",
            industry="агропромышленный комплекс",
            cache_dir=tmp_path,
        )
        found = await adapter.search(_query(), limit=5)

    assert adapter.oai_set == "agris"
    assert route.calls[0].request.url.params["set"] == "agris"
    assert len(found) == 1


@respx.mock
async def test_запасной_набор_уходит_в_параметр_set_при_неизвестной_отрасли(tmp_path: Path):
    route = respx.get(BASE_URL).mock(return_value=httpx.Response(200, text=_oai_list_records()))
    async with httpx.AsyncClient() as client:
        adapter = CyberLeninkaAdapter(client, oai_set="rsci", industry="производство мебели", cache_dir=tmp_path)
        await adapter.search(_query(), limit=5)

    assert route.calls[0].request.url.params["set"] == "rsci"


@respx.mock
async def test_пустая_выдача_не_поднимает_отказ(tmp_path: Path):
    """Инвариант №5 в прежней редакции: ноль записей при успешном ответе —
    результат, а не отказ источника. Выбор набора этого не меняет."""
    respx.get(BASE_URL).mock(return_value=httpx.Response(200, text=_OAI_NO_RECORDS_MATCH))
    async with httpx.AsyncClient() as client:
        adapter = CyberLeninkaAdapter(
            client,
            oai_set="rsci",
            industry="агропромышленный комплекс",
            cache_dir=tmp_path,
        )
        found = await adapter.search(_query(), limit=5)

    assert found == []


# ── Реестр и оркестратор ────────────────────────────────────────────────────


async def test_реестр_передаёт_отрасль_в_киберленинку(settings):
    async with httpx.AsyncClient() as client:
        with_industry = build_adapters(client, settings, industry="агропромышленный комплекс")
        without = build_adapters(client, settings)

    assert _cyberleninka(with_industry).oai_set == "agris"
    # Без отрасли — прежнее поведение: набор из настройки
    assert _cyberleninka(without).oai_set == settings.cyberleninka_oai_set


def _cyberleninka(adapters) -> CyberLeninkaAdapter:
    adapter = next(a for a in adapters if a.name == "cyberleninka")
    assert isinstance(adapter, CyberLeninkaAdapter)
    return adapter


class _StopRunError(Exception):
    """Прогон дальше сборки адаптеров в этом тесте не нужен."""


async def test_оркестратор_передаёт_отрасль_брифа_в_сборку_адаптеров(
    settings, fake_provider_factory, brief, monkeypatch
):
    """Отрасль брифа доезжает до `build_adapters` через `run` — то есть по
    тому же пути, что и в настоящем прогоне, а не только при прямом вызове
    `gather_candidates`."""
    captured: list[str | None] = []

    async def fake_plan_search(router, brief_):
        return SearchPlan(queries=[_query()])

    def fake_build_adapters(client, settings_=None, *, include_stubs=False, industry=None):
        captured.append(industry)
        raise _StopRunError

    monkeypatch.setattr(stages, "plan_search", fake_plan_search)
    monkeypatch.setattr(orchestrator_module, "build_adapters", fake_build_adapters)

    router = LLMRouter(settings, providers={"fake": fake_provider_factory("fake")})
    pipeline = ScoutPipeline(settings=settings, router=router)
    with pytest.raises(_StopRunError):
        await pipeline.run(brief)
    await pipeline.aclose()

    assert captured == [brief.industry]
