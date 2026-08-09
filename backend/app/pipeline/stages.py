"""Стадии пайплайна.

Каждая стадия — чистая функция: получает данные и роутер, возвращает данные.
Никакой работы с базой внутри стадий, чтобы их можно было тестировать на
фикстурах без сети и без БД.

Порядок: интейк → план → поиск → дедуп → скоринг → группировка → карточки →
проверка → сводка.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from pydantic import BaseModel, Field, field_validator

from app.config import get_settings
from app.llm import LLMRouter, Message
from app.schemas import (
    Brief,
    Candidate,
    Confidence,
    IntakeResult,
    ReportSummary,
    ScoredCandidate,
    SearchPlan,
    SolutionCard,
    SourceType,
)

log = logging.getLogger(__name__)


class CandidateScore(BaseModel):
    """Оценка одного кандидата в ответе модели.

    Модель ссылается на кандидата номером, а не пересказывает его поля: иначе
    ответ на батч из десяти кандидатов не влезает в лимит токенов и JSON
    обрывается на середине. Сопоставление номеров с кандидатами делает код.
    """

    index: int = Field(ge=0)
    relevance: int = Field(ge=0, le=10)
    maturity: int = Field(ge=0, le=10)
    accessibility: int = Field(ge=0, le=10)
    freshness: int = Field(ge=0, le=10)
    completeness: int = Field(ge=0, le=10)
    notes: str = ""


class ScoreBatch(BaseModel):
    """Обёртка ответа скоринга: модели проще вернуть объект, чем голый массив."""

    scores: list[CandidateScore]


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


def load_prompt(name: str) -> str:
    """Прочитать промпт из prompts/. Промпты — часть контракта, а не строки в коде."""
    path = get_settings().prompts_dir / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"нет промпта {path}. См. docs/SPEC.md, раздел про prompts/")
    return path.read_text(encoding="utf-8")


def load_gov_support() -> str:
    """Справочник инструментов господдержки — подаётся модели как контекст."""
    path = get_settings().data_dir / "gov_support.yaml"
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ── 1. Интейк ───────────────────────────────────────────────────────────────
async def run_intake(router: LLMRouter, raw_text: str) -> IntakeResult:
    """Из свободного текста — уточняющие вопросы и черновик брифа."""
    return await router.complete_json(
        "intake",
        [
            Message(role="system", content=load_prompt("intake")),
            Message(role="user", content=f"Текст задачи от бизнеса:\n\n{raw_text}"),
        ],
        IntakeResult,
    )


async def refine_brief(router: LLMRouter, raw_text: str, answers: dict[str, str]) -> Brief:
    """Собрать финальный бриф с учётом ответов на уточняющие вопросы."""
    answers_text = "\n".join(f"— {q}\n  Ответ: {a or 'не знаю'}" for q, a in answers.items())
    result = await router.complete_json(
        "intake",
        [
            Message(role="system", content=load_prompt("intake")),
            Message(
                role="user",
                content=(
                    f"Текст задачи:\n\n{raw_text}\n\n"
                    f"Ответы на уточняющие вопросы:\n{answers_text}\n\n"
                    "Вопросы больше не задавай: верни questions пустым списком "
                    "и заполненный draft_brief."
                ),
            ),
        ],
        IntakeResult,
    )
    return result.draft_brief


# ── 2. План поиска ──────────────────────────────────────────────────────────
async def plan_search(router: LLMRouter, brief: Brief) -> SearchPlan:
    """Превратить бриф в набор поисковых формулировок на двух языках."""
    return await router.complete_json(
        "planner",
        [
            Message(role="system", content=load_prompt("planner")),
            Message(role="user", content=f"Бриф:\n{brief.model_dump_json(indent=2)}"),
        ],
        SearchPlan,
    )


# ── 3. Дедупликация ─────────────────────────────────────────────────────────
def deduplicate(candidates: list[Candidate]) -> list[Candidate]:
    """Склеить дубли по DOI/номеру или нормализованному названию.

    При равенстве ключа выигрывает кандидат из витрины партнёров: он проверен
    человеком и у него есть контакт разработчика.
    """
    best: dict[str, Candidate] = {}
    for candidate in candidates:
        key = candidate.dedup_key()
        current = best.get(key)
        if current is None:
            best[key] = candidate
            continue
        from_showcase = candidate.source_type is SourceType.SHOWCASE
        current_from_showcase = current.source_type is SourceType.SHOWCASE
        richer_snippet = len(candidate.snippet) > len(current.snippet)
        if (from_showcase and not current_from_showcase) or richer_snippet:
            best[key] = candidate
    removed = len(candidates) - len(best)
    if removed:
        log.info("дедупликация убрала %s дублей из %s", removed, len(candidates))
    return list(best.values())


# ── 4. Скоринг ──────────────────────────────────────────────────────────────
async def score_candidates(
    router: LLMRouter, brief: Brief, candidates: list[Candidate], *, batch_size: int = 10
) -> list[ScoredCandidate]:
    """Оценить кандидатов по пяти критериям.

    Батчами, потому что дешёвая модель на длинном списке начинает ставить всем
    одинаковые оценки и терять кандидатов.
    """

    scored: list[ScoredCandidate] = []
    for start in range(0, len(candidates), batch_size):
        chunk = candidates[start : start + batch_size]
        listing = "\n\n".join(
            f"[{i}] {c.title}\nURL: {c.url}\nОрганизация: {c.org or 'не указана'}\n"
            f"Год: {c.year or 'не указан'}\nТип: {c.source_type.value}\nФрагмент: {c.snippet[:600]}"
            for i, c in enumerate(chunk)
        )
        try:
            batch = await router.complete_json(
                "scorer",
                [
                    Message(role="system", content=load_prompt("scorer")),
                    Message(
                        role="user",
                        content=(
                            f"Задача бизнеса:\n{brief.task_summary}\n"
                            f"Отрасль: {brief.industry}\nОграничения: {'; '.join(brief.constraints) or 'не заданы'}\n\n"
                            f"Кандидаты ({len(chunk)} шт.):\n{listing}\n\n"
                            f"Верни ровно {len(chunk)} оценок, ссылаясь на кандидатов "
                            f"номерами от 0 до {len(chunk) - 1}."
                        ),
                    ),
                ],
                ScoreBatch,
                max_tokens=4000,
            )
        except Exception as exc:
            log.warning("скоринг батча не удался (%s), беру нейтральные оценки", exc)
            scored.extend(_neutral_scores(chunk))
            continue

        by_index = {item.index: item for item in batch.scores}
        for position, candidate in enumerate(chunk):
            score = by_index.get(position)
            if score is None:
                # Модель потеряла кандидата — не выбрасываем его, а ставим
                # нейтральную оценку: пусть решает отбор, а не пропуск.
                log.info("скоринг пропустил кандидата %s, ставлю нейтральную оценку", candidate.title[:60])
                scored.extend(_neutral_scores([candidate]))
                continue
            scored.append(
                ScoredCandidate(
                    candidate=candidate,
                    relevance=score.relevance,
                    maturity=score.maturity,
                    accessibility=score.accessibility,
                    freshness=score.freshness,
                    completeness=score.completeness,
                    notes=score.notes,
                )
            )
    return scored


def _neutral_scores(candidates: list[Candidate]) -> list[ScoredCandidate]:
    """Средние оценки для кандидатов, которых модель не осилила оценить."""
    return [
        ScoredCandidate(
            candidate=candidate,
            relevance=5,
            maturity=5,
            accessibility=5,
            freshness=5,
            completeness=5,
            notes="оценка не получена, значение нейтральное",
        )
        for candidate in candidates
    ]


def select_top(scored: list[ScoredCandidate], *, limit: int) -> list[ScoredCandidate]:
    """Отобрать лучших, но не давать одному источнику занять всю выдачу."""
    ordered = sorted(scored, key=lambda s: s.total, reverse=True)
    per_provider: dict[str, int] = defaultdict(int)
    picked: list[ScoredCandidate] = []
    cap = max(2, limit // 2)
    for item in ordered:
        provider = item.candidate.provider
        if per_provider[provider] >= cap:
            continue
        picked.append(item)
        per_provider[provider] += 1
        if len(picked) >= limit:
            break
    return picked


def group_for_cards(selected: list[ScoredCandidate], *, target: int) -> list[list[ScoredCandidate]]:
    """Сгруппировать кандидатов в заготовки карточек.

    Пока группировка простая: один кандидат — одна карточка, но с запасом на
    склейку по организации (несколько публикаций одной лаборатории — один
    вариант решения, а не три).
    """
    by_org: dict[str, list[ScoredCandidate]] = defaultdict(list)
    singles: list[list[ScoredCandidate]] = []
    for item in selected:
        org = (item.candidate.org or "").strip().lower()
        if org:
            by_org[org].append(item)
        else:
            singles.append([item])
    groups = [group for group in by_org.values()] + singles
    groups.sort(key=lambda g: max(i.total for i in g), reverse=True)
    return groups[:target]


# ── 5. Карточки и проверка ──────────────────────────────────────────────────
async def author_card(router: LLMRouter, brief: Brief, group: list[ScoredCandidate]) -> SolutionCard:
    """Собрать карточку из группы кандидатов."""
    sources = "\n\n".join(
        f"URL: {i.candidate.url}\nНазвание: {i.candidate.title}\n"
        f"Организация: {i.candidate.org or 'не указана'}\nГод: {i.candidate.year or 'не указан'}\n"
        f"Авторы: {', '.join(i.candidate.authors) or 'не указаны'}\nФрагмент: {i.candidate.snippet[:1500]}"
        for i in group
    )
    card = await router.complete_json(
        "card_author",
        [
            # Системный промпт и справочник — в начале сообщения: так DeepSeek
            # кэширует префикс и вход дешевеет в десятки раз.
            Message(
                role="system",
                content=load_prompt("card_author") + "\n\n# Справочник господдержки\n" + load_gov_support(),
            ),
            Message(
                role="user",
                content=f"Бриф:\n{brief.model_dump_json(indent=2)}\n\nИсточники:\n{sources}",
            ),
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
    return card


async def verify_card(router: LLMRouter, card: SolutionCard) -> SolutionCard:
    """Проверить карточку на утверждения без источника и уточнить уверенность."""
    verdict = await router.complete_json(
        "verifier",
        [
            Message(role="system", content=load_prompt("verifier")),
            Message(role="user", content=card.model_dump_json(indent=2)),
        ],
        VerifyResult,
    )
    card.confidence = verdict.confidence
    card.confidence_note = verdict.confidence_note
    if verdict.unsourced_claims:
        # Не выбрасываем карточку: помечаем и оставляем решение эксперту.
        card.risks.append("Проверка нашла утверждения без источника: " + "; ".join(verdict.unsourced_claims[:3]))
        card.confidence = Confidence.LOW
    if card.has_unsourced_claims():
        card.confidence = Confidence.LOW
        card.confidence_note = (card.confidence_note + " Нет ни одного подтверждения ссылкой.").strip()
    return card


# ── 6. Сводка ───────────────────────────────────────────────────────────────
async def summarize(router: LLMRouter, brief: Brief, cards: list[SolutionCard]) -> ReportSummary:
    """Сравнительная сводка и рекомендация по вариантам."""
    cards_text = "\n\n".join(
        f"[{i + 1}] {c.title}\nЗрелость: {c.maturity_stage.value}\nСуть: {c.essence}\n"
        f"Применимость: {c.applicability}\nРиски: {'; '.join(c.risks) or 'не выявлены'}\n"
        f"Уверенность: {c.confidence.value}"
        for i, c in enumerate(cards)
    )
    return await router.complete_json(
        "summary",
        [
            Message(role="system", content=load_prompt("summary")),
            Message(role="user", content=f"Бриф:\n{brief.model_dump_json(indent=2)}\n\nКарточки:\n{cards_text}"),
        ],
        ReportSummary,
        max_tokens=6000,
    )
