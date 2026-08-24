"""Наложение правок оператора поверх `report_json` (РЕШ-186, РЕШ-148).

Правки хранятся в БД отдельно от отчёта — в колонке `solution_cards.editor_overrides`
каждой карточки (`app.models.SolutionCard`). Модуль не читает базу и не знает про
SQLAlchemy: маршрут (И-11) и будущий экспорт (Э4-2) собирают словарь правок по
ключу `(is_contest, position)` сами и передают его сюда вместе с `Report`,
разобранным из `report_json`. Наложение делает глубокую копию — исходный отчёт,
собранный моделью, не мутируется и остаётся тем, что реально нашёл пайплайн.
"""

from __future__ import annotations

from app.schemas import Report, SolutionCard

#: Единственные поля карточки, которые правит оператор (РЕШ-148, развилка В4/В5).
#: Всё остальное — `confidence`, `requirement_checks`, `unsourced_claims` и
#: любые другие поля — наложение молча игнорирует: расширять список нельзя без
#: решения владельца, потому что именно эти поля несут вычисленную моделью
#: уверенность и сверку с ограничениями заказчика.
EDITABLE_FIELDS: tuple[str, ...] = (
    "title",
    "essence",
    "applicability",
    "next_step",
    "risks",
    "contact_leads",
    "developer_org",
    "developer_team",
)

#: Ключ правки — пара «карточка конкурсной секции?» и порядковый номер карточки
#: в своей секции (нумерация с нуля, как в `pipeline.persistence_write._card_row`).
OverrideKey = tuple[bool, int]
OverrideMap = dict[OverrideKey, dict[str, object]]


def apply_overrides(report: Report, overrides: OverrideMap) -> Report:
    """Возвращает копию `report` с наложенными правками оператора.

    `overrides` — словарь по ключу `(is_contest, position)`. Значение — плоский
    словарь: текстовые поля из `EDITABLE_FIELDS` заменяются целиком, служебный
    ключ `evidence_removed` (список URL) убирает соответствующие строки из
    `evidence` карточки. Добавить новую строку `evidence` этим способом
    невозможно — в коде для этого нет пути. Пустой `overrides` возвращает
    отчёт, идентичный исходному; сам `report` при этом не изменяется.
    """
    result = report.model_copy(deep=True)
    result.cards = [
        _apply_to_card(card, overrides.get((False, position))) for position, card in enumerate(result.cards)
    ]
    result.contest_cards = [
        _apply_to_card(card, overrides.get((True, position))) for position, card in enumerate(result.contest_cards)
    ]
    return result


def _apply_to_card(card: SolutionCard, card_overrides: dict[str, object] | None) -> SolutionCard:
    """Накладывает правки одной карточки; без правок отдаёт карточку без изменений."""
    if not card_overrides:
        return card
    updates: dict[str, object] = {field: card_overrides[field] for field in EDITABLE_FIELDS if field in card_overrides}
    removed_urls = card_overrides.get("evidence_removed")
    if removed_urls:
        removed = {str(url) for url in removed_urls}
        updates["evidence"] = [item for item in card.evidence if str(item.url) not in removed]
    return card.model_copy(update=updates) if updates else card
