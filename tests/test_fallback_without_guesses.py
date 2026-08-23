"""З-5: откат на разбор адаптера не выдаёт догадки за подтверждённые факты.

Находка В-2 аудита Э2-Г: в воротном отчёте домен страницы («pt.2035.university»)
уехал в поле `org` конкурсной карточки как «Организация…», а время последней
правки страницы (`modtime`) — в поле `year` как «Год публикации». Обе догадки
собрал сам адаптер (`yandex_search.py`, старые `:425` и `:429`), стадия
`extractor` в этом прогоне не отработала, и откат на разбор адаптера пронёс
недосказанное как подтверждённое — нарушение инварианта №1.

Тест сквозной: от XML Яндекса и упавшей стадии `extractor` — через отказ
extractor, отбор кандидатов, сборку промпта карточки — до отрендеренного
markdown-отчёта. Каждый шаг проверяет, что домен и время правки не выдают себя
за факт: кандидат несёт `org=None`/`year=None`, промпт карточки получает
`"org": null`, а не строку домена, и отчёт печатает «не найдено», а не адрес
сайта.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime

import httpx
import respx

from app.export.markdown import render_markdown
from app.llm import LLMRouter
from app.llm.base import LLMResult, Usage
from app.pipeline import stages
from app.pipeline.orchestrator import ScoutPipeline
from app.pipeline.prompting import candidates_to_json
from app.schemas import Report, ReportSummary, ScoredCandidate, SearchPlan, SearchQuery, SourceType
from app.search.yandex_search import OPERATION_URL, SEARCH_ASYNC_URL, YandexSearchAdapter

#: Домен и время правки взяты по мотивам находки В-2: реальный адрес конкурсной
#: страницы и дата, которую воротный отчёт напечатал как «год публикации».
_DOMAIN = "pt.2035.university"
_MODTIME = "20250315T090000"  # → «Год публикации — 2025» в прежнем (дефектном) поведении


def _doc(path: str, title: str) -> str:
    return (
        "<doc>"
        f"<url>https://{_DOMAIN}/{path}</url><domain>{_DOMAIN}</domain>"
        f"<title>{title}</title>"
        f"<modtime>{_MODTIME}</modtime>"
        "<passages><passage>Описание проекта на странице конкурса.</passage></passages>"
        "</doc>"
    )


def _xml(*docs: str) -> str:
    body = "".join(docs)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f"<yandexsearch version='1.0'><response><results><grouping><group>{body}"
        "</group></grouping></results></response></yandexsearch>"
    )


def _operation_done(xml: str) -> dict:
    return {"done": True, "response": {"rawData": base64.b64encode(xml.encode("utf-8")).decode("ascii")}}


def _mock_yandex(xml: str) -> None:
    respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(return_value=httpx.Response(200, json=_operation_done(xml)))


class _FixedResponseProvider:
    """Провайдер-заглушка: один и тот же ответ на каждый вызов.

    Свой минимальный класс вместо `FakeProvider` из `conftest.py` — тесты не
    входят в файлы этой задачи, а поведение нужно ровно одно: что бы ни
    спросили, отвечать одним и тем же текстом (невалидным для стадии
    `extractor`, валидным для стадии `card_author`)."""

    price_in_rub_per_mtok = 10.0
    price_out_rub_per_mtok = 20.0

    def __init__(self, name: str, response: str) -> None:
        self.name = name
        self._response = response
        self.calls: list = []

    async def complete(self, messages, *, model=None, temperature=0.2, max_tokens=4096, json_mode=False):
        self.calls.append(messages)
        return LLMResult(
            text=self._response,
            usage=Usage(tokens_in=10, tokens_out=10, cost_rub=0.001, provider=self.name, model="fake"),
        )

    async def health(self):
        return True, "заглушка"


def _plan(text: str) -> SearchPlan:
    return SearchPlan(queries=[SearchQuery(text=text, source_type=SourceType.WEB, lang="ru", rationale="тест")])


async def _run_gather(settings, xml: str):
    """Общая часть обоих тестов: платный поиск отвечает `xml`, стадия
    `extractor` отказывает на каждой попытке (основной вызов и ремонтный)."""
    _mock_yandex(xml)
    extractor_provider = _FixedResponseProvider("gigachat", "точно не JSON, а разговор ни о чём")
    router = LLMRouter(settings, providers={"gigachat": extractor_provider})

    async with httpx.AsyncClient() as client:
        adapter = YandexSearchAdapter(
            client,
            api_key="test-key",
            folder_id="b1gtest",
            poll_interval_seconds=0.0,
            max_wait_seconds=5.0,
        )
        outcome = await ScoutPipeline(settings=settings, router=router, adapters=[adapter]).gather_candidates(
            _plan("итоги хакатона победитель диагностика")
        )
    return outcome


@respx.mock
async def test_откат_не_выдаёт_домен_и_modtime_за_org_и_year(settings, caplog):
    """Критерии 1 и 2: кандидаты доезжают до отбора разбором адаптера, но
    `org`/`year` пусты, а число кандидатов не теряется."""
    xml = _xml(_doc("project/1", "Команда-победитель"), _doc("project/2", "Второй проект команды"))
    with caplog.at_level(logging.WARNING, logger="app.pipeline.orchestrator"):
        outcome = await _run_gather(settings, xml)

    # Критерий 2: раздача от источника не потеряна — оба документа выдачи стали
    # кандидатами разбора адаптера.
    assert len(outcome.candidates) == 2
    for candidate in outcome.candidates:
        assert candidate.org is None, "домен не имеет права выдавать себя за организацию"
        assert candidate.year is None, "время правки страницы не имеет права выдавать себя за год публикации"
        assert _DOMAIN in str(candidate.url), "адрес всё ещё доступен — потери данных нет, есть честный пропуск поля"

    # Отказ стадии остаётся видимым: лог и problems прогона, а не тихая замена.
    assert any("беру разбор адаптера" in record.getMessage() for record in caplog.records)
    assert any("взят разбор адаптера" in problem for problem in outcome.problems)


@respx.mock
async def test_откат_не_доносит_домен_до_промпта_карточки_и_до_отчёта(settings, brief):
    """Продолжение сквозного теста: кандидат с пустыми `org`/`year` не даёт
    домену просочиться ни в промпт стадии card_author (`candidates_to_json`
    отдаёт `"org": null`), ни в отрендеренный отчёт («не найдено» вместо
    адреса площадки)."""
    outcome = await _run_gather(settings, _xml(_doc("project/1", "Команда-победитель")))

    assert len(outcome.candidates) == 1
    candidate = outcome.candidates[0]
    assert candidate.org is None and candidate.year is None

    group = [
        ScoredCandidate(candidate=candidate, relevance=8, maturity=5, accessibility=6, freshness=6, completeness=6)
    ]
    # Проверка того, что фактически уходит в системный промпт карточки, до
    # обращения к модели — код строит его сам (`candidates_to_json`). Ссылка
    # (`url`) законно несёт домен — это проверяемый адрес источника, а не
    # догадка; догадкой было бы именно поле `org`, и вот оно обязано быть null.
    prompt_json = candidates_to_json(group)
    payload = json.loads(prompt_json)
    assert payload[0]["org"] is None, "код обязан передать модели честное «не заполнено», а не адрес сайта"
    assert f'"org": "{_DOMAIN}"' not in prompt_json, "домен не должен стать в промпте фактом об организации"

    card_json = json.dumps(
        {
            "title": "Решение с площадки конкурса",
            "essence": "Команда представила решение на конкурсной площадке; организация источниками не подтверждена.",
            "developer_org": None,
            "evidence": [],
            "applicability": "не найдено",
            "next_step": "не найдено",
        },
        ensure_ascii=False,
    )
    card_provider = _FixedResponseProvider("deepseek", card_json)
    card_router = LLMRouter(settings, providers={"deepseek": card_provider})

    card = await stages.author_card(card_router, brief, group)

    # Тот же довод от противного, но уже на реальном системном сообщении,
    # которое получила модель карточки: ссылка в нём законно есть (это адрес
    # источника), а вот значением поля `org` домен быть не должен.
    system_text = card_provider.calls[0][0].content
    assert str(candidate.url) in system_text, "ссылка на источник обязана дойти до модели"
    assert f'"org": "{_DOMAIN}"' not in system_text

    report = Report(
        request_id=1,
        brief=brief,
        cards=[card],
        summary=ReportSummary(recommendation="Рассмотреть вариант №1."),
        generated_at=datetime.now(UTC),
    )
    markdown = render_markdown(report)

    # Критерий 1: ни одна строка отчёта не называет домен организацией —
    # разработчик честно напечатан как «не найдено», адреса сайта в тексте нет.
    assert _DOMAIN not in markdown
    assert "не найдено" in markdown
