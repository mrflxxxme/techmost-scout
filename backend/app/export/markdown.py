"""Отчёт в markdown — базовый формат выдачи.

Markdown выбран первым, потому что он читается глазами в терминале, ложится в
git и из него без потерь собираются PDF и DOCX на четвёртом этапе.

Форма отчёта повторяет структуру карточки из PRD: сначала сводка и
рекомендация, потом варианты, у каждого — подтверждения ссылками. Никаких
утверждений без источника: если evidence пуст, это видно в отчёте.
"""

from __future__ import annotations

from app.schemas import Confidence, MaturityStage, Report, SolutionCard

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

#: Заголовок секции для решений конкурсного класса (РЕШ-85 п. 1). Секция стоит
#: после основных карточек и в лимит 5–7 (FR-9) не входит: хакатонный прототип с
#: потолком зрелости `prototype` проигрывает статье с промышленной апробацией
#: 0,8–1,0 балла только на зрелости и в общем зачёте почти никогда не попал бы в
#: выдачу. Вынести класс отдельно дешевле, чем размывать главный критерий
#: приёмки продукта.
CONTEST_SECTION_HEADING = "## Ранние разработки и команды"

#: Формулировка статуса прав — дословно из РЕШ-79 п. 1. Текст идёт клиенту,
#: поэтому в коде не переписывается: правило живёт не на площадке, а в положении
#: конкретного конкурса, и машинно оно не определяется. Другая формулировка —
#: решение владельца, а не правка исполнителя.
IP_STATUS_NOTICE = "статус прав не подтверждён, требует выяснения у команды"

#: Оговорка клиенту, когда ограничения задачи не выделены из постановки даже
#: после повторного адресного вызова интейка (РЕШ-115, РЕШ-123). Текст идёт
#: клиенту дословно, поэтому в коде не переписывается. Печатается в двух
#: местах одной константой (не подстрокой): в разделе "Задача" по флагу
#: `Report.constraints_unresolved` и в "Ограничения этого поиска" тем же
#: механизмом оговорок, что и прочие caveats (`cli.py`, `orchestrator.py`).
#: Опознание оговорки по подстроке текста в экспортёре запрещено (постановка
#: задачи, мутация ж) - текст поменяется, и проверка молча исчезнет.
CONSTRAINTS_UNRESOLVED_NOTICE = (
    "Требования задачи не выделены из постановки — построчная сверка карточек не проводилась."
)


def render_markdown(report: Report, *, validated: bool = False, reviewer: str = "") -> str:
    """Собрать отчёт. До валидации экспертом на нём стоит пометка «черновик».

    Карточки конкурсного класса берутся из `report.contest_cards` (РЕШ-92).
    Они печатаются отдельной секцией «Ранние разработки и команды» и в лимит
    5–7 основной выдачи не входят (РЕШ-85 п. 1). Список пуст — секции нет
    вовсе: заголовок без карточек есть обещание, которого отчёт не выполняет.

    Отдельного параметра `contest_cards` у функции нет сознательно (Т-33):
    пока он был, `render_markdown(report)` печатал отчёт без секции молча —
    два способа передать одно и то же, и умолчание побеждало. Единственный
    источник карточек обеих секций — сам отчёт.
    """
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
    if report.constraints_unresolved:
        lines.append(CONSTRAINTS_UNRESOLVED_NOTICE)
        lines.append("")
    if brief.constraints:
        lines.append("**Требования и ограничения:** " + "; ".join(brief.constraints))
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
        lines.extend(_render_card(card, index))

    # Секция вне лимита 5–7 (РЕШ-85 п. 1). Печатается только при непустом
    # списке: пустой заголовок клиент читает как недосказанность.
    if report.contest_cards:
        lines.append(CONTEST_SECTION_HEADING)
        lines.append("")
        for index, card in enumerate(report.contest_cards, start=1):
            lines.extend(_render_card(card, index, contest=True))

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


def _card_platform(card: SolutionCard) -> str:
    """Площадка конкурса — узел адреса первого кандидата, если он известен.

    Берётся узел адреса, а не слово из текста: адрес кандидата заполняет код по
    исходной выдаче, тогда как название площадки в свободном описании пишет сама
    команда.
    """
    for url in card.candidate_urls:
        host = (url.host or "").lower()
        if host:
            return host.removeprefix("www.")
    return ""


def _ip_status_line(card: SolutionCard) -> str:
    """Строка статуса прав для карточки конкурсного класса (РЕШ-79 п. 1).

    Печатается у каждой карточки секции, известна площадка или нет: у
    конкурсного решения права — условие пригодности, и умолчание здесь клиент
    прочитает как «вопрос снят». Само значение `ip_status` идёт словами
    («не установлено» — честный отказ, а не догадка), формулировка риска —
    дословно из решения владельца.
    """
    platform = _card_platform(card)
    suffix = f" (площадка: {platform})" if platform else ""
    return f"**Статус прав:** {card.ip_status.value} — {IP_STATUS_NOTICE}{suffix}"


def _render_card(card: SolutionCard, index: int, *, contest: bool = False) -> list[str]:
    """Строки одной карточки. `contest` добавляет строку статуса прав.

    Тело карточки общее для обеих секций сознательно: проверка инварианта №1
    («без подтверждений нет заявленной зрелости») должна быть одна на все
    секции. Отдельная ветка печати для конкурсных карточек — ровно тот путь,
    которым проверка однажды обошлась стороной.
    """
    lines: list[str] = []
    lines.append(f"### {index}. {card.title}")
    lines.append("")
    # Без единого подтверждения карточка не имеет права заявлять зрелость —
    # это машинная проверка инварианта №1 (задача Ф-4).
    if card.evidence:
        maturity = MATURITY_RU.get(card.maturity_stage, card.maturity_stage.value)
        trl = f" (TRL ≈ {card.trl_estimate})" if card.trl_estimate else ""
        lines.append(f"**Стадия зрелости:** {maturity}{trl}")
        lines.append("")
    if contest:
        lines.append(_ip_status_line(card))
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
    return lines
