"""КиберЛенинка ищет через свой поисковый API, а не через OAI-PMH (Т-16, РЕШ-68).

Прежний адаптер ходил в OAI-PMH и на живых запросах давал ноль. Разведка
(.planning/evidence/E2A-ru-science.md, часть 1) показала пять причин, из
которых четыре со стороны клиента неустранимы: OAI-PMH КиберЛенинки — не
поисковый интерфейс. На том же запросе её собственный `POST /api/search`
отдаёт 719 попаданий с рефератом, годом, журналом и признаком ВАК.

Здесь проверяется всё, что делает переход честным: ответ API разбирается в
кандидатов с непустым рефератом; подсветка `<b>…</b>` снята; относительная
ссылка достроена до полной; отказ и 429 поднимают `SearchError` (инвариант №5,
РЕШ-43), а пустая выдача — нет; в запросе НЕТ фильтра по дате (та самая строка
`from = год − 5`, которая пришпиливала курсор OAI к самой старой партии);
`User-Agent` называет нас и содержит контакт владельца; пауза между запросами
не меньше секунды — обязательное условие РЕШ-70, потому что `robots.txt`
содержит `Disallow: /api/`. Сети нет — respx (инвариант №7).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx
import pytest
import respx

from app.schemas import SearchQuery, SourceType
from app.search import build_adapters
from app.search import cyberleninka as cyberleninka_module
from app.search.base import SearchError
from app.search.cyberleninka import API_URL, USER_AGENT, CyberLeninkaAdapter, _shorten_query

#: Ответ API в том виде, в каком его вернул живой запрос (§1.6 доказательства):
#: подсветка в названии и фрагментах, относительная ссылка, признак ВАК.
_ARTICLE: dict[str, Any] = {
    "name": "<b>Гидроочистка</b> дизельного топлива",
    "annotation": "Рассмотрены катализаторы <b>гидроочистки</b> дизельного топлива и их дезактивация.",
    "link": "/article/n/gidroochistka-dizelnogo-topliva",
    "authors": ["Иванов И.И.", "Петров П.П."],
    "year": "2015",
    "journal": "Вестник химии",
    "journal_link": "/journal/n/vestnik-himii",
    "ocr": ["… установка <b>гидроочистки</b> работает …"],
    "catalogs": [{"id": 8, "alias_name": "vak", "acronym": "ВАК"}],
}


def _response(*articles: dict[str, Any], found: int | None = None) -> httpx.Response:
    payload: dict[str, Any] = {"found": len(articles) if found is None else found}
    if articles:
        payload["articles"] = list(articles)
    return httpx.Response(200, json=payload)


def _query(text: str = "гидроочистка дизельного топлива") -> SearchQuery:
    return SearchQuery(text=text, source_type=SourceType.PUBLICATION, lang="ru", rationale="тест")


def _body(request: httpx.Request) -> dict[str, Any]:
    """Тело запроса как словарь. Читается из байтов: кириллица обязана доехать
    в UTF-8, иначе API отвечает честным 200 с `found: 0` (грабля §1.6)."""
    return json.loads(request.content.decode("utf-8"))


# ── Разбор ответа ───────────────────────────────────────────────────────────


@respx.mock
async def test_ответ_api_разбирается_в_кандидата_с_рефератом(tmp_path):
    """Главное, ради чего делался переход: у кандидата есть реферат, год и
    журнал — ровно то, чего в записях OAI не было вовсе."""
    respx.post(API_URL).mock(return_value=_response(_ARTICLE, found=719))
    async with httpx.AsyncClient() as client:
        found = await CyberLeninkaAdapter(client, cache_dir=tmp_path).search(_query(), limit=5)

    assert len(found) == 1
    candidate = found[0]
    assert candidate.title == "Гидроочистка дизельного топлива"
    assert str(candidate.url) == "https://cyberleninka.ru/article/n/gidroochistka-dizelnogo-topliva"
    assert candidate.year == 2015
    assert candidate.org == "Вестник химии"
    assert candidate.authors == ["Иванов И.И.", "Петров П.П."]
    assert candidate.provider == "cyberleninka"
    assert "дезактивация" in candidate.snippet
    assert "ВАК" in candidate.snippet


@respx.mock
async def test_подсветка_снята_из_названия_и_реферата(tmp_path):
    """`<b>` вокруг совпадений — разметка выдачи, а не текст статьи. В отчёт
    клиента она попасть не должна ни в названии, ни в реферате."""
    respx.post(API_URL).mock(
        return_value=_response(
            {
                **_ARTICLE,
                "name": "Гидро<b>очистк</b>а топлива",
                "annotation": "Про <b>катализаторы</b> &laquo;НК&raquo;",
            }
        )
    )
    async with httpx.AsyncClient() as client:
        found = await CyberLeninkaAdapter(client, cache_dir=tmp_path).search(_query(), limit=5)

    assert found[0].title == "Гидроочистка топлива"
    assert "<b>" not in found[0].snippet and "</b>" not in found[0].snippet
    assert "катализаторы «НК»" in found[0].snippet


@respx.mock
async def test_реферат_берётся_из_фрагментов_когда_annotation_пуст(tmp_path):
    respx.post(API_URL).mock(return_value=_response({**_ARTICLE, "annotation": ""}))
    async with httpx.AsyncClient() as client:
        found = await CyberLeninkaAdapter(client, cache_dir=tmp_path).search(_query(), limit=5)

    assert "установка гидроочистки работает" in found[0].snippet


@respx.mock
async def test_запись_без_ссылки_или_названия_отбрасывается(tmp_path):
    """Без ссылки карточку нечем подтвердить, без названия она пуста — такие
    записи не кандидаты, но и не повод уронить остальную выдачу."""
    respx.post(API_URL).mock(
        return_value=_response({**_ARTICLE, "link": ""}, {**_ARTICLE, "name": ""}, _ARTICLE, found=3)
    )
    async with httpx.AsyncClient() as client:
        found = await CyberLeninkaAdapter(client, cache_dir=tmp_path).search(_query(), limit=5)

    assert len(found) == 1


# ── Запрос ──────────────────────────────────────────────────────────────────


@respx.mock
async def test_запрос_уходит_в_поисковый_api_без_фильтра_по_дате(tmp_path):
    """Мутационный замок Т-16. Вернуть в запрос строку `from = год − 5` —
    ровно та правка, из-за которой источник давал ноль: в OAI-PMH `from` —
    нижняя граница, и курсор вставал на самую старую партию записей. У
    поискового API `from` — это смещение выдачи, целое число, и никакой даты в
    запросе быть не может.
    """
    route = respx.post(API_URL).mock(return_value=_response(_ARTICLE))
    async with httpx.AsyncClient() as client:
        await CyberLeninkaAdapter(client, cache_dir=tmp_path).search(_query(), limit=7)

    request = route.calls[0].request
    assert request.method == "POST"
    body = _body(request)
    assert body["mode"] == "articles"
    assert body["q"] == "гидроочистка дизельного топлива"
    assert body["size"] == 7
    assert body["from"] == 0 and isinstance(body["from"], int)
    assert set(body) == {"mode", "q", "size", "from"}
    # Ни в одном значении запроса не должно быть даты — ни в `from`, ни под
    # другим именем; и в адаптере не должно остаться константы, из которой
    # такую дату можно собрать.
    assert not re.search(r"\d{4}-\d{2}-\d{2}", request.content.decode("utf-8"))
    assert not hasattr(cyberleninka_module, "FRESHNESS_YEARS"), (
        "фильтр свежести в запросе — причина нулевой выдачи (Т-16)"
    )


@respx.mock
async def test_user_agent_называет_нас_и_содержит_контакт(tmp_path):
    """РЕШ-70: мы ходим в путь, закрытый robots.txt, поэтому обязаны называть
    себя честно и оставлять контакт владельца. Маскировка под браузер
    запрещена."""
    route = respx.post(API_URL).mock(return_value=_response(_ARTICLE))
    async with httpx.AsyncClient() as client:
        await CyberLeninkaAdapter(client, cache_dir=tmp_path).search(_query(), limit=5)

    agent = route.calls[0].request.headers["User-Agent"]
    assert agent == USER_AGENT
    assert "TechMostScout" in agent
    assert "@" in agent and "mailto:" in agent
    assert "Mozilla" not in agent and "Chrome" not in agent


@respx.mock
async def test_пауза_между_запросами_соблюдается(tmp_path):
    """Не чаще одного запроса в секунду — обязательное условие РЕШ-70.
    Проверяется фактом времени: два поиска подряд обязаны занять не меньше
    интервала."""
    respx.post(API_URL).mock(return_value=_response(_ARTICLE))
    async with httpx.AsyncClient() as client:
        adapter = CyberLeninkaAdapter(client, cache_dir=tmp_path)
        assert adapter.min_interval_seconds >= 1.0, "троттлинг адаптера — пустая операция"
        started = time.perf_counter()
        await adapter.search(_query("первый запрос"), limit=1)
        await adapter.search(_query("второй запрос"), limit=1)
        elapsed = time.perf_counter() - started

    # Первый запрос не ждёт (ждать не от чего), поэтому интервал ровно один.
    # Допуск на разрешение системного таймера — отсюда 0,9.
    assert elapsed >= adapter.min_interval_seconds * 0.9, f"запросы ушли подряд, за {elapsed:.3f} с"


@respx.mock
async def test_повторный_запрос_берётся_из_кэша(tmp_path):
    """Кэш — тоже форма вежливости к источнику: повторный прогон по той же
    формулировке вообще не трогает КиберЛенинку."""
    route = respx.post(API_URL).mock(return_value=_response(_ARTICLE))
    async with httpx.AsyncClient() as client:
        adapter = CyberLeninkaAdapter(client, cache_dir=tmp_path)
        first = await adapter.search(_query(), limit=5)
        second = await adapter.search(_query(), limit=5)

    assert route.call_count == 1
    assert [c.url for c in first] == [c.url for c in second]


# ── Укорачивание при found=0 (диагноз Э2-Ж, docs/sources.md) ───────────────

#: 8-словная формулировка модели из диагноза — на живом вызове дала found=0.
_LONG_QUERY = "ультразвуковая диагностика супоросности свиноматок с помощью нейросети"
#: Первые 4 содержательных слова той же формулировки (без стоп-слова «с»).
_SHORT_QUERY = "ультразвуковая диагностика супоросности свиноматок"


def _by_query(long_response: httpx.Response, short_response: httpx.Response):
    """Разные ответы на длинную и укороченную форму — по телу запроса."""

    def side_effect(request: httpx.Request) -> httpx.Response:
        return long_response if _body(request)["q"] == _LONG_QUERY else short_response

    return side_effect


@respx.mock
async def test_укорачивание_повторяет_запрос_при_нулевой_выдаче(tmp_path, caplog):
    """Критерий 1: длинная форма → 0, укороченная → N>0 — адаптер возвращает
    кандидатов укороченной формы, в логе прогона видны обе формы и оба found —
    диагноз следующего нуля читается из журнала без контрольных вызовов."""
    route = respx.post(API_URL).mock(side_effect=_by_query(_response(found=0), _response(_ARTICLE, found=21)))
    caplog.set_level("INFO", logger=cyberleninka_module.__name__)
    async with httpx.AsyncClient() as client:
        adapter = CyberLeninkaAdapter(client, cache_dir=tmp_path)
        found = await adapter.search(_query(_LONG_QUERY), limit=5)

        # Повторный вызов той же формулировки: обе формы уже в кэше — источник
        # больше не тревожим (кэш ключуется финальной формой запроса).
        found_again = await adapter.search(_query(_LONG_QUERY), limit=5)

    assert len(found) == 1
    assert route.call_count == 2, "должно быть ровно два обращения: исходная форма + один повтор"
    bodies = [_body(call.request)["q"] for call in route.calls]
    assert bodies == [_LONG_QUERY, _SHORT_QUERY]
    assert _LONG_QUERY in caplog.text
    assert _SHORT_QUERY in caplog.text
    assert "found=0" in caplog.text and "found=21" in caplog.text
    assert [c.url for c in found_again] == [c.url for c in found]


@respx.mock
async def test_found_ноль_на_обеих_формах_честный_пустой_список(tmp_path):
    """Критерий 2: если и укороченная форма ничего не нашла — это результат
    поиска, а не отказ источника."""
    respx.post(API_URL).mock(return_value=_response(found=0))
    async with httpx.AsyncClient() as client:
        found = await CyberLeninkaAdapter(client, cache_dir=tmp_path).search(_query(_LONG_QUERY), limit=5)

    assert found == []


@respx.mock
async def test_отказ_на_повторе_поднимает_searcherror(tmp_path):
    """Критерий 3: транспортный отказ на повторе — тот же контракт, что и на
    первой попытке; существующая машинерия `_fetch` не обходится стороной."""
    route = respx.post(API_URL).mock(
        side_effect=_by_query(_response(found=0), httpx.Response(500, text="internal error"))
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(SearchError):
            await CyberLeninkaAdapter(client, cache_dir=tmp_path).search(_query(_LONG_QUERY), limit=5)

    assert route.call_count == 2


@respx.mock
async def test_повтор_тоже_соблюдает_паузу(tmp_path):
    """Критерий 4: повтор — такой же сетевой вызов и подчиняется тому же
    `min_interval_seconds`, что и исходный запрос (РЕШ-70)."""
    respx.post(API_URL).mock(side_effect=_by_query(_response(found=0), _response(_ARTICLE, found=21)))
    async with httpx.AsyncClient() as client:
        adapter = CyberLeninkaAdapter(client, cache_dir=tmp_path)
        started = time.perf_counter()
        await adapter.search(_query(_LONG_QUERY), limit=5)
        elapsed = time.perf_counter() - started

    assert elapsed >= adapter.min_interval_seconds * 0.9, f"повтор ушёл без паузы, за {elapsed:.3f} с"


def test_укорачивание_детерминировано_и_сохраняет_порядок():
    """Критерий 5: одна и та же формулировка всегда даёт одну и ту же короткую
    форму; стоп-слова снимаются, порядок оставшихся слов не меняется."""
    text = "ультразвуковая диагностика для супоросности у свиноматок с помощью нейросети"

    short = _shorten_query(text)

    assert short == _shorten_query(text)
    assert short == "ультразвуковая диагностика супоросности свиноматок"


def test_укорачивание_короткого_запроса_не_меняет_его():
    """Формулировка из живого диагноза, давшая found=21: три содержательных
    слова, короче делать уже нечего — короткая форма совпадает с исходной."""
    assert _shorten_query("супоросность свиноматок узи") == "супоросность свиноматок узи"


# ── Отказы (инвариант №5, РЕШ-43) ───────────────────────────────────────────


@respx.mock
async def test_отказ_источника_поднимает_searcherror(tmp_path):
    respx.post(API_URL).mock(return_value=httpx.Response(500, text="internal error"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(SearchError):
            await CyberLeninkaAdapter(client, cache_dir=tmp_path).search(_query(), limit=5)


@respx.mock
async def test_429_поднимает_searcherror(tmp_path):
    """Лимиты API не документированы. 429 — отказ источника, а не «ничего не
    нашлось»: иначе в отчёте пропажа источника выглядела бы как пустая выдача."""
    respx.post(API_URL).mock(return_value=httpx.Response(429, text="too many requests"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(SearchError):
            await CyberLeninkaAdapter(client, cache_dir=tmp_path).search(_query(), limit=5)


@respx.mock
async def test_нечитаемое_тело_поднимает_searcherror(tmp_path):
    """200, но тело не JSON (например, страница-заглушка) — тот же транспортный
    отказ, что и HTTP-ошибка."""
    respx.post(API_URL).mock(return_value=httpx.Response(200, text="<html>не JSON</html>"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(SearchError):
            await CyberLeninkaAdapter(client, cache_dir=tmp_path).search(_query(), limit=5)


@respx.mock
async def test_пустая_выдача_не_поднимает_отказ(tmp_path):
    """`found: 0` приходит вообще без `articles` — это результат, а не отказ."""
    respx.post(API_URL).mock(return_value=_response(found=0))
    async with httpx.AsyncClient() as client:
        found = await CyberLeninkaAdapter(client, cache_dir=tmp_path).search(_query(), limit=5)

    assert found == []


# ── Реестр ──────────────────────────────────────────────────────────────────


async def test_реестр_включает_киберленинку_без_ключа(settings):
    """Источник бесплатный и ключа не требует — он обязан быть в сборке."""
    async with httpx.AsyncClient() as client:
        adapters = build_adapters(client, settings)

    adapter = next(a for a in adapters if a.name == "cyberleninka")
    assert isinstance(adapter, CyberLeninkaAdapter)
    assert adapter.requires_key is False
