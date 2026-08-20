"""Проверки код-нормализации конкурсных формулировок плана (РЕШ-121).

План строит модель, а РЕШ-109 запрещает считать её послушание гарантией:
`normalize_contest_queries` приводит план к ровно двум формулировкам `contest`
независимо от того, что вернула модель — ноль, одну без якорей итогов или
несколько лишних. Вызывается и в `plan_search`, и в `replan_search`
(мутация (г) постановки задачи): в наборе обязателен случай второго прохода,
иначе снятие вызова из `replan_search` осталось бы незамеченным.

Сеть не используется: сеть не используется вовсе — `normalize_contest_queries`
чистая функция, а один тест на `replan_search` идёт через `FakeProvider`.
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


def test_default_contest_queries_use_brief_keywords():
    """Достроенная кодом формулировка берёт тему из `brief.keywords_ru`, а не
    заглушку без темы задачи."""
    plan = SearchPlan(queries=[_query("диагностика супоросности УЗИ", source_type=SourceType.PUBLICATION)])
    brief = _brief(keywords_ru=["диагностика супоросности"])

    result = stages.normalize_contest_queries(plan, brief)

    narrowed, outcome = _split(result.queries)
    assert "диагностика супоросности" in narrowed[0].text
    assert "диагностика супоросности" in outcome[0].text


# ── Мутация (г): normalize_contest_queries обязана звать и plan_search, и replan_search ──


async def test_replan_search_also_normalizes_contest_queries(settings, fake_provider_factory):
    """Второй проход строит собственный план моделью заново - без вызова
    normalize_contest_queries внутри `replan_search` (а не только внутри
    `plan_search`) конкурсный класс второго прохода остался бы без гарантии
    РЕШ-121."""
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
    provider = fake_provider_factory("gigachat", [plan_json])
    router = LLMRouter(settings, providers={"gigachat": provider})

    brief = _brief(industry="агропром", task_summary="Автоматизация выпойки поросят.", keywords_ru=["выпойка поросят"])
    previous_plan = SearchPlan(queries=[_query("выпойка поросят", source_type=SourceType.WEB)])
    candidate = Candidate(source_type=SourceType.WEB, title="Т", url="https://example.org/a", provider="yandex_search")
    scored = [
        ScoredCandidate(candidate=candidate, relevance=2, maturity=2, accessibility=2, freshness=2, completeness=2)
    ]

    result = await stages.replan_search(router, brief, previous_plan, scored)

    narrowed, outcome = _split(result.queries)
    assert len(narrowed) == 1, "replan_search не нормализовал формулировку «площадки»"
    assert len(outcome) == 1, "replan_search не нормализовал формулировку «итоги»"
