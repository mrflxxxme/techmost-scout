"""Аналоги из мира: чистые функции отбора и сборки карточек (Э6-2а).

Секция «Аналоги из мира» нужна при виде решения «Рынок» (`product`, `digital`,
`business_model`): зарубежные примеры того, что уже работает на рынке, с
оценкой переносимости в РФ. К «Науке» (`research`) модуль не имеет отношения.

Страна аналога — основание допуска в режиме подачи (`app.countries`), поэтому
ей нельзя верить на слово: структурная страна кандидата (`Candidate.country`,
её проставляет адаптер по данным источника) сильнее страны, названной моделью,
а ссылка-основание страны обязана быть среди ссылок группы. Без этого модель
могла бы «перенести» аналог из исключённой страны в допустимую одним полем.

Модуль не знает ни базы, ни оркестратора (CLAUDE.md: стадии — чистые функции);
вызовы модели идут только через `LLMRouter`.
"""

from __future__ import annotations

import json
import logging

from app.countries import CountryStatus, country_status
from app.llm import LLMRouter, Message
from app.pipeline.prompting import _GO, render_prompt
from app.schemas import AnalogCard, Brief, ScoredCandidate

log = logging.getLogger("app.pipeline.analogs")

#: Код «страна не установлена» из формата ошибок `analog_scorer.md`. Он проходит
#: проверку `^[A-Z]{2}$` схемы, но допуском страны быть не может: карточка с ним
#: не публикуется никогда.
UNKNOWN_COUNTRY = "ZZ"

#: Сколько знаков фрагмента кандидата видит модель — как в `candidates_to_json`.
_SNIPPET_LIMIT = 1500


def prefilter_analog_candidates(scored: list[ScoredCandidate], mode: str) -> list[ScoredCandidate]:
    """Снять кандидатов с известной страной в статусе `excluded` ДО вызова модели.

    Платить за карточку аналога, который заведомо не напечатают (Тайвань в режиме
    «Гос»), незачем. Кандидат без страны проходит: страну по нему может назвать
    модель, и её проверит `select_publishable_analogs` уже по готовой карточке.
    """
    kept: list[ScoredCandidate] = []
    for item in scored:
        code = item.candidate.country
        if code is not None and country_status(code, mode) is CountryStatus.EXCLUDED:
            log.info(
                "аналог «%s»: страна %s исключена в режиме %s — модели не отдаётся", item.candidate.title, code, mode
            )
            continue
        kept.append(item)
    return kept


def analog_candidates_to_json(group: list[ScoredCandidate]) -> str:
    """Кандидаты группы для `analog_scorer`: те же поля, что у `candidates_to_json`, плюс страна.

    Отдельный сериализатор, а не правка общего: общий уходит в промпты «Науки», и
    лишнее поле сдвинуло бы их кэшируемый префикс. Страна `null`, когда адаптер её
    не знает, — модель не должна считать пустоту отказом назвать страну сама.
    """
    return json.dumps(
        [
            {
                "url": str(item.candidate.url),
                "title": item.candidate.title,
                "org": item.candidate.org,
                "year": item.candidate.year,
                "source_type": item.candidate.source_type.value,
                "provider": item.candidate.provider,
                "country": item.candidate.country,
                "snippet": item.candidate.snippet[:_SNIPPET_LIMIT],
            }
            for item in group
        ],
        ensure_ascii=False,
        indent=2,
    )


async def author_analog_card(router: LLMRouter, brief: Brief, group: list[ScoredCandidate]) -> AnalogCard | None:
    """Собрать карточку аналога из группы кандидатов; `None` — публиковать нельзя.

    Ссылки группы (`candidate_urls`) проставляет код, а не модель: она переписывает
    их с опечатками. Страна проверяется по структурным данным кандидатов: если у
    них она известна, а модель назвала другую (или кандидаты спорят между собой),
    карточка не публикуется — подмена страны ломала бы режим «Гос». Страна
    «не установлена» от модели при отсутствии структурной тоже не допуск.
    """
    card = await router.complete_json(
        "analog_scorer",
        [
            Message(
                role="system",
                content=render_prompt(
                    "analog_scorer",
                    BRIEF_JSON=brief.model_dump_json(indent=2),
                    ANALOG_CANDIDATES_JSON=analog_candidates_to_json(group),
                ),
            ),
            Message(role="user", content=_GO),
        ],
        AnalogCard,
        # Тот же запас на рассуждающую модель, что у карточки решения.
        max_tokens=7000,
    )
    card.candidate_urls = [item.candidate.url for item in group]
    structural = {item.candidate.country for item in group if item.candidate.country}
    if len(structural) > 1:
        log.warning(
            "аналог «%s»: у кандидатов группы разные страны %s — не публикуется", card.title, sorted(structural)
        )
        return None
    if structural:
        (code,) = structural
        if card.country != code:
            log.warning(
                "аналог «%s»: модель назвала страну %s, источник даёт %s — не публикуется",
                card.title,
                card.country,
                code,
            )
            return None
    elif card.country == UNKNOWN_COUNTRY:
        log.info("аналог «%s»: страна не установлена — не публикуется", card.title)
        return None
    return card


def select_publishable_analogs(cards: list[AnalogCard], mode: str, limit: int) -> list[AnalogCard]:
    """Отобрать карточки аналогов, которые честно можно показать (инвариант №1).

    Условия: непустой `evidence`; ссылка-основание страны `country_evidence_url` —
    одна из ссылок группы (`candidate_urls`), а не выдумка модели; страна установлена
    и в режиме не `excluded`. Сноска (`footnote`) — это допуск: решает ли она
    печать в основной части или в сноске, определяют выгрузки (Э6-4).
    """
    kept: list[AnalogCard] = []
    for card in cards:
        if not card.evidence:
            continue
        if card.country == UNKNOWN_COUNTRY:
            continue
        if str(card.country_evidence_url) not in {str(url) for url in card.candidate_urls}:
            log.info("аналог «%s»: ссылка-основание страны не из источников группы — не публикуется", card.title)
            continue
        if country_status(card.country, mode) is CountryStatus.EXCLUDED:
            log.info("аналог «%s»: страна %s исключена в режиме %s — не публикуется", card.title, card.country, mode)
            continue
        kept.append(card)
    return kept[:limit]
