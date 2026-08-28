"""Единственный сборщик данных для показа отчёта и обеих выгрузок (РЕШ-196 п. 8).

`build_export_view` — общий путь для страницы отчёта (Э4-И2), PDF (Э4-И4),
DOCX (Э4-И5) и команды `scout export` (Э4-И6): читает `requests.report_json`,
накладывает правки оператора (`report_overrides.apply_overrides`), затем
правку эксперта поверх рекомендации и оговорок сводки
(`summary_overrides.apply_summary_override`, РЕШ-206) и только потом зовёт
`verdicts.visible_report` дважды одной и той же функцией (РЕШ-213): без флага
— для `ExportView.report`, который видит операторская страница отчёта (reject
убран, бессылочная карточка остаётся с красной плашкой, РЕШ-204/205); с
`require_evidence=True` — для `ExportView.client_report`, который видят
`export/pdf.py` и `export/docx.py` (reject и пустой `evidence` убраны одним
проходом, строки таблицы сравнения — тем же проходом, что и карточки). Второго
расчёта видимости для клиента в проекте не остаётся. Порядок операций важен:
правки накладываются ДО фильтра — иначе правленый текст ещё не отклонённой
карточки не дошёл бы до клиентского вида. Свой разбор `report_json` или
повторный вызов `apply_overrides`/`visible_report` в другом месте — второй
путь правды, которого этот этап не заводит.
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
from app.verdicts import CardKey, effective_verdicts, hidden_reason, visible_position_count, visible_report


@dataclass
class ExportView:
    """Всё, что нужно странице отчёта, PDF и DOCX, — одним объектом."""

    request_id: int
    report: Report
    #: id строки `solution_cards` по ключу (секция, позиция) — позиции уже
    #: пересчитаны под порядок `report.cards`/`report.contest_cards` **после**
    #: фильтра reject, поэтому `enumerate(view.report.cards)` совпадает с этим
    #: ключом без дополнительной сверки на стороне шаблона или экспортёра.
    #: Ключ считается по `report` (только reject) — для клиентских выгрузок
    #: (`client_report`) отдельного словаря id не заводится: ни `pdf.py`, ни
    #: `docx.py` номерами карточек из БД не пользуются, они нумеруют печатаемые
    #: карточки заново с единицы.
    card_ids: dict[CardKey, int]
    is_draft: bool
    #: Имя эксперта из действующего вердикта (не `settings.reviewer_name` —
    #: та настройка только предзаполняет форму вердикта, РЕШ-197). Пустая
    #: строка, пока вердиктов нет вовсе.
    reviewer_name: str
    validated_at: datetime | None
    #: Отчёт для клиентских выгрузок (РЕШ-213): тот же `verdicts.visible_report`,
    #: что собрал `report`, но с `require_evidence=True` — карточка без единого
    #: подтверждения убрана из `cards`/`contest_cards`, а её строка убрана из
    #: `summary.comparison_rows` тем же проходом. `export/pdf.py` и
    #: `export/docx.py` строят печать из этого поля, а не из `report`, и своего
    #: фильтра по `evidence` не ведут — БЛОК-6 финального аудита нашёл живой
    #: маршрут, где `report` печатал карточку без подтверждений в таблице
    #: сравнения, потому что решение «убрать карточку» и решение «убрать
    #: строку» считались двумя разными фильтрами в разных местах. По умолчанию
    #: `None` — `__post_init__` собирает `client_report` из `report` тем же
    #: `verdicts.visible_report(…, require_evidence=True)` (без `card_ids`/
    #: `effective` — `report` уже прошёл фильтр reject, повторно его убирать
    #: нечем и незачем): безопасный запасной путь для конструкторов, которые
    #: строят `ExportView` напрямую и про `client_report` не знают.
    #: `build_export_view` передаёт настоящий, уже посчитанный отчёт явно.
    client_report: Report | None = None
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
    #: `verdicts.visible_report` спрятал таблицу сравнения целиком (РЕШ-212,
    #: РЕШ-213): исходные строки сводки были, но после фильтра (reject или
    #: пустой `evidence` — тот же признак, что у `client_report`) их число не
    #: совпало с числом карточек — сопоставить строку с конкретной карточкой
    #: нечем (см. `verdicts._visible_summary`). Пустая сводка модели без
    #: единой строки с самого начала этот флаг не поднимает: там скрывать
    #: нечего, а не «нечем сопоставить». Флаг решает, печатать ли на месте
    #: таблицы честную строку вместо тишины — выбор ТЕКСТА заглушки вместо
    #: рекомендации от него больше не зависит (РЕШ-214): экспортёры смотрят на
    #: факт «таблица напечатана» (`bool(client_report.summary.comparison_rows)`),
    #: а не на причину её отсутствия — этот флаг называет только одну из
    #: возможных причин и сам по себе фактом не является.
    comparison_table_hidden: bool = False

    def __post_init__(self) -> None:
        """`client_report` без явного значения — `report`, довычищенный от пустого `evidence`.

        Пустые `card_ids`/`effective` означают «reject уже применён снаружи,
        второй раз его считать нечем» — `visible_report` при пустых словарях
        не находит ни одного вердикта и просто применяет вторую часть
        признака (РЕШ-213), см. докстроку поля выше.
        """
        if self.client_report is None:
            self.client_report = visible_report(self.report, {}, {}, require_evidence=True)


@asynccontextmanager
async def _session_scope(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Оборачивает уже открытую сессию запроса под форму `SessionFactory` `load_report`."""
    yield session


async def build_export_view(session: AsyncSession, request_id: int) -> ExportView:
    """Собрать `ExportView` запроса: report_json → правки → фильтр видимости.

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
    client_visible = visible_report(edited_report, original_ids, effective, require_evidence=True)
    card_ids, rework_keys = _reindex_after_filter(original_ids, effective)
    comparison_table_hidden = _comparison_table_hidden(edited_report, original_ids, effective)

    latest = max(effective.values(), key=lambda row: (row.created_at, row.id), default=None)

    return ExportView(
        request_id=request_id,
        report=visible,
        client_report=client_visible,
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


def _comparison_table_hidden(
    edited_report: Report,
    original_ids: dict[CardKey, int],
    effective: dict[int, VerdictRow],
) -> bool:
    """True, когда клиентский `verdicts.visible_report` спрятал таблицу сравнения целиком (РЕШ-212, РЕШ-213).

    Повторяет то же условие, каким `verdicts._visible_summary` решает «не
    печатать таблицу вовсе», уже с `require_evidence=True` (тот же признак
    видимости, что у `ExportView.client_report`, — иначе карточка, скрытая
    только из-за пустого `evidence`, не учлась бы здесь, и решение «спрятана
    ли таблица» разошлось бы с решением «что реально попало в печать»,
    БЛОК-6). `edited_report` — отчёт ДО фильтра (тот же вход, что видела
    `visible_report`), `original_ids`/`effective` — исходные id карточек и
    действующие вердикты, тем же контрактом, что и у `visible_report`.

    Условие «спрятана» — три штуки разом: 1) исходные строки были
    (`comparison_rows` непуст — пустая сводка `fallback_summary` или отчёт без
    сравнения не получает пометки, там скрывать нечего с самого начала); 2)
    хотя бы одна карточка основной секции реально скрыта (иначе таблица и не
    трогалась); 3) число исходных строк не совпало с числом карточек до
    фильтра — только это несовпадение и не даёт построчно сопоставить строку
    с карточкой (`_visible_summary` в противном случае просто отфильтровывает
    строки по тем же позициям, что и карточки, ничего не пряча).
    """
    original_rows = edited_report.summary.comparison_rows
    if not original_rows:
        return False
    original_main_cards = len(edited_report.cards)
    visible_main_cards = visible_position_count(
        edited_report.cards, original_ids, effective, is_contest=False, require_evidence=True
    )
    if visible_main_cards == original_main_cards:
        return False  # ни одна карточка не скрыта — скрывать нечего
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
    original_ids: dict[CardKey, int],
    effective: dict[int, VerdictRow],
) -> tuple[dict[CardKey, int], set[CardKey]]:
    """Пересчитать (секция, позиция) под порядок, который отдаёт `visible_report`.

    Решение «карточка скрыта» берёт `verdicts.hidden_reason` (РЕШ-215) —
    своего сравнения с `REJECT` эта функция больше не ведёт, вызывается без
    `require_evidence` (по умолчанию `False`): результат обязан совпадать с
    `visible_report(edited_report, ..., require_evidence=False)`, тем самым
    отчётом, что видит `ExportView.report` (операторский вид, а не
    `client_report`) — см. докстроку поля `ExportView.card_ids`, от этого
    согласования зависит, какой карточке принадлежит правка оператора.
    Пометка `rework` — второй, независимый признак (она не про скрытие,
    карточка с `rework` клиенту уезжает): считается по-прежнему, отдельно от
    решения `hidden_reason`.

    Карточка в вызов `hidden_reason` не передаётся (П-25 пятой проверки
    аудита): при `require_evidence=False` — значение по умолчанию и
    единственное здесь — карточка не нужна, и сигнатура это допускает явно
    (`card: SolutionCard | None`; с `require_evidence=True` та же функция
    падает `ValueError`, если карточку не дали). Раньше сюда индексировали
    `edited_report.cards[key[1]]`/`edited_report.contest_cards[key[1]]` ради
    аргумента, который так и не читался, — при рассинхроне («строк
    `solution_cards` в базе больше, чем карточек в `report_json`») это падало
    `IndexError` (500 на операторской странице отчёта и на выгрузке) вместо
    честного результата, который отдавала версия кода до четвёртой волны, —
    она вообще не индексировала карточки по позиции здесь.
    """
    card_ids: dict[CardKey, int] = {}
    rework_keys: set[CardKey] = set()
    for is_contest in (False, True):
        ordered_keys = sorted(key for key in original_ids if key[0] == is_contest)
        new_position = 0
        for key in ordered_keys:
            card_id = original_ids[key]
            verdict = effective.get(card_id)
            if hidden_reason(None, verdict) is not None:
                continue
            new_key = (is_contest, new_position)
            card_ids[new_key] = card_id
            if verdict is not None and verdict.decision == VerdictDecision.REWORK:
                rework_keys.add(new_key)
            new_position += 1
    return card_ids, rework_keys
