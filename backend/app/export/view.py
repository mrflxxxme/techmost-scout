"""Единственный сборщик данных для показа отчёта и обеих выгрузок (РЕШ-196 п. 8).

`build_export_view` — общий путь для страницы отчёта (Э4-И2), PDF (Э4-И4),
DOCX (Э4-И5) и команды `scout export` (Э4-И6): читает `requests.report_json`,
накладывает правки оператора (`report_overrides.apply_overrides`), затем
правку эксперта поверх рекомендации и оговорок сводки
(`summary_overrides.apply_summary_override`, РЕШ-206) и только потом
фильтрует reject-карточки (`verdicts.visible_report`, РЕШ-198). Порядок
операций важен: правки накладываются ДО фильтра — иначе правленый текст ещё
не отклонённой карточки не дошёл бы до клиентского вида. Свой разбор
`report_json` или повторный вызов `apply_overrides`/`visible_report` в другом
месте — второй путь правды, которого этот этап не заводит.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Request as RequestRow
from app.models import RequestStatus
from app.models import SolutionCard as SolutionCardRow
from app.models import SummaryOverride as SummaryOverrideRow
from app.models import Verdict as VerdictRow
from app.pipeline.persistence import ReportNotFoundError, load_report
from app.report_overrides import OverrideMap, apply_overrides
from app.schemas import Report
from app.schemas import Verdict as VerdictDecision
from app.summary_overrides import SummaryOverrideData, apply_summary_override
from app.verdicts import CardKey, effective_verdicts, visible_report


@dataclass
class ExportView:
    """Всё, что нужно странице отчёта, PDF и DOCX, — одним объектом."""

    request_id: int
    report: Report
    #: id строки `solution_cards` по ключу (секция, позиция) — позиции уже
    #: пересчитаны под порядок `report.cards`/`report.contest_cards` **после**
    #: фильтра reject, поэтому `enumerate(view.report.cards)` совпадает с этим
    #: ключом без дополнительной сверки на стороне шаблона или экспортёра.
    card_ids: dict[CardKey, int]
    is_draft: bool
    #: Имя эксперта из действующего вердикта (не `settings.reviewer_name` —
    #: та настройка только предзаполняет форму вердикта, РЕШ-197). Пустая
    #: строка, пока вердиктов нет вовсе.
    reviewer_name: str
    validated_at: datetime | None
    #: Ключи (тем же соглашением, что и `card_ids`, — уже после фильтра)
    #: карточек с действующим вердиктом rework. Пометка «на доработку» видна
    #: только оператору на странице отчёта (РЕШ-198) — в выгрузки не попадает.
    rework_keys: set[CardKey] = field(default_factory=set)
    #: Число карточек в отчёте ДО фильтра reject, обе секции вместе (РЕШ-206) —
    #: контракт для `recommendation_printable`/`caveats_printable` и для
    #: задачи Э4-П3: сравнение с числом карточек, которые реально печатаются,
    #: показывает, скрыла ли выгрузка хоть одну карточку.
    total_cards: int = 0
    #: Эксперт правил рекомендацию сводки (РЕШ-206, РЕШ-211) — иначе печатается
    #: свободный текст модели, который мог адресоваться к карточкам по
    #: нумерации, ушедшей вместе с отклонённым вариантом. Пополевой флаг, а не
    #: общий `summary_text_edited` (задача Э4-П7 сделала поля независимыми,
    #: БЛОК-5 финального аудита нашёл живой маршрут: эксперт правит только
    #: оговорки — рекомендация модели с чужой нумерацией всё равно уезжает
    #: клиенту, потому что общий флаг «правлено хоть что-то» считал её
    #: печатной). `recommendation_edited` и `caveats_edited` выводятся из
    #: `SummaryOverrideData` в одном месте — `build_export_view` — второй
    #: точки правды не заводится.
    recommendation_edited: bool = False
    #: Эксперт правил прозу оговорок сводки — независимо от
    #: `recommendation_edited` (см. докстроку выше). Печать/непечать текста
    #: оговорок решает только этот флаг: правка рекомендации не делает
    #: печатной непровленную прозу оговорок, и наоборот.
    caveats_edited: bool = False
    #: `verdicts.visible_report` спрятал таблицу сравнения целиком (РЕШ-212):
    #: исходные строки сводки были, но после фильтра reject их число не
    #: совпало с числом карточек — сопоставить строку с конкретной карточкой
    #: нечем (см. `verdicts._visible_summary`). Пустая сводка модели без
    #: единой строки с самого начала этот флаг не поднимает: там скрывать
    #: нечего, а не «нечем сопоставить». Флаг решает, какой текст заглушки
    #: печатать вместо рекомендации и печатать ли на месте таблицы честную
    #: строку — второй раз логику `_visible_summary` в экспортёрах не считают.
    comparison_table_hidden: bool = False


@asynccontextmanager
async def _session_scope(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Оборачивает уже открытую сессию запроса под форму `SessionFactory` `load_report`."""
    yield session


async def build_export_view(session: AsyncSession, request_id: int) -> ExportView:
    """Собрать `ExportView` запроса: report_json → правки → фильтр reject.

    `ReportNotFoundError` (нет строки запроса, нет `report_json` или он не
    читается схемой) пробрасывается наружу как есть — вызывающий (страница
    отчёта, команда выгрузки) решает, как отказать читателю.
    """
    request_row = (await session.execute(select(RequestRow).where(RequestRow.id == request_id))).scalars().one_or_none()
    if request_row is None:
        raise ReportNotFoundError(f"запрос №{request_id} не найден")

    raw_report = await load_report(lambda: _session_scope(session), request_id)

    card_rows = (
        (await session.execute(select(SolutionCardRow).where(SolutionCardRow.request_id == request_id))).scalars().all()
    )
    overrides: OverrideMap = {}
    original_ids: dict[CardKey, int] = {}
    for card_row in card_rows:
        key = (card_row.is_contest, card_row.position)
        overrides[key] = dict(card_row.editor_overrides or {})
        original_ids[key] = card_row.id

    edited_report = apply_overrides(raw_report, overrides)

    summary_override_row = (
        (await session.execute(select(SummaryOverrideRow).where(SummaryOverrideRow.request_id == request_id)))
        .scalars()
        .one_or_none()
    )
    summary_override = (
        SummaryOverrideData(recommendation=summary_override_row.recommendation, caveats=summary_override_row.caveats)
        if summary_override_row is not None
        else None
    )
    edited_report = apply_summary_override(edited_report, summary_override)
    # Пополевой признак правки — читается прямо из `SummaryOverrideData`, а не
    # из общего `is_summary_edited` (РЕШ-211): `override is None` считается
    # «не правил» для обоих полей теми же условиями, что и `apply_summary_override`.
    recommendation_edited = summary_override is not None and summary_override.recommendation is not None
    caveats_edited = summary_override is not None and summary_override.caveats is not None
    total_cards = len(edited_report.cards) + len(edited_report.contest_cards)

    verdict_rows: list[VerdictRow] = []
    if original_ids:
        found = await session.execute(select(VerdictRow).where(VerdictRow.card_id.in_(original_ids.values())))
        verdict_rows = found.scalars().all()
    effective = effective_verdicts(verdict_rows)

    visible = visible_report(edited_report, original_ids, effective)
    card_ids, rework_keys = _reindex_after_filter(original_ids, effective)
    comparison_table_hidden = _comparison_table_hidden(edited_report, card_ids)

    latest = max(effective.values(), key=lambda row: (row.created_at, row.id), default=None)

    return ExportView(
        request_id=request_id,
        report=visible,
        card_ids=card_ids,
        is_draft=request_row.status not in (RequestStatus.VALIDATED, RequestStatus.EXPORTED),
        reviewer_name=latest.reviewer_name if latest is not None else "",
        validated_at=latest.created_at if latest is not None else None,
        rework_keys=rework_keys,
        total_cards=total_cards,
        recommendation_edited=recommendation_edited,
        caveats_edited=caveats_edited,
        comparison_table_hidden=comparison_table_hidden,
    )


def _comparison_table_hidden(edited_report: Report, card_ids: dict[CardKey, int]) -> bool:
    """True, когда `verdicts.visible_report` спрятал таблицу сравнения целиком (РЕШ-212).

    Повторяет то же условие, каким `verdicts._visible_summary` решает «не
    печатать таблицу вовсе» — но без второго похода в приватную функцию
    `verdicts`: те же числа уже посчитаны здесь. `edited_report` — отчёт ДО
    фильтра reject (тот же вход, что видела `visible_report`), `card_ids` —
    уже пересчитанные под порядок видимых карточек ключи (после фильтра).

    Условие «спрятана» — три штуки разом: 1) исходные строки были
    (`comparison_rows` непуст — пустая сводка `fallback_summary` или отчёт без
    сравнения не получает пометки, там скрывать нечего с самого начала); 2)
    хотя бы одна карточка основной секции реально отклонена (иначе таблица
    и не трогалась); 3) число исходных строк не совпало с числом карточек до
    фильтра — только это несовпадение и не даёт построчно сопоставить строку
    с карточкой (`_visible_summary` в противном случае просто отфильтровывает
    строки по тем же позициям, что и карточки, ничего не пряча).
    """
    original_rows = edited_report.summary.comparison_rows
    if not original_rows:
        return False
    original_main_cards = len(edited_report.cards)
    visible_main_cards = sum(1 for is_contest, _position in card_ids if not is_contest)
    if visible_main_cards == original_main_cards:
        return False  # ни одна карточка не отклонена — скрывать нечего
    return len(original_rows) != original_main_cards


def recommendation_printable(view: ExportView, printed_cards: int) -> bool:
    """Можно ли печатать рекомендацию сводки в клиентской выгрузке (РЕШ-211).

    Рекомендацию пишет модель свободной прозой со ссылками на карточки по
    номеру («Первым берём вариант 1»). Если хотя бы одна карточка скрыта из
    клиентского вида (reject или пустой `evidence`), нумерация карточек в
    выгрузке отличается от той, что видела модель при сборке сводки, —
    печатать такой текст клиенту нельзя (РЕШ-206). Печатать можно в двух
    случаях: эксперт правил рекомендацию сам (`view.recommendation_edited` —
    тогда нумерацию расставил человек заново) — либо ни одна карточка не
    скрыта (`printed_cards == view.total_cards`).

    Читает только `view.recommendation_edited` — не `view.caveats_edited` и
    не единый флаг «правлено хоть что-то» (БЛОК-5 финального аудита: общий
    флаг печатал непровленную рекомендацию, потому что эксперт правил только
    оговорки). Вызывающему негде перепутать поле — второго параметра, куда
    можно подставить чужой флаг, у функции нет.
    """
    return view.recommendation_edited or printed_cards == view.total_cards


def caveats_printable(view: ExportView, printed_cards: int) -> bool:
    """Можно ли печатать свободную прозу оговорок сводки (РЕШ-211).

    Симметрична `recommendation_printable`, но решает независимо: читает
    `view.caveats_edited`, а не `view.recommendation_edited`. Машинные
    пометки (РЕШ-210 — платный предел поиска, отказ интейка/рефайна/сводки)
    в это решение не входят вовсе: они печатаются всегда, отдельным путём
    (`app.export.markdown.limitations_lines` с `include_caveats=…`, где этот
    флаг решает судьбу только свободной прозы, не машинных строк).
    """
    return view.caveats_edited or printed_cards == view.total_cards


def _reindex_after_filter(
    original_ids: dict[CardKey, int], effective: dict[int, VerdictRow]
) -> tuple[dict[CardKey, int], set[CardKey]]:
    """Пересчитать (секция, позиция) под порядок, который отдаёт `visible_report`.

    `visible_report` убирает reject-карточки, сохраняя относительный порядок
    остальных, — эта функция не принимает отдельного решения о видимости
    карточки, а лишь повторяет тот же обход (та же проверка на `REJECT`, что
    и внутри `verdicts._visible_section`), чтобы id и пометка «на доработку»
    совпадали с позициями уже отфильтрованного отчёта.
    """
    card_ids: dict[CardKey, int] = {}
    rework_keys: set[CardKey] = set()
    for is_contest in (False, True):
        ordered_keys = sorted(key for key in original_ids if key[0] == is_contest)
        new_position = 0
        for key in ordered_keys:
            card_id = original_ids[key]
            verdict = effective.get(card_id)
            if verdict is not None and verdict.decision == VerdictDecision.REJECT:
                continue
            new_key = (is_contest, new_position)
            card_ids[new_key] = card_id
            if verdict is not None and verdict.decision == VerdictDecision.REWORK:
                rework_keys.add(new_key)
            new_position += 1
    return card_ids, rework_keys
