"""Стадия 5 — карточки: сборка из группы кандидатов и проверка утверждений.

Вынесено из `stages.py` (РЕШ-94) без изменения поведения — только перенос
тела функций и нужных импортов.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from pydantic import BaseModel, field_validator

from app.llm import LLMRouter, Message
from app.pipeline.constraints_guard import ensure_requirement_checks
from app.pipeline.prompting import _GO, candidates_to_json, load_gov_support, render_prompt
from app.schemas import Brief, Candidate, Confidence, MaturityStage, ScoredCandidate, SolutionCard, SourceType

log = logging.getLogger(__name__)


class VerifyResult(BaseModel):
    """Ответ проверяющей стадии по одной карточке."""

    needs_rework: bool = False
    confidence: Confidence = Confidence.MEDIUM
    confidence_note: str = ""
    unsourced_claims: list[str] = []
    notes: str = ""

    @field_validator("notes", "confidence_note", mode="before")
    @classmethod
    def _join_lists(cls, value: object) -> object:
        """Дешёвые модели любят возвращать заметки списком — склеиваем в строку."""
        if isinstance(value, list):
            return "; ".join(str(item) for item in value)
        return value


# ── 5. Карточки и проверка ──────────────────────────────────────────────────
async def author_card(router: LLMRouter, brief: Brief, group: list[ScoredCandidate]) -> SolutionCard:
    """Собрать карточку из группы кандидатов."""
    card = await router.complete_json(
        "card_author",
        [
            # Всё содержательное — в системном сообщении: так DeepSeek кэширует
            # префикс и вход дешевеет в десятки раз.
            Message(
                role="system",
                content=render_prompt(
                    "card_author",
                    BRIEF_JSON=brief.model_dump_json(indent=2),
                    GROUP_CANDIDATES_JSON=candidates_to_json(group),
                    GOV_SUPPORT_JSON=load_gov_support(),
                ),
            ),
            Message(role="user", content=_GO),
        ],
        SolutionCard,
        # Запас на рассуждающую модель: она сначала тратит токены на
        # reasoning_content и лишь потом пишет сам JSON карточки.
        max_tokens=7000,
    )
    # Ссылки на исходных кандидатов заполняем кодом, а не моделью: она их
    # переписывает с опечатками, а проверяющая стадия потом справедливо
    # жалуется, что кандидаты не переданы.
    card.candidate_urls = [item.candidate.url for item in group]
    # Стадию зрелости конкурсной карточки правит код, а не модель: описание
    # хакатонного проекта пишет сама команда, и оно всегда звучит как готовый
    # продукт (см. cap_contest_maturity).
    card = cap_contest_maturity(card, group)
    return ensure_requirement_checks(card, brief)


#: Порядок стадий зрелости по возрастанию. `unknown` в него не входит: это не
#: стадия, а честное «ни один признак источниками не подтверждён», и понижать
#: его потолком некуда.
_MATURITY_ORDER: tuple[MaturityStage, ...] = (
    MaturityStage.LAB,
    MaturityStage.PROTOTYPE,
    MaturityStage.PILOT,
    MaturityStage.SERIES,
)

#: Площадки конкурсов с многолетним циклом (§2.2 разведки `contests-recon.md`):
#: только у XPRIZE и Up Great решения проходят натурные испытания по регламенту
#: организатора с независимым замером — поэтому их потолок `pilot`, а не
#: `prototype`. Опознаём по УЗЛУ адреса кандидата: домен нельзя случайно
#: получить из чужой аннотации, в отличие от слова в тексте.
LONG_CYCLE_CONTEST_DOMAINS: tuple[str, ...] = (
    "xprize.org",
    "upgreat.one",
)

#: Имена тех же конкурсов — на случай, когда решение лежит не на сайте
#: организатора (новость вуза, страница команды). Ищутся ТОЛЬКО в названии и
#: организации кандидата, но не в `snippet`: сниппет — произвольный текст чужой
#: поисковой выдачи.
#:
#: Здесь не осталось ни «многолетн», ни «национальная технологическая
#: инициатива», и обе метки убраны по одной причине — они мерили не то. Первая
#: поднимала потолок обычному хакатону на любом «многолетнем опыте команды» в
#: аннотации, то есть давала ровно то завышение, ради запрета которого потолок и
#: писался (РЕШ-79 п. 2: хакатон — не выше прототипа). Вторая в живом тексте
#: почти не срабатывала: «при поддержке Национальной технологической
#: инициативы» стоит в родительном падеже и с подстрокой не совпадает.
LONG_CYCLE_CONTEST_NAMES: tuple[str, ...] = (
    "xprize",
    "upgreat",
    "up great",
    "up-great",
    "ап грейт",
    "апгрейт",
)


def contest_maturity_ceiling(group: Sequence[ScoredCandidate]) -> MaturityStage | None:
    """Потолок зрелости, заданный конкурсными кандидатами группы.

    `None` — конкурсных кандидатов в группе нет, и потолка тоже нет: обычная
    карточка по публикациям и патентам этим правилом не трогается.
    """
    contests = [item.candidate for item in group if item.candidate.source_type is SourceType.CONTEST]
    if not contests:
        return None
    if any(_is_long_cycle_contest(candidate) for candidate in contests):
        return MaturityStage.PILOT
    return MaturityStage.PROTOTYPE


def _is_long_cycle_contest(candidate: Candidate) -> bool:
    """Конкурс ли это с многолетним циклом — по узлу адреса и по имени конкурса.

    Свободный текст выдачи (`snippet`) не смотрится вовсе: там «многолетний
    опыт команды» встречается сплошь и рядом, и слово в аннотации поднимало бы
    потолок обычному хакатону. Домен сверяется по узлу целиком, а не подстрокой:
    иначе `xprize.org.example.com` выдавал бы себя за площадку XPRIZE, и признак
    «домен» оказался бы не надёжнее слова в тексте.
    """
    host = (candidate.url.host or "").lower()
    if any(host == domain or host.endswith(f".{domain}") for domain in LONG_CYCLE_CONTEST_DOMAINS):
        return True
    named = f"{candidate.title} {candidate.org or ''}".lower()
    return any(name in named for name in LONG_CYCLE_CONTEST_NAMES)


def cap_contest_maturity(card: SolutionCard, group: Sequence[ScoredCandidate]) -> SolutionCard:
    """Понизить стадию зрелости карточки до потолка, заданного типом конкурса.

    Правило владельца (РЕШ-79 п. 2) и вывод разведки §2.2: `maturity_stage`
    решения конкурсного класса **никогда не выставляется моделью по тексту
    описания**. Описание пишет сама команда, и оно звучит как готовый продукт
    («система предиктивной диагностики для промышленных предприятий»), — модель,
    читая такой текст, ставит `pilot` за 48 часов работы. Поэтому стадию правит
    код по типу источника, как и `pdf_urls`: хакатон — не выше `prototype`,
    конкурс с многолетним циклом — не выше `pilot`.

    Понижение одностороннее: стадию ниже потолка функция не поднимает, а
    `unknown` не трогает вовсе — это не завышение, а отсутствие подтверждения.
    """
    ceiling = contest_maturity_ceiling(group)
    if ceiling is None or card.maturity_stage not in _MATURITY_ORDER:
        return card
    if _MATURITY_ORDER.index(card.maturity_stage) > _MATURITY_ORDER.index(ceiling):
        log.info(
            "конкурсная карточка «%s»: стадия %s понижена до потолка %s (РЕШ-79 п. 2)",
            card.title,
            card.maturity_stage.value,
            ceiling.value,
        )
        card.maturity_stage = ceiling
    return card


#: Формат записи о неподтверждённой зрелости из правила 7 prompts/verifier.md:
#: «стадия зрелости не подтверждена: <стадия>». По этой записи (а не по
#: пересказу) код понижает maturity_stage карточки — сама проверяющая стадия
#: карточку не переписывает (РЕШ-42).
_MATURITY_UNCONFIRMED_MARKER = "стадия зрелости не подтверждена"


async def verify_card(
    router: LLMRouter, card: SolutionCard, group: list[ScoredCandidate] | None = None
) -> SolutionCard:
    """Проверить карточку на утверждения без источника и уточнить уверенность.

    Кандидаты передавать обязательно: без текстов источников проверяющей стадии
    нечем сверять утверждения, и она справедливо занижает уверенность всем
    карточкам подряд.
    """
    verdict = await router.complete_json(
        "verifier",
        [
            Message(
                role="system",
                content=render_prompt(
                    "verifier",
                    CARD_JSON=card.model_dump_json(indent=2),
                    GROUP_CANDIDATES_JSON=candidates_to_json(group or []),
                ),
            ),
            Message(role="user", content=_GO),
        ],
        VerifyResult,
        # Проверка разбирает карточку по утверждениям и сверяет их с источниками:
        # рассуждающей модели на это нужен запас, иначе content приходит пустым.
        max_tokens=6000,
    )
    card.confidence = verdict.confidence
    card.confidence_note = verdict.confidence_note
    if verdict.unsourced_claims:
        # РЕШ-42: находки проверки — это SolutionCard.unsourced_claims, а не
        # только строка в risks. Без этого контракт промпта Ф-2 остаётся
        # мёртвым: поле заполняет модель, но код его не читает.
        card.unsourced_claims.extend(verdict.unsourced_claims)
        card.confidence = Confidence.LOW
        if any(_MATURITY_UNCONFIRMED_MARKER in claim.lower() for claim in verdict.unsourced_claims):
            # Неподтверждённая зрелость понижается до unknown, а не остаётся
            # заявленной стадией (правило 7 prompts/verifier.md).
            card.maturity_stage = MaturityStage.UNKNOWN
    if card.has_unsourced_claims():
        card.confidence = Confidence.LOW
        card.confidence_note = (card.confidence_note + " Нет ни одного подтверждения ссылкой.").strip()
    return card
