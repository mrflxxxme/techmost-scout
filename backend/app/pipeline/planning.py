"""Стадия 2 — план поиска: бриф → набор поисковых формулировок.

Вынесено из `stages.py` (РЕШ-94) без изменения поведения — только перенос
тела функций и нужных импортов. Сюда же перенесены `plan_queries_listing`,
`first_pass_listing` и `replan_search` (раздел «4а» исходного файла): это
вторая половина условного второго прохода поиска, отвечающая за сам план,
тогда как решение «нужен ли проход» (`needs_second_pass`) осталось в
`scoring.py` — оно судит по оценкам модели, а не строит формулировки.
"""

from __future__ import annotations

import logging

from annotated_types import MaxLen

from app.llm import LLMRouter, Message
from app.pipeline.prompting import _GO, render_prompt
from app.pipeline.scoring import has_neutral_score
from app.schemas import Brief, ScoredCandidate, SearchPlan, SearchQuery, SourceType
from app.search.yandex_search import has_contest_outcome_marker

log = logging.getLogger(__name__)


# ── 2. План поиска ──────────────────────────────────────────────────────────
async def plan_search(router: LLMRouter, brief: Brief) -> SearchPlan:
    """Превратить бриф в набор поисковых формулировок на двух языках."""
    plan = await router.complete_json(
        "planner",
        [
            Message(role="system", content=render_prompt("planner", BRIEF_JSON=brief.model_dump_json(indent=2))),
            Message(role="user", content=_GO),
        ],
        SearchPlan,
        # План бывает из двух десятков формулировок, у каждой обоснование —
        # стандартных 4096 токенов на это не хватает.
        max_tokens=8000,
    )
    return normalize_contest_queries(plan, brief)


#: Признак «модель не вернула нужную конкурсную формулировку» — печатается в
#: лог при каждом срабатывании нормализации, тем же приёмом, что и страховка
#: requirement_checks (РЕШ-122): молчаливая подмена превратила бы дефект
#: промпта в вечный, а видимая — в число, которое можно предъявить.
_CONTEST_NARROWED_RATIONALE = (
    "код-нормализация плана (РЕШ-121): формулировка «площадки» достроена, модель её не вернула"
)
_CONTEST_OUTCOME_RATIONALE = "код-нормализация плана (РЕШ-121): формулировка «итоги» достроена, модель её не вернула"

#: Потолок `SearchPlan.queries` (`schemas.py`), прочитанный из самой схемы, а
#: не продублированный числом рядом (РЕШ-127 п. 1): `plan.model_copy` в
#: pydantic v2 не валидирует результат, и план из 12 формулировок без единой
#: `contest` молча превращался в план из 14 — план сверх потолка писался в
#: `search_plans`, а `load_search_plan` потом не мог прочитать его через ту же
#: схему (`SearchPlan.model_validate`). Число берётся отсюда, чтобы правка
#: потолка в схеме не разошлась с этим местом молча.
#: Совет повторной проверки пост-аудита, взят оркестратором: без значения по
#: умолчанию `next` бросает `StopIteration` прямо на импорте модуля — если
#: pydantic изменит представление метаданных, падение будет без внятной
#: причины и в неожиданном месте. Явный отказ называет, что именно потеряно.
_max_plan_queries = next(
    (m.max_length for m in SearchPlan.model_fields["queries"].metadata if isinstance(m, MaxLen)),
    None,
)
if _max_plan_queries is None:  # pragma: no cover — защита от молчаливой потери потолка
    raise RuntimeError(
        "потолок SearchPlan.queries не объявлен через MaxLen: нормализация плана осталась бы без границы, "
        "а план сверх потолка пишется в search_plans и потом не читается той же схемой"
    )
_MAX_PLAN_QUERIES = _max_plan_queries


def normalize_contest_queries(plan: SearchPlan, brief: Brief) -> SearchPlan:
    """Код-гарантия РЕШ-121: после этой функции в плане ровно две формулировки
    типа `contest` — одна без якорей итогов (уйдёт с сужением по площадкам,
    `_with_site_filter`), одна с ними (уйдёт открытым вебом,
    `has_contest_outcome_marker`).

    План строит модель, а РЕШ-109 запрещает считать её послушание гарантией:
    лишние конкурсные формулировки отбрасываются, недостающая достраивается
    кодом из `brief.keywords_ru`. Вызывается в конце `plan_search` и
    `replan_search`, ДО записи плана в `search_plans` (РЕШ-83) — прогон по
    сохранённому плану (`--plan-id`) воспроизводит ровно то, что было
    отправлено, а не пересчитывает нормализацию заново при каждом повторе.
    """
    contest = [q for q in plan.queries if q.source_type is SourceType.CONTEST]
    other = [q for q in plan.queries if q.source_type is not SourceType.CONTEST]

    narrowed = next((q for q in contest if not has_contest_outcome_marker(q.text)), None)
    outcome = next((q for q in contest if has_contest_outcome_marker(q.text)), None)

    if narrowed is None:
        log.info("normalize_contest_queries: формулировка «площадки» достроена кодом")
        narrowed = _default_contest_query(brief, outcome=False)
    if outcome is None:
        log.info("normalize_contest_queries: формулировка «итоги» достроена кодом")
        outcome = _default_contest_query(brief, outcome=True)

    # РЕШ-127 п. 1: потолок схемы главнее числа формулировок, которые вернула
    # модель. `model_copy(update=...)` в pydantic v2 результат не валидирует,
    # и без этой проверки план из 12 формулировок без единой `contest` молча
    # становился планом из 14 — за объявленным потолком `SearchPlan.queries`
    # (`schemas.py`). Вытесняются САМЫЕ ПОСЛЕДНИЕ формулировки основной
    # очереди: `other` короче терять хвост, чем середину, а что именно
    # вытеснено, обязано быть видно в логе, а не пропадать молча.
    overflow = len(other) + 2 - _MAX_PLAN_QUERIES
    if overflow > 0:
        dropped, other = other[-overflow:], other[:-overflow]
        log.warning(
            "normalize_contest_queries: потолок схемы %s формулировок — конкурсные вытеснили %s: %s",
            _MAX_PLAN_QUERIES,
            overflow,
            "; ".join(q.text for q in dropped),
        )

    return plan.model_copy(update={"queries": [*other, narrowed, outcome]})


def strip_contest_queries(plan: SearchPlan) -> SearchPlan:
    """Код-гарантия РЕШ-126: второй проход конкурсные формулировки не шлёт.

    Второй проход существует ради ГЛАВНОЙ очереди — порог его включения
    считается по ней (`needs_second_pass`), а конкурсный класс идёт отдельной
    секцией вне лимита 5-7 и вне метрики покрытия (РЕШ-85). Платить вторым
    платным проходом за повторный поиск конкурсов значит тратить деньги на
    очередь, ради которой проход не запускался: конкурсы ищутся один раз за
    прогон, в `plan_search`, обеими формулировками (РЕШ-117).

    Промпт `replanner.md` конкурсные формулировки не просит, но РЕШ-109
    запрещает считать послушание модели гарантией: если она всё же вернула
    тип `contest`, он вырезается кодом ДО записи плана в `search_plans`
    (симметрично тому, как `normalize_contest_queries` достраивает конкурсные
    в первом проходе) — иначе спрос на платные запросы Яндекса снова
    превышает `max_queries` ровно так, как нашёл аудит волны Э2-В: спрос 8 при
    пределе 6, обе конкурсные формулировки второго прохода отбрасывались
    источником детерминированно, потому что нормализация ставила их в конец
    списка, а предел исчерпывался раньше, на web-формулировках.
    """
    kept = [q for q in plan.queries if q.source_type is not SourceType.CONTEST]
    dropped = len(plan.queries) - len(kept)
    if dropped:
        log.info("strip_contest_queries: %s конкурсных формулировок второго прохода срезано кодом (РЕШ-126)", dropped)
    if not kept:
        # Теоретический край: модель реплана вернула ИСКЛЮЧИТЕЛЬНО конкурсные
        # формулировки — план без главной очереди для второго прохода не
        # имеет смысла (SearchPlan.queries требует минимум одну формулировку).
        # `replan_search` вызывается из `_second_pass`, который отказ стадии
        # (в том числе этот) уже ловит и продолжает прогон по первой выдаче
        # (инвариант №5) — падать сюда безопаснее, чем строить пустой план.
        raise ValueError("второй план поиска целиком состоит из конкурсных формулировок — план без главной очереди")
    return plan.model_copy(update={"queries": kept})


def _default_contest_query(brief: Brief, *, outcome: bool) -> SearchQuery:
    """Достроить недостающую конкурсную формулировку кодом (РЕШ-121).

    Текст собирается из `brief.keywords_ru`, а не заглушкой без темы задачи:
    без ключевых слов формулировка искала бы вообще ничего не значащий текст
    «хакатон итоги».
    """
    keyword = brief.keywords_ru[0] if brief.keywords_ru else brief.industry
    if outcome:
        text = f"итоги хакатона победитель проект команды {keyword}"
        rationale = _CONTEST_OUTCOME_RATIONALE
    else:
        text = f"хакатон конкурс решение команды {keyword}"
        rationale = _CONTEST_NARROWED_RATIONALE
    return SearchQuery(text=text, source_type=SourceType.CONTEST, lang="ru", rationale=rationale)


def plan_queries_listing(plan: SearchPlan) -> str:
    """Формулировки плана строками — вход стадии `replan`.

    Модель обязана видеть, что уже спрашивали: без этого «уточнённый» план
    получается пересказом прежнего, и второй проход опрашивает источники теми
    же словами за те же деньги.
    """
    lines = [f"— [{query.source_type.value}/{query.lang}] {query.text}" for query in plan.queries]
    return "\n".join(lines) or "формулировок не было"


#: Сколько кандидатов первой выдачи показывать стадии `replan`. Прогон приносит
#: до сотни, и весь список раздул бы вход ради хвоста с одинаково низкими
#: оценками: для вывода «искали не там» хватает верхушки.
_REPLAN_LISTING_LIMIT = 40


def first_pass_listing(scored: list[ScoredCandidate], *, limit: int = _REPLAN_LISTING_LIMIT) -> str:
    """Первая выдача для стадии `replan`: оценка, тип источника, заголовок, организация.

    Ни ссылок, ни фрагментов: стадия не сверяет утверждения, а называет, чего не
    нашли, и лишний текст тут только съедает лимит. Кандидаты идут по убыванию
    `relevance` — промах виднее всего по верхушке списка.

    Нейтральные оценки помечены словами, а не пятёркой (`has_neutral_score`):
    показать модели поставленное кодом число как её собственное суждение значит
    соврать ей о качестве выдачи.
    """
    ranked = sorted(scored, key=lambda item: item.relevance, reverse=True)[:limit]
    lines = []
    for position, item in enumerate(ranked):
        mark = "оценка не получена" if has_neutral_score(item) else f"relevance {item.relevance}"
        org = item.candidate.org or "организация не указана"
        lines.append(f"[{position}] {mark} | {item.candidate.source_type.value} | {item.candidate.title[:160]} | {org}")
    return "\n".join(lines) or "первая выдача пуста"


async def replan_search(router: LLMRouter, brief: Brief, plan: SearchPlan, scored: list[ScoredCandidate]) -> SearchPlan:
    """Новый план поиска по итогам слабой первой выдачи (РЕШ-81).

    Модель получает бриф, прежние формулировки и верхушку первой выдачи с
    оценками, а возвращает ТОЛЬКО новые формулировки той же схемы `SearchPlan`:
    переписывать вход в ответ её не просят (грабля §0.1 п. 2 SPEC) — на
    кандидатов она ссылается номерами списка.
    """
    new_plan = await router.complete_json(
        "replan",
        [
            Message(
                role="system",
                content=render_prompt(
                    "replanner",
                    BRIEF_JSON=brief.model_dump_json(indent=2),
                    PREVIOUS_QUERIES=plan_queries_listing(plan),
                    FIRST_PASS_RESULTS=first_pass_listing(scored),
                ),
            ),
            Message(role="user", content=_GO),
        ],
        SearchPlan,
        # Тот же расчёт, что у `plan_search`: ответ — план до двенадцати
        # формулировок с обоснованием каждой, и стадии предстоит рассуждение о
        # промахе первой выдачи вдобавок к нему.
        max_tokens=8000,
    )
    # РЕШ-126 (правка блокирующей находки аудита волны Э2-В): второй проход
    # конкурсные формулировки НЕ достраивает и НЕ шлёт вовсе. До этой правки
    # здесь стояла та же `normalize_contest_queries`, что и в `plan_search`
    # (мутация (г) постановки задачи З-3) — она честно достраивала обе
    # конкурсные формулировки и на втором проходе, но `yandex_search` отдаёт
    # источнику формулировки типов WEB и CONTEST за оба прохода вместе, а
    # предел `max_queries = 6` считается на весь прогон. Спрос вырос до 8, и
    # обе конкурсные формулировки второго прохода отбрасывались источником
    # ДЕТЕРМИНИРОВАННО: нормализация ставит их в конец списка, а предел
    # исчерпывался раньше, на web-формулировках. См. `strip_contest_queries`.
    return strip_contest_queries(new_plan)
