"""Критерии приёмки задачи З-4 волны Э2-Ж (РЕШ-173): тип `grant` доезжает
сквозь план → поиск → экстрактор до кандидата, не схлопываясь в `web`.

До этой задачи грантовая находка была неотличима от прочего веба, и метрика
покрытия SPEC («≥ 4 из: публикации, патенты, гранты, веб») была недостижима
по построению (аудит Т-2 волны Э2-Ж) — ни один кандидат не мог получить тип
`grant`. Здесь — сквозная проверка на подменах: план с грантовой
формулировкой отдаётся адаптеру Яндекса (respx), сырая выдача уходит в
стадию extractor (заглушка модели), и итоговый кандидат сохраняет тип
`grant` через обе стадии.

Сети нет (инвариант №7): HTTP-вызовы Яндекса подменены respx, LLM-вызов
extractor — `FakeProvider` из `conftest.py`.
"""

from __future__ import annotations

import base64
import json

import httpx
import respx

from app.llm import LLMRouter
from app.pipeline.orchestrator import ScoutPipeline
from app.schemas import SearchPlan, SearchQuery, SourceType
from app.search.yandex_search import OPERATION_URL, SEARCH_ASYNC_URL, YandexSearchAdapter


def _xml(url: str, title: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<yandexsearch version='1.0'><response><results><grouping><group><doc>"
        f"<url>{url}</url><domain>rscf.ru</domain><title>{title}</title>"
        "<modtime>20230115T103000</modtime>"
        "<passages><passage>Грант РНФ поддержал разработку катализатора гидроочистки.</passage></passages>"
        "</doc></group></grouping></results></response></yandexsearch>"
    )


def _operation_done(xml: str) -> dict:
    return {"done": True, "response": {"rawData": base64.b64encode(xml.encode("utf-8")).decode("ascii")}}


@respx.mock
async def test_план_с_грантовой_формулировкой_даёт_кандидата_типа_grant(settings, fake_provider_factory):
    """Критерий 1 задачи З-4: план (с формулировкой `source_type: grant`) →
    поиск (адаптер Яндекса принимает `grant` тем же путём, что `web` и
    `contest`) → extractor (модель) → кандидат с `source_type == SourceType.GRANT`.

    Мутация (а) постановки задачи — тип `grant` схлопывается в `web` на
    экстракторе — обязана ронять именно эту проверку: `extract_candidates`
    навязывает кандидату тип из `query.source_type` (не из ответа модели), и
    здесь `query.source_type` — `grant`.
    """
    route = respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(
        return_value=httpx.Response(
            200,
            json=_operation_done(_xml("https://rscf.ru/project/42", "Грант РНФ на разработку катализатора")),
        )
    )

    extractor_response = json.dumps(
        {
            "candidates": [
                {
                    "source_type": "grant",
                    "title": "Грант РНФ на разработку катализатора",
                    "url": "https://rscf.ru/project/42",
                    "org": "РНФ",
                    "authors": [],
                    "year": 2023,
                    "record_index": 0,
                    "raw_id": None,
                    "provider": "yandex_search",
                }
            ],
            "dropped_no_url": 0,
        },
        ensure_ascii=False,
    )

    async with httpx.AsyncClient() as client:
        adapter = YandexSearchAdapter(
            client,
            api_key="test-key",
            folder_id="b1gtest",
            poll_interval_seconds=0.0,
            max_wait_seconds=5.0,
        )
        provider = fake_provider_factory("deepseek", [extractor_response])
        router = LLMRouter(settings, providers={"deepseek": provider})
        pipeline = ScoutPipeline(settings=settings, router=router, adapters=[adapter])

        # План — как его собрал бы planner.md после РЕШ-173: грантовая
        # формулировка размечена типом `grant`, не `web` (правило 9).
        plan = SearchPlan(
            queries=[
                SearchQuery(
                    text="поддержанные проекты РНФ катализатор гидроочистки",
                    source_type=SourceType.GRANT,
                    lang="ru",
                    rationale="тест: план с грантовой формулировкой",
                ),
            ]
        )
        outcome = await pipeline.gather_candidates(plan)

    # Формулировка физически дошла до источника (сужена до реестров РНФ и ФСИ)
    assert route.call_count == 1
    sent_text = json.loads(route.calls[0].request.content)["query"]["queryText"]
    assert "site:rscf.ru" in sent_text and "site:fasie.ru" in sent_text

    assert outcome.problems == []
    assert len(outcome.candidates) == 1
    candidate = outcome.candidates[0]
    assert candidate.source_type is SourceType.GRANT, (
        f"тип кандидата {candidate.source_type!r} схлопнулся вместо того, чтобы остаться `grant`"
    )
    assert candidate.provider == "yandex_search"
