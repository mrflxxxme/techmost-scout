"""Слой вердиктов эксперта — единственная реализация фильтра «reject» (РЕШ-198).

Чистые функции над уже прочитанными строками ORM `app.models.Verdict`: без
сессии и без запросов к базе, поэтому проверяются на фикстурах без сети
(инвариант №7). Второго перечисления решений в проекте нет — сравнение везде
идёт с `schemas.Verdict`, а не со строковым литералом.
"""

from __future__ import annotations

from datetime import datetime

from app.export.markdown import REVIEWED_FOOTER
from app.models import Verdict as VerdictRow
from app.schemas import Report, SolutionCard
from app.schemas import Verdict as VerdictDecision

#: Ключ карточки в секции отчёта: «конкурсная секция?» и порядковый номер в
#: своей секции — тот же ключ, каким `report_overrides.OverrideKey` находит
#: правки оператора (РЕШ-186). Значение — идентификатор строки `solution_cards`
#: в базе, по нему ищется действующий вердикт.
CardKey = tuple[bool, int]


def effective_verdicts(rows: list[VerdictRow]) -> dict[int, VerdictRow]:
    """Действующий вердикт каждой карточки — последняя строка по `created_at`.

    Вердикты не обновляются и не удаляются (РЕШ-196 п. 3): накопленные строки —
    обучающая выборка. Действующим считается последний по времени; при
    совпадении `created_at` (вставки одной транзакции получают одинаковую
    метку с точностью до микросекунды) тай-брейк — по большему `id`, иначе
    «действующий» определялся бы порядком выборки, а не решением эксперта.
    """
    result: dict[int, VerdictRow] = {}
    for row in rows:
        current = result.get(row.card_id)
        if current is None or (row.created_at, row.id) >= (current.created_at, current.id):
            result[row.card_id] = row
    return result


def visible_report(
    report: Report,
    card_ids: dict[CardKey, int],
    effective: dict[int, VerdictRow],
) -> Report:
    """Отчёт для клиента и выгрузок: карточки с действующим вердиктом reject убраны.

    Убирает reject-карточки из обеих секций (`cards` и `contest_cards`,
    РЕШ-198), порядок остальных карточек сохраняет и выдачу не добивает — если
    после фильтра карточек стало меньше лимита, это не повод искать ещё.
    Вердикт `rework` карточку не убирает: пометка «на доработку» видна только
    оператору на странице отчёта, в клиентский вид и выгрузки не попадает.
    Карточка без ключа в `card_ids` (вердиктов ещё не было) остаётся видимой.
    """
    result = report.model_copy(deep=True)
    result.cards = _visible_section(result.cards, card_ids, effective, is_contest=False)
    result.contest_cards = _visible_section(result.contest_cards, card_ids, effective, is_contest=True)
    return result


def _visible_section(
    cards: list[SolutionCard],
    card_ids: dict[CardKey, int],
    effective: dict[int, VerdictRow],
    *,
    is_contest: bool,
) -> list[SolutionCard]:
    """Одна секция отчёта без карточек с действующим вердиктом reject."""
    visible: list[SolutionCard] = []
    for position, card in enumerate(cards):
        card_id = card_ids.get((is_contest, position))
        verdict = effective.get(card_id) if card_id is not None else None
        if verdict is not None and verdict.decision == VerdictDecision.REJECT:
            continue
        visible.append(card)
    return visible


def all_cards_reviewed(card_ids: dict[CardKey, int], effective: dict[int, VerdictRow]) -> bool:
    """True, когда у каждой карточки запроса есть хотя бы один вердикт.

    Считается по строкам `solution_cards` обеих секций (РЕШ-196 п. 4): пока
    хоть у одной вердикта нет, статус запроса до `validated` не дорастает.
    Запрос без карточек считается непроверенным, а не тривиально пройденным.
    """
    if not card_ids:
        return False
    return all(card_id in effective for card_id in card_ids.values())


def signature_line(reviewer_name: str, when: datetime) -> str:
    """Строка подписи эксперта — единственная точка сборки текста РЕШ-197.

    Формат берётся из `REVIEWED_FOOTER` (`app.export.markdown`) — тем же
    способом, каким его использует markdown-экспорт, чтобы страница отчёта,
    PDF и DOCX печатали дословно одну и ту же строку.
    """
    return REVIEWED_FOOTER.format(name=reviewer_name, date=f"{when:%d.%m.%Y}")
