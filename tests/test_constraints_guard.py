"""Проверки страховки ограничений задачи (РЕШ-115, РЕШ-116, РЕШ-122).

Инвариант подэтапа Э2-В: непустой текст задачи с признаками измеримых
требований => непустой `Brief.constraints` либо явная строка клиенту.
Молчаливого третьего исхода нет - сегодня он есть, и именно он провалил
ворота Э2-Б (`.planning/evidence/E2B-gate-фарма.md:9`).

Сеть не используется: все вызовы модели - через `FakeProvider` (РЕШ-110).
"""

from __future__ import annotations

import json
import logging

import pytest

from app.llm import CostLimitExceededError, LLMRouter
from app.llm.base import LLMError, LLMProvider, LLMResult, Message, Usage
from app.pipeline import stages
from app.pipeline.constraints_guard import (
    STAGE_INTAKE_CONSTRAINTS,
    ensure_requirement_checks,
    has_requirement_signs,
    retry_constraints_if_missing,
)
from app.schemas import Brief, Candidate, RequirementCheck, ScoredCandidate, SolutionCard, SourceType

#: Текст из `.planning/evidence/E2B-gate-фарма.md:9` - боевой текст, потерявший
#: ограничения на воротах Э2-Б: «Требуемая точность 95-98%, уровень готовности
#: TRL 6-9» осталось в `task_summary`, `constraints` вышел пустым.
_FARMA_TEXT = (
    "Разработка системы автоматического анализа ультразвуковых исследований свиноматок на основе ИИ "
    "для повышения точности диагностики супоросности, прохолоста и микроаборта. Требуемая точность "
    "95-98%, уровень готовности TRL 6-9."
)

#: Текст без единого признака требований: ни числа, ни процента, ни «не
#: менее/не более», ни «TRL», ни «ГОСТ».
_NO_SIGNS_TEXT = (
    "Нужно найти технологию переработки бытовых отходов для среднего города без потери эффективности "
    "сортировки на существующей линии."
)


def _brief(*, constraints: list[str] | None = None, industry: str = "фарма") -> Brief:
    return Brief(
        industry=industry,
        task_summary="Суть задачи для теста страховки ограничений.",
        constraints=constraints or [],
    )


def _intake_response(*, industry: str, task_summary: str, constraints: list[str]) -> str:
    return json.dumps(
        {
            "questions": [],
            "draft_brief": {
                "industry": industry,
                "task_summary": task_summary,
                "constraints": constraints,
                "target_maturity": None,
                "geography": "РФ",
                "budget_hint": None,
                "timeline_hint": None,
                "keywords_ru": ["диагностика супоросности"],
                "keywords_en": ["sow pregnancy diagnostics"],
            },
        },
        ensure_ascii=False,
    )


# --- Критерий 1: детектор признаков ------------------------------------------


def test_has_requirement_signs_true_on_farma_text_with_percent_and_trl():
    """Боевой текст, потерявший ограничения на воротах Э2-Б, обязан дать True."""
    assert has_requirement_signs(_FARMA_TEXT) is True


def test_has_requirement_signs_false_without_any_sign():
    """Текст без чисел, процентов, «не менее/не более», «TRL» и «ГОСТ» - False."""
    assert has_requirement_signs(_NO_SIGNS_TEXT) is False


@pytest.mark.parametrize(
    "text",
    [
        "Требуется КПД около 92% в среднем режиме эксплуатации установки.",
        "Продукт должен достичь заявленного уровня TRL в финале проекта.",
        "Прочность материала не менее нормативной для данного класса изделий.",
        "Срок службы не более нормативного для данного класса оборудования.",
        "Изделие обязано соответствовать требованиям ГОСТ по безопасности.",
        "Рабочая температура достигает 850°C в пиковом режиме эксплуатации.",
    ],
    ids=["процент", "trl", "не-менее", "не-более", "гост", "число-с-единицей"],
)
def test_has_requirement_signs_true_on_each_sign_in_isolation(text: str):
    """Мутация (а): детектор обязан ловить КАЖДЫЙ признак по отдельности -
    иначе «перестать видеть %/TRL» проходит незамеченным ни одним тестом."""
    assert has_requirement_signs(text) is True


# --- Критерии 2, 3: повторный адресный вызов ---------------------------------


async def test_retry_constraints_fills_only_constraints_field(settings, fake_provider_factory):
    """Критерий 2: при пустом `constraints` и признаках требований интейк
    зовётся ровно один лишний раз, а ответ пополняет только `constraints`."""
    first = _intake_response(industry="фарма", task_summary="Суть задачи из первого разбора.", constraints=[])
    second = _intake_response(
        industry="подменённая отрасль - не должна попасть в бриф",
        task_summary="подменённая суть - не должна попасть в бриф",
        constraints=["точность 95-98%", "TRL 6-9"],
    )
    provider = fake_provider_factory("gigachat", [first, second])
    router = LLMRouter(settings, providers={"gigachat": provider})

    intake = await stages.run_intake(router, _FARMA_TEXT)
    assert router.cost.calls == 1, "база отсчёта: один вызов на сам run_intake"

    stage_log: list = []
    updated_brief, unresolved = await retry_constraints_if_missing(
        router, intake.draft_brief, _FARMA_TEXT, industry_hint="фарма", stage_log=stage_log
    )

    assert router.cost.calls == 2, "повторный вызов обязан состояться ровно один раз - на одно обращение больше базы"
    assert unresolved is False
    assert updated_brief.constraints == ["точность 95-98%", "TRL 6-9"]
    assert updated_brief.task_summary == intake.draft_brief.task_summary, (
        "второй вызов вызван за одним полем - task_summary переписывать не должен"
    )
    assert updated_brief.keywords_ru == intake.draft_brief.keywords_ru, "ключевые слова не должны разъехаться"
    assert updated_brief.industry == intake.draft_brief.industry, "отрасль первого ответа должна уцелеть"
    assert len(stage_log) == 1
    assert stage_log[0].stage == STAGE_INTAKE_CONSTRAINTS
    assert stage_log[0].error is None


async def test_retry_constraints_skipped_without_requirement_signs(settings, fake_provider_factory):
    """Критерий 3: пустой `constraints`, но признаков требований нет -
    повторного вызова быть не должно."""
    provider = fake_provider_factory("gigachat", ["{}"])
    router = LLMRouter(settings, providers={"gigachat": provider})
    brief = _brief(constraints=[])
    stage_log: list = []

    updated_brief, unresolved = await retry_constraints_if_missing(
        router, brief, _NO_SIGNS_TEXT, industry_hint="не указана", stage_log=stage_log
    )

    assert router.cost.calls == 0, "без признаков требований в тексте лишнего вызова быть не должно"
    assert unresolved is False
    assert updated_brief is brief
    assert stage_log == []


async def test_retry_constraints_skipped_when_constraints_already_present(settings, fake_provider_factory):
    """Обратная сторона критерия 2: `constraints` уже заполнен - вызова нет
    независимо от признаков в тексте (мутация (б): вызов делается всегда)."""
    provider = fake_provider_factory("gigachat", ["{}"])
    router = LLMRouter(settings, providers={"gigachat": provider})
    brief = _brief(constraints=["уже есть ограничение"])

    updated_brief, unresolved = await retry_constraints_if_missing(
        router, brief, _FARMA_TEXT, industry_hint="фарма", stage_log=[]
    )

    assert router.cost.calls == 0
    assert unresolved is False
    assert updated_brief is brief


async def test_retry_constraints_empty_second_answer_marks_unresolved(settings, fake_provider_factory):
    """Второй вызов тоже не дал ограничений - сигнал `constraints_unresolved=True`,
    а не пустой список без объяснения (критерий 4, готовит клиентскую строку)."""
    first = _intake_response(industry="фарма", task_summary="Суть.", constraints=[])
    second = _intake_response(industry="фарма", task_summary="Суть.", constraints=[])
    provider = fake_provider_factory("gigachat", [first, second])
    router = LLMRouter(settings, providers={"gigachat": provider})
    brief = _brief(constraints=[])

    updated_brief, unresolved = await retry_constraints_if_missing(
        router, brief, _FARMA_TEXT, industry_hint="фарма", stage_log=[]
    )

    assert unresolved is True
    assert updated_brief.constraints == []


# --- Критерий 7: отказы повторного вызова ------------------------------------


class _AlwaysFailsProvider(LLMProvider):
    """Провайдер, отказывающий без права на повтор - без сна в тесте
    (`retryable=False` не даёт `LLMRouter.complete` ждать между попытками)."""

    name = "gigachat"
    price_in_rub_per_mtok = 10.0
    price_out_rub_per_mtok = 20.0

    async def complete(self, messages: list[Message], **kwargs: object) -> LLMResult:
        raise LLMError(self.name, "недоступен", retryable=False)

    async def health(self) -> tuple[bool, str]:
        return False, "недоступен"


class _CostlyProvider(LLMProvider):
    """Провайдер, у которого один вызов дороже всего лимита запроса - тот же
    образец, что `test_pipeline_resilience._CostlyProvider`."""

    name = "gigachat"
    price_in_rub_per_mtok = 10.0
    price_out_rub_per_mtok = 20.0

    async def complete(self, messages: list[Message], **kwargs: object) -> LLMResult:
        return LLMResult(
            text="{}",
            usage=Usage(tokens_in=10, tokens_out=10, cost_rub=99.0, provider=self.name, model="fake"),
        )

    async def health(self) -> tuple[bool, str]:
        return True, "заглушка"


async def test_retry_constraints_llm_error_does_not_raise_run_continues(settings):
    """Отказ повторного вызова не роняет прогон (инвариант №5): предупреждение
    в лог, своя строка журнала стадий, `constraints_unresolved=True`."""
    router = LLMRouter(settings, providers={"gigachat": _AlwaysFailsProvider()})
    brief = _brief(constraints=[])
    stage_log: list = []

    updated_brief, unresolved = await retry_constraints_if_missing(
        router, brief, _FARMA_TEXT, industry_hint="фарма", stage_log=stage_log
    )

    assert unresolved is True
    assert updated_brief is brief
    assert len(stage_log) == 1
    assert stage_log[0].stage == STAGE_INTAKE_CONSTRAINTS
    assert stage_log[0].error


async def test_retry_constraints_cost_limit_propagates(settings):
    """Исчерпание лимита стоимости (РЕШ-45) - команда «стоп тратить», не отказ
    модели: исключение обязано выйти наружу, а не смягчиться."""
    router = LLMRouter(settings, providers={"gigachat": _CostlyProvider()})
    brief = _brief(constraints=[])
    stage_log: list = []

    with pytest.raises(CostLimitExceededError):
        await retry_constraints_if_missing(router, brief, _FARMA_TEXT, industry_hint="фарма", stage_log=stage_log)

    assert len(stage_log) == 1, "трата уже случилась до броска - строка журнала обязана остаться"
    assert stage_log[0].stage == STAGE_INTAKE_CONSTRAINTS
    assert stage_log[0].error


# --- Регрессия Э2-Г (диагноз E2D-З9-diagnosis.md): непустой constraints с ----
# --- непокрытым маркером ------------------------------------------------------

#: Текст с двумя маркерами `_LEVEL_MARKERS`, ни один из которых не совпадает с
#: единственным уже собранным требованием - критерий 5 (один вызов на оба).
_TWO_MARKERS_TEXT = (
    "Прочность материала не менее нормативной для данного класса изделий. "
    "Уровень технологической готовности - TRL 6-9."
)


async def test_retry_constraints_fires_when_constraints_non_empty_but_marker_uncovered(
    settings, fake_provider_factory
):
    """Регрессия Э2-Г: `constraints` уже содержит точность (список непуст), но
    в тексте задачи есть маркер TRL, не покрытый ни одним собранным
    требованием - страховка обязана дозвониться и добавить его, а не молчать
    только потому, что список формально не пуст."""
    second = _intake_response(
        industry="фарма",
        task_summary="не должно попасть в бриф",
        constraints=["уровень технологической готовности от TRL 6 до TRL 9"],
    )
    provider = fake_provider_factory("gigachat", [second])
    router = LLMRouter(settings, providers={"gigachat": provider})
    brief = _brief(constraints=["точность 95-98%"])
    stage_log: list = []

    updated_brief, unresolved = await retry_constraints_if_missing(
        router, brief, _FARMA_TEXT, industry_hint="фарма", stage_log=stage_log
    )

    assert router.cost.calls == 1, "непокрытый маркер TRL обязан вызвать один адресный повторный вызов"
    assert unresolved is False
    assert updated_brief.constraints == [
        "точность 95-98%",
        "уровень технологической готовности от TRL 6 до TRL 9",
    ], "требования объединяются, а не затираются - иначе фикс терял бы уже собранную точность (мутация б)"
    assert len(stage_log) == 1
    assert stage_log[0].stage == STAGE_INTAKE_CONSTRAINTS
    assert stage_log[0].error is None


async def test_retry_constraints_skipped_when_marker_already_covered(settings, fake_provider_factory):
    """Критерий 4, первая половина: маркер TRL уже покрыт собранным
    требованием - здоровый прогон не дорожает, повторного вызова нет."""
    provider = fake_provider_factory("gigachat", ["{}"])
    router = LLMRouter(settings, providers={"gigachat": provider})
    brief = _brief(constraints=["точность 95-98%, уровень готовности TRL 6-9"])

    updated_brief, unresolved = await retry_constraints_if_missing(
        router, brief, _FARMA_TEXT, industry_hint="фарма", stage_log=[]
    )

    assert router.cost.calls == 0, "мутация (а): условие расширено до «любой прогон» - здесь оно обязано молчать"
    assert unresolved is False
    assert updated_brief is brief


async def test_retry_constraints_marker_coverage_ignores_case(settings, fake_provider_factory):
    """Мутация (в): сравнение маркера с требованием обязано игнорировать
    регистр - «trl» в требовании покрывает «TRL» в тексте задачи."""
    provider = fake_provider_factory("gigachat", ["{}"])
    router = LLMRouter(settings, providers={"gigachat": provider})
    brief = _brief(constraints=["готовность trl 6-9"])

    updated_brief, unresolved = await retry_constraints_if_missing(
        router, brief, _FARMA_TEXT, industry_hint="фарма", stage_log=[]
    )

    assert router.cost.calls == 0
    assert unresolved is False
    assert updated_brief is brief


async def test_retry_constraints_two_uncovered_markers_trigger_single_call(settings, fake_provider_factory):
    """Критерий 5: два непокрытых маркера в одном тексте - вызов всё равно
    один, а не по одному на маркер (мутация г)."""
    second = _intake_response(
        industry="пром",
        task_summary="s",
        constraints=["прочность не менее нормативной", "уровень готовности TRL 6-9"],
    )
    provider = fake_provider_factory("gigachat", [second])
    router = LLMRouter(settings, providers={"gigachat": provider})
    brief = _brief(constraints=["исходное требование без обоих маркеров"], industry="пром")

    updated_brief, unresolved = await retry_constraints_if_missing(
        router, brief, _TWO_MARKERS_TEXT, industry_hint="пром", stage_log=[]
    )

    assert router.cost.calls == 1, "два непокрытых маркера - один вызов, не два"
    assert unresolved is False
    assert updated_brief.constraints == [
        "исходное требование без обоих маркеров",
        "прочность не менее нормативной",
        "уровень готовности TRL 6-9",
    ]


# --- Критерий 6: страховка блока сверки --------------------------------------


def _card(**overrides: object) -> SolutionCard:
    fields = {
        "title": "Т",
        "essence": "Э",
        "applicability": "П",
        "next_step": "С",
    }
    fields.update(overrides)
    return SolutionCard(**fields)


def test_ensure_requirement_checks_backfills_not_confirmed_per_requirement():
    """Непустой `constraints`, пустой `requirement_checks` - код заполняет по
    одному пункту на требование со статусом `not_confirmed`."""
    brief = _brief(constraints=["точность 95-98%", "TRL 6-9"])
    card = _card(requirement_checks=[])

    result = ensure_requirement_checks(card, brief)

    assert len(result.requirement_checks) == 2
    assert {item.requirement for item in result.requirement_checks} == {"точность 95-98%", "TRL 6-9"}
    assert all(item.status == "not_confirmed" for item in result.requirement_checks)


def test_ensure_requirement_checks_leaves_existing_checks_untouched():
    """Модель уже вернула сверку - страховка её не трогает и не дублирует."""
    brief = _brief(constraints=["точность 95-98%"])
    existing = [
        RequirementCheck(requirement="точность 95-98%", status="confirmed", evidence_url="https://example.org/a")
    ]
    card = _card(requirement_checks=existing)

    result = ensure_requirement_checks(card, brief)

    assert result.requirement_checks == existing


def test_ensure_requirement_checks_noop_without_brief_constraints():
    """Пустой `constraints` брифа - заполнять нечем, страховка ничего не делает."""
    brief = _brief(constraints=[])
    card = _card(requirement_checks=[])

    result = ensure_requirement_checks(card, brief)

    assert result.requirement_checks == []


def test_ensure_requirement_checks_logs_on_backfill(caplog):
    """РЕШ-122: страховка обязана печатать строку лога именно при срабатывании
    подстановки, а не всегда - иначе частота срабатывания невидима, и
    обещание "число, которое можно предъявить" непроверяемо (ревью, цикл 1,
    находка 2)."""
    brief = _brief(constraints=["точность 95-98%", "TRL 6-9"])
    card = _card(requirement_checks=[])

    with caplog.at_level(logging.INFO, logger="app.pipeline.constraints_guard"):
        ensure_requirement_checks(card, brief)

    assert any("страховка requirement_checks сработала" in record.message for record in caplog.records), (
        "срабатывание страховки обязано оставить строку лога"
    )


def test_ensure_requirement_checks_does_not_log_when_not_triggered(caplog):
    """Обратная сторона: модель уже вернула сверку - строки лога быть не
    должно, иначе частота срабатывания страховки станет непоказательной."""
    brief = _brief(constraints=["точность 95-98%"])
    existing = [RequirementCheck(requirement="точность 95-98%", status="confirmed")]
    card = _card(requirement_checks=existing)

    with caplog.at_level(logging.INFO, logger="app.pipeline.constraints_guard"):
        ensure_requirement_checks(card, brief)

    assert not any("страховка requirement_checks сработала" in record.message for record in caplog.records), (
        "страховка не срабатывала - строки лога быть не должно"
    )


def _scored_publication() -> ScoredCandidate:
    candidate = Candidate(
        source_type=SourceType.PUBLICATION,
        title="Катализатор с присадками",
        url="https://doi.org/10.1/x",
        org="Институт катализа",
        year=2025,
        snippet="Испытан на действующей установке.",
        raw_id="10.1/x",
        provider="crossref",
    )
    return ScoredCandidate(candidate=candidate, relevance=8, maturity=6, accessibility=7, freshness=6, completeness=7)


async def test_author_card_backfills_requirement_checks_when_model_omits_them(settings, fake_provider_factory):
    """Мутация (е): страховка обязана быть ВЫЗВАНА стадией `author_card`, а не
    только существовать как отдельная функция - сквозной прогон через стадию."""
    card_json = json.dumps(
        {
            "title": "Катализатор с присадками",
            "essence": "Присадка продлевает срок службы катализатора.",
            "applicability": "Закрывает задачу продления межрегенерационного цикла.",
            "next_step": "Запросить протокол испытаний.",
            "evidence": [{"claim": "Испытан на установке", "url": "https://doi.org/10.1/x"}],
            "requirement_checks": [],
        },
        ensure_ascii=False,
    )
    provider = fake_provider_factory("deepseek", [card_json])
    router = LLMRouter(settings, providers={"deepseek": provider})
    brief = _brief(constraints=["точность 95-98%", "TRL 6-9"])

    card = await stages.author_card(router, brief, [_scored_publication()])

    assert len(card.requirement_checks) == 2
    assert {item.requirement for item in card.requirement_checks} == {"точность 95-98%", "TRL 6-9"}
    assert all(item.status == "not_confirmed" for item in card.requirement_checks)
