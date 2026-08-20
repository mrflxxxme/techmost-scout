"""Отбор кандидатов и карточек: топ по взвешенной сумме, группировка,
отсев неполных заготовок перед публикацией.

Вынесено из `stages.py` (РЕШ-94) без изменения поведения — только перенос
тела функций и нужных импортов.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from app.config import get_settings
from app.pipeline.scoring import _has_neutral_score, log_relevance_calibration
from app.schemas import ScoredCandidate, SolutionCard

log = logging.getLogger(__name__)


def select_top(scored: list[ScoredCandidate], *, limit: int, relevance_min: int | None = None) -> list[ScoredCandidate]:
    """Отобрать лучших, но не давать одному источнику занять всю выдачу.

    Перед отбором работает отсечка по предметному соответствию (РЕШ-48):
    кандидат с `relevance` ниже порога до карточек не доходит. База сравнения
    показала, почему это нужно: до половины карточек уходили «мимо задачи»,
    потому что взвешенная сумма вытягивала кандидата с низкой релевантностью за
    счёт свежести и доступности.

    Порог берётся из настроек (`RELEVANCE_MIN`), по умолчанию 0 — отсечка
    выключена, пока значение не подобрано на прогоне. Явный `relevance_min`
    нужен тестам и разбору.

    Кандидатов с нейтральной оценкой отсечка не трогает никогда: их оценки
    проставил код после отказа батча скоринга, и один такой отказ иначе тихо
    выкосил бы десяток кандидатов, а причина была бы неотличима от «все
    нерелевантны».
    """
    if relevance_min is None:
        relevance_min = get_settings().relevance_min
    eligible = [item for item in scored if _has_neutral_score(item) or item.relevance >= relevance_min]
    picked = _pick_top(eligible, limit=limit)
    # Строка считается по готовому отбору — иначе её вторая половина (сколько
    # отсеклось бы среди тех, кто доходит до карточек) неизвестна, — но уходит в
    # журнал до того, как отбор увидит следующая стадия.
    log_relevance_calibration(scored, picked, relevance_min=relevance_min, limit=limit)
    return picked


def _pick_top(eligible: list[ScoredCandidate], *, limit: int) -> list[ScoredCandidate]:
    """Отбор по взвешенной сумме с потолком на один источник.

    Вынесен отдельно, чтобы журнал калибровки считал колонку по тому же составу,
    который стадия вернёт на самом деле: две копии этой логики разошлись бы, и
    Т-10 подбирал бы порог по несуществующей выдаче.
    """
    ordered = sorted(eligible, key=lambda s: s.total, reverse=True)
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


# ── 5а. Отбор карточек к публикации ─────────────────────────────────────────
#: Верхняя граница выдачи (§2а SPEC, FR-9: 5–7 карточек). Нижней границы нет:
#: сколько честных карточек получилось, столько и идёт в отчёт (Ф-7).
MAX_PUBLISHED_CARDS = 7

_EMPTY_CARD_TEXT = "не найдено"


def select_publishable_cards(cards: list[SolutionCard], *, limit: int = MAX_PUBLISHED_CARDS) -> list[SolutionCard]:
    """Отобрать карточки, которые честно можно показать клиенту.

    Карточка без единого подтверждения (`evidence` пуст) до отчёта не доходит
    — тот же инвариант №1, что и в экспортёре, только на входе, а не на
    печати. Карточка-заглушка «не найдено» (формат ошибки 1 из
    `prompts/card_author.md` — группа без источников) тоже не идёт: пустая
    карточка дороже честного «карточек получилось меньше, чем просили».
    Порядок сохраняется, число карточек в отчёте искусственно не добивается.
    """
    kept = [card for card in cards if card.evidence and not _is_empty_card(card)]
    return kept[:limit]


def _is_empty_card(card: SolutionCard) -> bool:
    """Каркас-заглушка: и название, и суть — «не найдено»."""
    return card.title.strip().lower() == _EMPTY_CARD_TEXT and card.essence.strip().lower() == _EMPTY_CARD_TEXT
