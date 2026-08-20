"""РЕШ-126: план обоих проходов доезжает до источника без потерь по пределу.

ЗАЧЕМ ОТДЕЛЬНЫЙ ФАЙЛ
    Блокирующая находка аудита волны Э2-В: волна подняла спрос на платные
    запросы Яндекса с 6 до 8 за прогон (`yandex_search.py:189`,
    `max_queries = 6`), и обе конкурсные формулировки второго прохода
    отбрасывались источником ДЕТЕРМИНИРОВАННО — `normalize_contest_queries`
    ставит их в конец списка, а `gather_candidates` создаёт задачи в порядке
    `plan.queries`; предел исчерпывался раньше, на web-формулировках.

    До правки это пережило 630 зелёных тестов, потому что `test_contest_queries.py`
    проверял только сам факт вызова нормализации в `replan_search`, а не то,
    доехал ли нормализованный план до источника при ДЕЙСТВУЮЩЕМ платном
    пределе. Здесь считаются формулировки, ФАКТИЧЕСКИ отправленные источнику
    (`route.call_count` и тела реальных HTTP-запросов через `respx`), а не
    вызов функции нормализации — именно эта подмена одного другим и была
    найдена аудитом.

Сети нет: `respx` подменяет оба вызова Яндекса (запуск асинхронного поиска и
опрос операции), реальный `YandexSearchAdapter` работает с этими подменами.
"""

from __future__ import annotations

import base64
import json

import httpx
import respx

from app.llm import LLMRouter
from app.pipeline import stages
from app.pipeline.orchestrator import ScoutPipeline
from app.schemas import SearchPlan, SearchQuery, SourceType
from app.search.yandex_search import OPERATION_URL, SEARCH_ASYNC_URL, YandexSearchAdapter


def _xml(*docs: str) -> str:
    body = "".join(docs)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f"<yandexsearch version='1.0'><response><results><grouping><group>{body}"
        "</group></grouping></results></response></yandexsearch>"
    )


def _doc(url: str) -> str:
    return (
        "<doc>"
        f"<url>{url}</url><domain>example.org</domain>"
        "<title>Заголовок про катализатор</title><modtime>20240115T103000</modtime>"
        "<passages><passage>Фрагмент страницы.</passage></passages>"
        "</doc>"
    )


def _operation_done(xml: str) -> dict:
    return {"done": True, "response": {"rawData": base64.b64encode(xml.encode("utf-8")).decode("ascii")}}


def _mock_ok() -> respx.Route:
    """Каждый запрос отвечает одним и тем же непустым документом — детали
    выдачи тесту не важны, важно только, сколько раз источника СПРОСИЛИ."""
    route = respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(
        return_value=httpx.Response(200, json=_operation_done(_xml(_doc("https://a.example.org/p"))))
    )
    return route


def _q(text: str, source_type: SourceType = SourceType.WEB) -> SearchQuery:
    return SearchQuery(text=text, source_type=source_type, lang="ru", rationale="тест")


#: Тексты двух конкурсных формулировок ПЕРВОГО прохода — план собран дословно
#: по арифметике аудита (2 publication, 2 web, 1 showcase, 1 patent, 2 contest).
_NARROWED = "хакатон решение команды диагностика супоросности"
_OUTCOME = "итоги хакатона победитель проект команды диагностика супоросности"
#: Формулировка, которую (вопреки промпту replanner.md) вернула бы непослушная
#: модель на ВТОРОМ проходе — маркер узнаваем по тексту, чтобы отличить
#: «не отправлена, потому что срезана кодом» от «не отправлена, потому что
#: не хватило предела»: обе выглядели бы как отсутствие текста в теле запроса,
#: если бы не разное происхождение.
_SECOND_PASS_CONTEST_TEXT = "хакатон второй попытки диагностика супоросности"


@respx.mock
async def test_обе_конкурсные_формулировки_первого_прохода_доезжают_а_спрос_не_превышает_предел(settings):
    """Полная арифметика аудита в одном тесте: план первого прохода — 8
    формулировок (2 publication, 2 web, 1 showcase, 1 patent, 2 contest),
    план второго — 2 web (модель реплана их не просит, но вернула бы контест,
    если бы не послушалась промпта; здесь проверяем оба случая).

    Критерий: `queries_used` не превышает `max_queries = 6`, ни одна
    формулировка не пропущена по пределу (`limit_problems` пуст), и ОБЕ
    конкурсные формулировки первого прохода физически ушли источнику — это
    подтверждают тела реальных HTTP-запросов, а не факт вызова нормализации.
    """
    route = _mock_ok()
    async with httpx.AsyncClient() as client:
        adapter = YandexSearchAdapter(
            client,
            api_key="test-key",
            folder_id="b1gtest",
            max_queries=6,
            poll_interval_seconds=0.0,
            max_wait_seconds=5.0,
        )
        # Стадия extractor (Э2-5) к задаче не относится и требует модели:
        # проверяется доставка формулировок до адаптера, а не разбор его
        # выдачи (тот же приём, что в tests/test_search_yandex.py).
        adapter.needs_extraction = False
        router = LLMRouter(settings, providers={})
        pipeline = ScoutPipeline(settings=settings, router=router, adapters=[adapter])

        # Первый проход — план после normalize_contest_queries, ровно как
        # соберёт его plan_search (порядок ролей значения не имеет: контест
        # уже корректен, нормализация его не тронет).
        plan1 = SearchPlan(
            queries=[
                _q("публикация первая", source_type=SourceType.PUBLICATION),
                _q("публикация вторая", source_type=SourceType.PUBLICATION),
                _q("веб первая формулировка"),
                _q("веб вторая формулировка"),
                _q("витрина стартапов", source_type=SourceType.SHOWCASE),
                _q("патентный поиск", source_type=SourceType.PATENT),
                _q(_NARROWED, source_type=SourceType.CONTEST),
                _q(_OUTCOME, source_type=SourceType.CONTEST),
            ]
        )
        outcome1 = await pipeline.gather_candidates(plan1)

        # Второй проход — модель реплана вопреки промпту вернула контест
        # вперемешку с web; strip_contest_queries обязана срезать его ДО
        # того, как план попадёт в gather_candidates.
        plan2_raw = SearchPlan(
            queries=[
                _q("веб формулировка второго прохода один"),
                _q("веб формулировка второго прохода два"),
                _q(_SECOND_PASS_CONTEST_TEXT, source_type=SourceType.CONTEST),
            ]
        )
        plan2 = stages.strip_contest_queries(plan2_raw)
        outcome2 = await pipeline.gather_candidates(plan2)

    # Спрос вернулся к шести и предел сходится ровно, как до волны.
    assert adapter.queries_used == 6, "спрос на платные запросы обязан остаться в пределах max_queries = 6"
    assert adapter.limit_problems == [], "ни одна формулировка не имеет права быть пропущена по пределу"
    assert outcome1.problems == [] and outcome2.problems == [], "оба прохода обязаны пройти без пропусков по пределу"
    assert route.call_count == 6, "источнику фактически отправлено ровно шесть платных запросов"

    sent_texts = [json.loads(call.request.content)["query"]["queryText"] for call in route.calls]
    # «План доехал», а не «нормализация вызвана»: обе конкурсные формулировки
    # ПЕРВОГО прохода обязаны быть СРЕДИ РЕАЛЬНО ОТПРАВЛЕННЫХ текстов.
    assert any(text.startswith(_NARROWED) for text in sent_texts), "формулировка «площадки» не дошла до источника"
    assert any(text == _OUTCOME for text in sent_texts), "формулировка «итоги» не дошла до источника"
    # Контест второго прохода не должен быть отправлен НИКОГДА — не потому,
    # что кончился предел, а потому что strip_contest_queries его срезала.
    assert not any(_SECOND_PASS_CONTEST_TEXT in text for text in sent_texts), (
        "конкурсная формулировка второго прохода обязана быть срезана кодом до отправки источнику"
    )
    assert outcome2.candidates, "второй проход обязан принести кандидатов — web-формулировки дошли"
