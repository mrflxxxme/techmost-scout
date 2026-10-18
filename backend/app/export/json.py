"""JSON-выгрузка отчёта для приложений экосистемы (SPEC Э6-4, РЕШ-250).

Данные — тот же `ExportView` (`export.view.build_export_view`), из которого
печатаются PDF и DOCX: карточки и строки сравнения берутся из
`view.client_report` (reject и карточки без `evidence` уже убраны одним
проходом `verdicts.visible_report`, РЕШ-213), рекомендация и проза оговорок
— теми же пополевыми гейтами `recommendation_printable`/`caveats_printable`
(РЕШ-211), строки ограничений — тем же `limitations_lines`. Своего фильтра
видимости этот модуль не ведёт: второй путь правды разошёлся бы с печатью.

Замок черновика тот же, что у PDF/DOCX (РЕШ-199), но без флага `--draft`:
до вердикта эксперта выгрузки нет вовсе (`ReportNotValidatedError`).
Приложение экосистемы показывает выдачу людям, и отданный ему черновик был
бы автоотправкой клиенту (инвариант №2).

Стоимость запроса в выгрузку не входит: это внутренняя цифра компании, не
часть клиентского вида.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.export.analogs import analog_payload
from app.export.markdown import (
    COMPARISON_TABLE_HIDDEN_NOTICE,
    SUMMARY_NOT_PRINTABLE_NO_TABLE_NOTICE,
    SUMMARY_NOT_PRINTABLE_NOTICE,
    limitations_lines,
)
from app.export.view import ExportView, build_export_view, caveats_printable, recommendation_printable
from app.schemas import ECOSYSTEM_BRIEF_FIELDS, Brief, SolutionCard
from app.verdicts import signature_line

#: Версия формата выгрузки. Меняется только вместе с контрактом потребителей.
SCHEMA_VERSION = "1.0"

#: Поля карточки, которые печать не показывает клиенту: ссылки кандидатов —
#: служебный след отбора, подтверждения живут в `evidence`.
_CARD_INTERNAL_FIELDS = frozenset({"candidate_urls"})


class ReportNotValidatedError(Exception):
    """Отчёт ещё не проверен экспертом — JSON-выгрузки нет (РЕШ-199, инвариант №2)."""


def brief_payload(brief: Brief) -> dict[str, Any]:
    """Бриф для выгрузки: поля схемы плюс поля экосистемы явно.

    У `Brief` поля экосистемы исключены из сериализации (их нельзя пускать в
    промпты), поэтому `model_dump` их не отдаёт — здесь они дописываются.
    """
    payload = brief.model_dump(mode="json")
    for name in ECOSYSTEM_BRIEF_FIELDS:
        payload[name] = getattr(brief, name)
    return payload


def _card_payload(card: SolutionCard) -> dict[str, Any]:
    return card.model_dump(mode="json", exclude=set(_CARD_INTERNAL_FIELDS))


def build_report_payload(view: ExportView) -> dict[str, Any]:
    """Собрать словарь JSON-выгрузки из `ExportView` — тот же клиентский вид, что печать.

    Поля экосистемы брифа (регион, потребность, режим) уже наложены на
    `view.client_report.brief` в `build_export_view` — своего пути к строке
    `briefs` этот модуль не ведёт. Секция `analog_cards` — из
    `view.analog_sections`: `{main: [...], footnotes: [{title, country, url}]}`.

    `ReportNotValidatedError` — отчёт ещё черновик: выгрузки нет ни в каком
    режиме (РЕШ-199, РЕШ-250).
    """
    if view.is_draft:
        raise ReportNotValidatedError(f"отчёт запроса №{view.request_id} ещё не проверен экспертом")

    report = view.client_report
    printed_total = len(report.cards) + len(report.contest_cards)
    recommendation_ok = recommendation_printable(view, printed_total)
    caveats_ok = caveats_printable(view, printed_total)
    table_printed = bool(report.summary.comparison_rows)

    recommendation_notice = None
    if not recommendation_ok:
        recommendation_notice = SUMMARY_NOT_PRINTABLE_NOTICE if table_printed else SUMMARY_NOT_PRINTABLE_NO_TABLE_NOTICE

    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": view.request_id,
        "generated_at": report.generated_at.isoformat(),
        "validation": {
            "validated": True,
            "reviewer_name": view.reviewer_name,
            "validated_at": view.validated_at.isoformat() if view.validated_at is not None else None,
            "signature": signature_line(view.reviewer_name, view.validated_at) if view.validated_at else None,
        },
        "brief": brief_payload(report.brief),
        "cards": [_card_payload(card) for card in report.cards],
        "contest_cards": [_card_payload(card) for card in report.contest_cards],
        "analog_cards": analog_payload(view.analog_sections),
        "summary": {
            "recommendation": report.summary.recommendation if recommendation_ok else None,
            "recommendation_notice": recommendation_notice,
            "comparison_rows": [dict(row) for row in report.summary.comparison_rows],
            "comparison_table_notice": COMPARISON_TABLE_HIDDEN_NOTICE if view.comparison_table_hidden else None,
            # Пустые строки — разделители абзацев печати, в данных они не нужны.
            "limitations": [line for line in limitations_lines(report, include_caveats=caveats_ok) if line],
        },
        "sources_unavailable": list(report.sources_unavailable),
        "constraints_unresolved": report.constraints_unresolved,
    }


async def export_report_json(session: AsyncSession, request_id: int) -> dict[str, Any]:
    """Собрать JSON-выгрузку запроса: `build_export_view` → замок → словарь.

    Пробрасывает `ReportNotFoundError` (нет запроса или отчёта) и
    `ReportNotValidatedError` (нет вердикта эксперта) — вызывающий решает,
    каким ответом отказать.
    """
    view = await build_export_view(session, request_id)
    if view.is_draft:
        raise ReportNotValidatedError(f"отчёт запроса №{request_id} ещё не проверен экспертом")
    return build_report_payload(view)
