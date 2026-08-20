"""Проверки код-нормализации конкурсных формулировок плана (РЕШ-121, РЕШ-126).

План строит модель, а РЕШ-109 запрещает считать её послушание гарантией:
`normalize_contest_queries` приводит план ПЕРВОГО прохода (`plan_search`) к
ровно двум формулировкам `contest` независимо от того, что вернула модель —
ноль, одну без якорей итогов или несколько лишних.

Второй проход (`replan_search`) конкурсные формулировки не достраивает и не
шлёт вовсе (РЕШ-126, правка блокирующей находки аудита волны Э2-В): платный
предел Яндекса считается на весь прогон, а не на проход, и повторная отправка
обеих конкурсных формулировок поднимала спрос с 6 до 8 при пределе 6 — обе
терялись детерминированно. За это отвечает `strip_contest_queries`: если
модель реплана вопреки промпту вернула тип `contest`, он вырезается кодом, а
не доезжает до источника.

Сеть не используется: `normalize_contest_queries` и `strip_contest_queries` —
чистые функции, а тесты на `replan_search` идут через `FakeProvider`.
"""

from __future__ import annotations

import json

from app.llm import LLMRouter
from app.pipeline import stages
from app.schemas import Brief, Candidate, ScoredCandidate, SearchPlan, SearchQuery, SourceType
from app.search.yandex_search import has_contest_outcome_marker


def _brief(**overrides: object) -> Brief:
    fields = {
        "industry": "фарма",
        "task_summary": "Диагностика супоросности свиноматок по УЗИ.",
        "keywords_ru": ["диагностика супоросности", "УЗИ свиноматок"],
    }
    fields.update(overrides)
    return Brief(**fields)


def _query(text: str, source_type: SourceType = SourceType.WEB, lang: str = "ru") -> SearchQuery:
    return SearchQuery(text=text, source_type=source_type, lang=lang, rationale="тест")


def _contest_narrowed(text: str = "хакатон решение команды диагностика УЗИ") -> SearchQuery:
    return _query(text, source_type=SourceType.CONTEST)


def _contest_outcome(text: str = "итоги хакатона победитель диагностика УЗИ") -> SearchQuery:
    return _query(text, source_type=SourceType.CONTEST)


def _split(queries: list[SearchQuery]) -> tuple[list[SearchQuery], list[SearchQuery]]:
    """Конкурсные формулировки, разведённые по признаку якоря итогов."""
    contest = [q for q in queries if q.source_type is SourceType.CONTEST]
    narrowed = [q for q in contest if not has_contest_outcome_marker(q.text)]
    outcome = [q for q in contest if has_contest_outcome_marker(q.text)]
    return narrowed, outcome


# ── Критерий 4: три случая, которые обязана закрывать normalize_contest_queries ──


def test_zero_contest_queries_both_built_by_code():
    """Случай 1: модель вернула ноль конкурсных формулировок - обе достраиваются кодом."""
    plan = SearchPlan(queries=[_query("диагностика супоросности УЗИ", source_type=SourceType.PUBLICATION)])

    result = stages.normalize_contest_queries(plan, _brief())

    narrowed, outcome = _split(result.queries)
    assert len(narrowed) == 1
    assert len(outcome) == 1
    # Исходная не-конкурсная формулировка сохранена как есть
    assert any(q.source_type is SourceType.PUBLICATION for q in result.queries)


def test_one_narrowed_contest_query_outcome_built_by_code():
    """Случай 2: модель вернула одну (без якорей) - код достраивает только "итоги",
    а модельную формулировку не переписывает."""
    plan = SearchPlan(
        queries=[_query("диагностика супоросности УЗИ", source_type=SourceType.PUBLICATION), _contest_narrowed()]
    )

    result = stages.normalize_contest_queries(plan, _brief())

    narrowed, outcome = _split(result.queries)
    assert len(narrowed) == 1
    assert len(outcome) == 1
    assert narrowed[0].text == _contest_narrowed().text, "формулировку модели переписывать нельзя"


def test_three_contest_queries_two_with_anchors_reduced_to_exactly_two():
    """Случай 3: модель вернула три (две с якорями) - лишняя отбрасывается,
    в плане остаётся ровно одна с якорями и одна без."""
    plan = SearchPlan(
        queries=[
            _query("диагностика супоросности УЗИ", source_type=SourceType.PUBLICATION),
            _contest_outcome("итоги хакатона победитель первый вариант"),
            _contest_outcome("призёр хакатона проект команды второй вариант"),
            _contest_narrowed(),
        ]
    )

    result = stages.normalize_contest_queries(plan, _brief())

    narrowed, outcome = _split(result.queries)
    assert len(narrowed) == 1
    assert len(outcome) == 1


def test_contest_queries_already_correct_are_left_untouched():
    """Обратная сторона: план уже содержит ровно две правильные формулировки -
    нормализация не трогает их текст."""
    plan = SearchPlan(queries=[_contest_narrowed(), _contest_outcome()])

    result = stages.normalize_contest_queries(plan, _brief())

    narrowed, outcome = _split(result.queries)
    assert narrowed[0].text == _contest_narrowed().text
    assert outcome[0].text == _contest_outcome().text


# ── РЕШ-127 п. 1: normalize_contest_queries не обходит потолок схемы ────────


def test_normalize_contest_queries_не_превышает_потолок_схемы_план_валиден():
    """12 формулировок без единой `contest` (потолок SearchPlan.queries) —
    после нормализации план обязан пройти `SearchPlan.model_validate` как
    есть: `model_copy(update=...)` в pydantic v2 результат не проверяет, и до
    правки план из 12 молча становился планом из 14 (аудит волны Э2-В,
    РЕШ-127 п. 1). Такой план потом не может прочитать сам `load_search_plan`
    через ту же схему — прогон записывает то, что сам же не открывает."""
    plan = SearchPlan(queries=[_query(f"формулировка основной очереди {i}") for i in range(12)])

    result = stages.normalize_contest_queries(plan, _brief())

    assert len(result.queries) == 12, "потолок схемы обязан соблюдаться, а не расширяться до 14"
    SearchPlan.model_validate(result.model_dump())  # не должно поднимать ValidationError

    narrowed, outcome = _split(result.queries)
    assert len(narrowed) == 1 and len(outcome) == 1, "обе конкурсные обязаны попасть в план даже при вытеснении"


def test_normalize_contest_queries_вытесняет_последние_формулировки_основной_очереди(caplog):
    """При вытеснении срезаются самые последние формулировки `other` — не
    середина и не начало — и потеря обязана быть видна в логе, а не молча."""
    import logging

    caplog.set_level(logging.WARNING, logger="app.pipeline.stages")
    queries = [_query(f"формулировка основной очереди {i}") for i in range(12)]
    plan = SearchPlan(queries=queries)

    result = stages.normalize_contest_queries(plan, _brief())

    kept_main = [q for q in result.queries if q.source_type is not SourceType.CONTEST]
    assert [q.text for q in kept_main] == [q.text for q in queries[:10]], (
        "вытеснены обязаны быть две ПОСЛЕДНИЕ формулировки основной очереди"
    )
    assert "потолок схемы" in caplog.text and "формулировка основной очереди 10" in caplog.text


def test_default_contest_queries_use_brief_keywords():
    """Достроенная кодом формулировка берёт тему из `brief.keywords_ru`, а не
    заглушку без темы задачи."""
    plan = SearchPlan(queries=[_query("диагностика супоросности УЗИ", source_type=SourceType.PUBLICATION)])
    brief = _brief(keywords_ru=["диагностика супоросности"])

    result = stages.normalize_contest_queries(plan, brief)

    narrowed, outcome = _split(result.queries)
    assert "диагностика супоросности" in narrowed[0].text
    assert "диагностика супоросности" in outcome[0].text


# ── РЕШ-126: replan_search конкурсные формулировки не достраивает и не шлёт ──


async def _replan(brief, previous_plan, scored, plan_json, settings, fake_provider_factory):
    provider = fake_provider_factory("gigachat", [plan_json])
    router = LLMRouter(settings, providers={"gigachat": provider})
    return await stages.replan_search(router, brief, previous_plan, scored)


def _scored_web_candidate() -> list[ScoredCandidate]:
    candidate = Candidate(source_type=SourceType.WEB, title="Т", url="https://example.org/a", provider="yandex_search")
    return [ScoredCandidate(candidate=candidate, relevance=2, maturity=2, accessibility=2, freshness=2, completeness=2)]


async def test_replan_search_не_достраивает_конкурсные_формулировки(settings, fake_provider_factory):
    """Модель реплана вернула план без единой `contest` (как и просит промпт
    replanner.md) — РЕШ-126 требует, чтобы `replan_search` их и не добавлял:
    до правки здесь стояла `normalize_contest_queries`, которая честно
    достраивала обе формулировки заново на каждом втором проходе."""
    plan_json = json.dumps(
        {
            "queries": [
                {
                    "text": "автоматическая выпойка поросят оборудование",
                    "source_type": "web",
                    "lang": "ru",
                    "rationale": "тест",
                }
            ]
        },
        ensure_ascii=False,
    )
    brief = _brief(industry="агропром", task_summary="Автоматизация выпойки поросят.", keywords_ru=["выпойка поросят"])
    previous_plan = SearchPlan(queries=[_query("выпойка поросят", source_type=SourceType.WEB)])

    result = await _replan(brief, previous_plan, _scored_web_candidate(), plan_json, settings, fake_provider_factory)

    narrowed, outcome = _split(result.queries)
    assert narrowed == [] and outcome == [], "replan_search не имеет права заводить конкурсные формулировки (РЕШ-126)"
    assert len(result.queries) == 1, "не-конкурсная формулировка модели обязана дойти без изменений"


async def test_replan_search_срезает_конкурсные_если_модель_их_всё_же_вернула(settings, fake_provider_factory):
    """РЕШ-109: послушание модели — не гарантия. Реплан вопреки промпту вернул
    формулировку типа `contest` вперемешку с `web` — `strip_contest_queries`
    обязана срезать именно её, не тронув остальные."""
    plan_json = json.dumps(
        {
            "queries": [
                {"text": "выпойка поросят автоматика повтор", "source_type": "web", "lang": "ru", "rationale": "тест"},
                {
                    "text": "хакатон агротех выпойка поросят",
                    "source_type": "contest",
                    "lang": "ru",
                    "rationale": "тест: модель вернула конкурс вопреки промпту реплана",
                },
            ]
        },
        ensure_ascii=False,
    )
    brief = _brief(industry="агропром", task_summary="Автоматизация выпойки поросят.", keywords_ru=["выпойка поросят"])
    previous_plan = SearchPlan(queries=[_query("выпойка поросят", source_type=SourceType.WEB)])

    result = await _replan(brief, previous_plan, _scored_web_candidate(), plan_json, settings, fake_provider_factory)

    assert all(q.source_type is not SourceType.CONTEST for q in result.queries), (
        "конкурсная формулировка второго прохода обязана быть срезана кодом, а не доехать до источника"
    )
    assert len(result.queries) == 1 and result.queries[0].source_type is SourceType.WEB


def test_strip_contest_queries_не_трогает_план_без_конкурсных():
    """Обратная сторона: план и так без `contest` — срезать нечего, текст и
    порядок формулировок сохраняются как есть."""
    plan = SearchPlan(queries=[_query("выпойка поросят автоматика"), _query("сенсор влажности корма")])

    result = stages.strip_contest_queries(plan)

    assert result.queries == plan.queries


def test_strip_contest_queries_падает_если_план_целиком_конкурсный():
    """Теоретический край: план реплана состоит ИСКЛЮЧИТЕЛЬНО из `contest`.
    План без главной очереди для второго прохода бессмыслен и нарушает
    минимум схемы (`SearchPlan.queries`, `min_length=1` после среза) —
    стадия обязана упасть понятно, а не молча вернуть пустой план
    (`_second_pass` ловит отказ стадии и продолжает прогон по первой выдаче,
    инвариант №5)."""
    plan = SearchPlan(queries=[_contest_narrowed(), _contest_outcome()])

    try:
        stages.strip_contest_queries(plan)
    except ValueError:
        pass
    else:
        raise AssertionError("план из одних конкурсных формулировок обязан отказать, а не вернуться пустым")
