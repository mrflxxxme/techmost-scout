"""Слой вердиктов эксперта — единственная реализация фильтра видимости (РЕШ-198, РЕШ-213).

Чистые функции над уже прочитанными строками ORM `app.models.Verdict`: без
сессии и без запросов к базе, поэтому проверяются на фикстурах без сети
(инвариант №7). Второго перечисления решений в проекте нет — сравнение везде
идёт с `schemas.Verdict`, а не со строковым литералом.

`_kept_positions` — единственное место, где решается, видна ли карточка
(РЕШ-213): признак один на весь проект — «отклонена эксперт (reject)» и,
опционально флагом `require_evidence`, «нет ни одного подтверждения». Флаг
разводит два разных потребителя одной функции: операторская страница отчёта
(`web/routes/report.py`) видит бессылочные карточки с красной плашкой и зовёт
`visible_report` без флага (только reject, поведение не меняется, РЕШ-204/205);
клиентский экспорт (`export/pdf.py`, `export/docx.py`, через
`export/view.build_export_view`) зовёт ту же функцию с `require_evidence=True`
и получает карточки и строки таблицы сравнения, отфильтрованные одним и тем же
проходом. Второго расчёта видимости (свой фильтр по `evidence` в `pdf.py`/
`docx.py`, как было до РЕШ-213) в проекте больше нет.
"""

from __future__ import annotations

from datetime import datetime

from app.export.markdown import REVIEWED_FOOTER
from app.models import Verdict as VerdictRow
from app.schemas import Report, ReportSummary, SolutionCard
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
    *,
    require_evidence: bool = False,
) -> Report:
    """Отчёт для показа: карточки с действующим вердиктом reject убраны.

    Убирает reject-карточки из обеих секций (`cards` и `contest_cards`,
    РЕШ-198), порядок остальных карточек сохраняет и выдачу не добивает — если
    после фильтра карточек стало меньше лимита, это не повод искать ещё.
    Вердикт `rework` карточку не убирает: пометка «на доработку» видна только
    оператору на странице отчёта, в клиентский вид и выгрузки не попадает.
    Карточка без ключа в `card_ids` (вердиктов ещё не было) остаётся видимой.

    `require_evidence=False` (по умолчанию) — режим операторской страницы
    отчёта (РЕШ-204/205): бессылочная карточка остаётся видимой с красной
    плашкой «нет подтверждений», убирается только reject. `require_evidence=
    True` — режим клиентского экспорта (РЕШ-213): карточка убирается ещё и
    тогда, когда её `evidence` пуст, тем же проходом и тем же признаком
    видимости, каким убираются карточки, — второго фильтра по `evidence` в
    `export/pdf.py`/`export/docx.py` эта функция не оставляет.

    Заодно фильтрует `summary.comparison_rows` (РЕШ-206): промпт `summary.md`
    п. 2 гарантирует построчное соответствие таблицы сравнения основной секции
    карточек, поэтому строку убираем тем же решением, что убрало саму
    карточку — по позиции в исходном (до фильтра) списке `report.cards`. Если
    построчное соответствие не сходится (число строк не равно числу карточек
    до фильтра) и хотя бы одна карточка скрыта, таблица не печатается вовсе
    (Э4-П8, повторный аудит, правка П-7) — см. `_visible_summary`.
    """
    result = report.model_copy(deep=True)
    result.cards = _visible_section(
        report.cards, card_ids, effective, is_contest=False, require_evidence=require_evidence
    )
    result.contest_cards = _visible_section(
        report.contest_cards, card_ids, effective, is_contest=True, require_evidence=require_evidence
    )
    result.summary = _visible_summary(
        report.summary, report.cards, card_ids, effective, require_evidence=require_evidence
    )
    return result


def _kept_positions(
    cards: list[SolutionCard],
    card_ids: dict[CardKey, int],
    effective: dict[int, VerdictRow],
    *,
    is_contest: bool,
    require_evidence: bool = False,
) -> list[int]:
    """Позиции карточек секции в исходном порядке, которые остаются видимыми.

    Единственное место, где решается видимость карточки (РЕШ-213): reject
    убирает всегда, пустой `evidence` — только при `require_evidence=True`.
    """
    kept: list[int] = []
    for position, card in enumerate(cards):
        card_id = card_ids.get((is_contest, position))
        verdict = effective.get(card_id) if card_id is not None else None
        if verdict is not None and verdict.decision == VerdictDecision.REJECT:
            continue
        if require_evidence and not card.evidence:
            continue
        kept.append(position)
    return kept


def _visible_section(
    cards: list[SolutionCard],
    card_ids: dict[CardKey, int],
    effective: dict[int, VerdictRow],
    *,
    is_contest: bool,
    require_evidence: bool = False,
) -> list[SolutionCard]:
    """Одна секция отчёта без карточек, скрытых `_kept_positions`."""
    kept = _kept_positions(cards, card_ids, effective, is_contest=is_contest, require_evidence=require_evidence)
    return [cards[position] for position in kept]


def _visible_summary(
    summary: ReportSummary,
    main_cards: list[SolutionCard],
    card_ids: dict[CardKey, int],
    effective: dict[int, VerdictRow],
    *,
    require_evidence: bool = False,
) -> ReportSummary:
    """Строки сравнения без карточек основной секции, скрытых `_kept_positions`.

    Соответствие построчное и берётся из **исходного** (до фильтра) списка
    `main_cards` — считать позицию заново по уже отфильтрованному списку
    карточек нельзя: это сдвинуло бы номера и убрало не ту строку. Конкурсные
    карточки (`contest_cards`) на таблицу сравнения не влияют — стадия
    `summarize` в `pipeline/summary.py` строит её только по основной секции.

    Если число строк не совпадает с числом карточек до фильтра (запасная
    сводка `fallback_summary`, старый отчёт другой формы), сопоставить строку
    с конкретной карточкой нечем. Пока среди карточек нет ни одной скрытой,
    это не страшно — таблица остаётся как есть, скрывать нечего (критерий 2
    Э4-П8). Но если скрытая карточка есть (reject или, при `require_evidence=
    True`, пустой `evidence`), единственное безопасное решение — не печатать
    таблицу вовсе: при несовпадении числа строк с числом карточек напечатать
    «отфильтрованную» версию означает гадать, какая строка чья, а
    неотфильтрованная версия рискует показать клиенту строку уже скрытой
    карточки — ровно тот случай, который нашёл повторный аудит (правка П-7,
    и БЛОК-6 для случая пустого `evidence`): строка сравнения печатала
    подтверждение карточки, у которой подтверждений нет.
    """
    rows = summary.comparison_rows
    if not rows:
        return summary
    kept = _kept_positions(main_cards, card_ids, effective, is_contest=False, require_evidence=require_evidence)
    if len(kept) == len(main_cards):
        return summary  # скрытых карточек нет — скрывать нечего
    if len(rows) != len(main_cards):
        return summary.model_copy(update={"comparison_rows": []})
    filtered_rows = [rows[position] for position in kept]
    return summary.model_copy(update={"comparison_rows": filtered_rows})


def visible_position_count(
    cards: list[SolutionCard],
    card_ids: dict[CardKey, int],
    effective: dict[int, VerdictRow],
    *,
    is_contest: bool,
    require_evidence: bool = False,
) -> int:
    """Число карточек секции, которые останутся видимы после фильтра `_kept_positions`.

    Тонкая обёртка для `export.view._comparison_table_hidden` (РЕШ-212,
    РЕШ-213): та функция раньше считала видимые карточки по уже
    пересчитанному `card_ids` (только reject), из-за чего скрытие карточки по
    пустому `evidence` не учитывалось при решении, спрятана ли таблица
    сравнения целиком. Здесь тот же проход, что и у `visible_report`, — второй
    независимый расчёт видимости этим не заводится.
    """
    return len(_kept_positions(cards, card_ids, effective, is_contest=is_contest, require_evidence=require_evidence))


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
