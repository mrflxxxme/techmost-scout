"""Наложение правки эксперта поверх рекомендации и оговорок сводки (РЕШ-206).

Аудит Э4 нашёл: карточка отклонена экспертом и убрана из клиентской выгрузки,
но рекомендация модели по-прежнему советовала отклонённый вариант первым, а
оговорки адресовались по исходной нумерации карточек. Таблицу сравнения чинит
механический фильтр (`app.verdicts.visible_report`, задача Э4-П1) — но текст
рекомендации и оговорок пишет модель свободной прозой, и починить нумерацию в
нём кодом нельзя: эксперт правит текст сам, тем же по духу механизмом, что и
правки карточек (`app.report_overrides`, РЕШ-186).

Модуль — чистые функции над `schemas.Report`, без сессии и без SQLAlchemy:
чтение и запись строки `summary_overrides` (`app.models.SummaryOverride`)
делает вызывающий (маршрут ревью и `app.export.view.build_export_view`).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas import Report


@dataclass(frozen=True)
class SummaryOverrideData:
    """Правка эксперта, уже прочитанная из строки `summary_overrides`.

    `None` в любом из полей значит «эксперт это поле не правил» — используется
    исходный текст модели; это не то же самое, что пустая строка или пустой
    список, которые эксперт мог сохранить осознанно.
    """

    recommendation: str | None = None
    caveats: list[str] | None = None

    def is_empty(self) -> bool:
        """True, если правки нет вовсе — наложение ничего не изменит."""
        return self.recommendation is None and self.caveats is None


def apply_summary_override(report: Report, override: SummaryOverrideData | None) -> Report:
    """Вернуть копию `report` с рекомендацией и/или оговорками эксперта.

    Правится `report.summary.recommendation` и `report.summary.caveats`
    целиком — как правки карточек (РЕШ-186), частичного слияния текста
    (склейки предложений, правки по подстроке) здесь нет. `override is None`
    либо оба поля правки пусты (`is_empty()`) — отчёт возвращается без
    изменений, сам `report` при этом не мутируется.
    """
    if override is None or override.is_empty():
        return report
    result = report.model_copy(deep=True)
    updates: dict[str, object] = {}
    if override.recommendation is not None:
        updates["recommendation"] = override.recommendation
    if override.caveats is not None:
        updates["caveats"] = list(override.caveats)
    result.summary = result.summary.model_copy(update=updates)
    return result
