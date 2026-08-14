"""Отчёт в markdown — базовый формат выдачи.

Markdown выбран первым, потому что он читается глазами в терминале, ложится в
git и из него без потерь собираются PDF и DOCX на четвёртом этапе.

Форма отчёта повторяет структуру карточки из PRD: сначала сводка и
рекомендация, потом варианты, у каждого — подтверждения ссылками. Никаких
утверждений без источника: если evidence пуст, это видно в отчёте.
"""

from __future__ import annotations

from app.schemas import Confidence, MaturityStage, Report

MATURITY_RU: dict[MaturityStage, str] = {
    MaturityStage.LAB: "работает в лаборатории",
    MaturityStage.PROTOTYPE: "есть опытный образец",
    MaturityStage.PILOT: "прошла пилот на объекте",
    MaturityStage.SERIES: "применяется серийно",
    MaturityStage.UNKNOWN: "стадия не определена",
}

CONFIDENCE_RU: dict[Confidence, str] = {
    Confidence.HIGH: "высокая",
    Confidence.MEDIUM: "средняя",
    Confidence.LOW: "низкая",
}


def render_markdown(report: Report, *, validated: bool = False, reviewer: str = "") -> str:
    """Собрать отчёт. До валидации экспертом на нём стоит пометка «черновик»."""
    lines: list[str] = []
    brief = report.brief

    lines.append("# Подбор научно-технических решений")
    lines.append("")
    if validated and reviewer:
        lines.append(f"*Подготовлено системой «ТехМост Скаут», проверено: {reviewer}, {report.generated_at:%d.%m.%Y}*")
    else:
        lines.append("> **ЧЕРНОВИК — не проверено экспертом.** Не отправлять клиенту до научной валидации.")
    lines.append("")

    lines.append("## Задача")
    lines.append("")
    lines.append(f"**Отрасль:** {brief.industry}")
    lines.append("")
    lines.append(brief.task_summary)
    lines.append("")
    if brief.constraints:
        lines.append("**Ограничения:** " + "; ".join(brief.constraints))
        lines.append("")
    if brief.target_maturity:
        lines.append(f"**Требуемая готовность:** {MATURITY_RU[brief.target_maturity]}")
        lines.append("")

    lines.append("## Рекомендация")
    lines.append("")
    lines.append(report.summary.recommendation)
    lines.append("")

    if report.summary.comparison_rows:
        keys = list(report.summary.comparison_rows[0].keys())
        lines.append("## Сравнение вариантов")
        lines.append("")
        lines.append("| " + " | ".join(keys) + " |")
        lines.append("|" + "|".join(["---"] * len(keys)) + "|")
        for row in report.summary.comparison_rows:
            lines.append("| " + " | ".join(str(row.get(k, "")).replace("|", "\\|") for k in keys) + " |")
        lines.append("")

    lines.append("## Варианты решений")
    lines.append("")
    if not report.cards:
        lines.append("_Пригодных вариантов не найдено._")
        lines.append("")
    for index, card in enumerate(report.cards, start=1):
        lines.append(f"### {index}. {card.title}")
        lines.append("")
        # Без единого подтверждения карточка не имеет права заявлять зрелость —
        # это машинная проверка инварианта №1 (задача Ф-4).
        if card.evidence:
            maturity = MATURITY_RU.get(card.maturity_stage, card.maturity_stage.value)
            trl = f" (TRL ≈ {card.trl_estimate})" if card.trl_estimate else ""
            lines.append(f"**Стадия зрелости:** {maturity}{trl}")
            lines.append("")
        lines.append(card.essence)
        lines.append("")

        developer = card.developer_org or "не найдено"
        if card.developer_team:
            developer += f" — {card.developer_team}"
        lines.append(f"**Разработчик:** {developer}")
        lines.append("")
        if card.contact_leads:
            lines.append("**Как выйти на связь:** " + "; ".join(card.contact_leads))
            lines.append("")

        lines.append(f"**Применимость к задаче:** {card.applicability}")
        lines.append("")
        if card.requirement_checks:
            lines.append("**Сверка с требованиями задачи:**")
            lines.append("")
            for check in card.requirement_checks:
                if check.status == "confirmed":
                    status_text = "подтверждено источником"
                    if check.evidence_url:
                        status_text += f" ([ссылка]({check.evidence_url}))"
                else:
                    status_text = "не подтверждено источником"
                check_note = f" — {check.note}" if check.note else ""
                lines.append(f"- {check.requirement}: {status_text}{check_note}")
            lines.append("")
        if card.risks:
            lines.append("**Риски:**")
            lines.append("")
            for risk in card.risks:
                lines.append(f"- {risk}")
            lines.append("")
        if card.gov_support:
            lines.append("**Господдержка:**")
            lines.append("")
            for item in card.gov_support:
                limit = f" — {item.limit}" if item.limit else ""
                url = f" ([условия]({item.url}))" if item.url else ""
                lines.append(f"- **{item.instrument}**{limit}: {item.why}{url}")
            lines.append("")

        lines.append(f"**Следующий шаг:** {card.next_step}")
        lines.append("")
        confidence = CONFIDENCE_RU.get(card.confidence, card.confidence.value)
        note = f" — {card.confidence_note}" if card.confidence_note else ""
        lines.append(f"**Уверенность:** {confidence}{note}")
        lines.append("")

        lines.append("**Чем подтверждено:**")
        lines.append("")
        if card.evidence:
            for item in card.evidence:
                lines.append(f"- {item.claim} — [источник]({item.url})")
        else:
            lines.append("- _Подтверждений не приложено — карточку нельзя показывать клиенту._")
        lines.append("")

        if card.unsourced_claims:
            lines.append("**Не подтверждено проверкой:**")
            lines.append("")
            for claim in card.unsourced_claims:
                lines.append(f"- {claim}")
            lines.append("")

        for pdf_url in card.pdf_urls:
            lines.append(f"Полный текст: [PDF]({pdf_url})")
            lines.append("")

    # Заголовок раздела печатается, если есть хоть что-то из двух: недоступные
    # источники или оговорки. Раньше он был привязан только к недоступным
    # источникам, и на здоровом прогоне оговорка (отказ интейка, отказ сводки)
    # выпадала в конец отчёта голым пунктом списка — клиент видел висящую
    # строку непонятно к чему.
    if report.sources_unavailable or report.summary.caveats:
        lines.append("## Ограничения этого поиска")
        lines.append("")
        if report.sources_unavailable:
            lines.append(
                "Не ответили источники: "
                + ", ".join(report.sources_unavailable)
                + ". Выдача могла быть полнее — стоит повторить запрос позже."
            )
            lines.append("")
        if report.summary.caveats:
            for caveat in report.summary.caveats:
                lines.append(f"- {caveat}")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        f"*Запрос №{report.request_id or '—'} · {report.generated_at:%d.%m.%Y %H:%M} · "
        f"стоимость машинной обработки {report.cost_rub:.2f} ₽*"
    )
    lines.append("")
    lines.append("*Кирилл Уклонский · uklonskiy.k@gmail.com*")
    return "\n".join(lines)
