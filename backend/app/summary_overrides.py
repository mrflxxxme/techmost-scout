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


def is_summary_edited(override: SummaryOverrideData | None) -> bool:
    """True, если сводку правил эксперт — хотя бы одно поле реально отличается от текста модели.

    Отдельная именованная функция, а не инлайн `override is not None and not
    override.is_empty()` в вызывающем коде (аудит Э4, правка П-8): мутация,
    убирающая проверку `is_empty()` (`summary_text_edited = override is not
    None`), ловится юнит-тестом прямо на этой функции, без похода в БД и HTTP
    — набор из 1132 тестов её прежде не заметил именно потому, что проверки
    не было нигде отдельно от многослойного `build_export_view`.
    """
    return override is not None and not override.is_empty()


def normalize_summary_text(text: str) -> str:
    """Убрать пробелы и пустые строки по краям текста для сравнения правки с исходником.

    Сравнение правки эксперта с исходным текстом модели идёт по этому
    нормализованному виду (П-6): лишний пробел в конце строки или пустая
    строка, случайно оставленная в начале/конце textarea браузером, не
    должны превращать «сохранение без изменений» в правку. Внутренние
    переносы строк и пробелы намеренно не трогаются — только края текста
    целиком: считать абзацы внутри текста не наша забота, а увеличение зоны
    нормализации рискует счесть неправкой то, что эксперт изменил осознанно.
    """
    return text.strip()


def _normalize_caveats(caveats: list[str]) -> list[str]:
    """Список оговорок для сравнения — без пустых строк и пробелов по краям каждой."""
    return [item.strip() for item in caveats if item.strip()]


def resolve_summary_edit(
    *,
    original_recommendation: str,
    original_caveats: list[str],
    submitted_recommendation: str,
    submitted_caveats: list[str],
) -> SummaryOverrideData:
    """Решить, какие поля формы правки сводки эксперт действительно изменил.

    Поле сравнивается с исходным текстом отчёта (тем, что дал бы `apply_overrides`
    без правки самой сводки) по нормализованному виду (`normalize_summary_text`):
    дословное совпадение — не правка, даже если форма отправлена и сохранение
    прошло успешно (П-6, аудит Э4: один клик по «Сохранить сводку» без единой
    правки не должен печатать клиенту текст модели с чужой нумерацией карточек).
    Поля правятся независимо друг от друга — заголовок задачи «правкой считается
    изменённый текст», а не факт отправки формы целиком: если эксперт поменял
    только рекомендацию, оговорки остаются `None` («не правил») и наоборот.
    """
    recommendation = (
        None
        if normalize_summary_text(submitted_recommendation) == normalize_summary_text(original_recommendation)
        else submitted_recommendation
    )
    caveats = (
        None
        if _normalize_caveats(submitted_caveats) == _normalize_caveats(original_caveats)
        else list(submitted_caveats)
    )
    return SummaryOverrideData(recommendation=recommendation, caveats=caveats)
