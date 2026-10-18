"""Секция «Что делают в мире» во всех выходах (Э6-4, РЕШ-244).

Единственное место, где решается, какие зарубежные аналоги видит читатель:
страница отчёта, markdown, PDF, DOCX и JSON зовут `analog_sections` и не
считают это сами (урок РЕШ-213: одно условие — в одном месте). Условие
двойное: аналог без единого подтверждения (`evidence`) не печатается нигде —
правило «ни одного факта без ссылки»; страна аналога идёт через
`countries.country_status` по режиму брифа отчёта — `excluded` не печатается,
`footnote` печатается одной строкой (название, страна, ссылка), `main` — полной
карточкой.

Режим берётся из `report.brief.mode`. В `report_json` он не сохраняется
(`Brief.mode` исключён из сериализации), поэтому `export.view.build_export_view`
накладывает его из строки `briefs` до того, как отчёт попадёт сюда.

Чистые функции без сети и без базы (инвариант №7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.countries import CountryStatus, country_status
from app.schemas import AnalogCard, Report

#: Заголовок секции — задан SPEC Э6-4. Секция стоит после основных и конкурсных
#: карточек и в лимит 5–7 не входит.
ANALOG_SECTION_TITLE = "Что делают в мире"

#: Подпись блока сносок. Текст клиенту — на утверждение владельцу (§9 п. 4).
ANALOG_FOOTNOTES_TITLE = "Справочно: аналоги из других стран"

#: Пустое распространение — честное «не найдено», а не пропуск строки.
TRACTION_NOT_FOUND = "не найдено"

#: Поля JSON-карточки, которые клиенту не отдаются: ссылки кандидатов — след
#: отбора, подтверждения живут в `evidence` (как у `SolutionCard`).
_INTERNAL_FIELDS = frozenset({"candidate_urls"})


@dataclass(frozen=True)
class AnalogFootnote:
    """Аналог из страны режима «сноской»: название, страна, ссылка."""

    title: str
    country: str
    url: str


@dataclass(frozen=True)
class AnalogSections:
    """Что печатать: полные карточки и сноски — после фильтра `analog_sections`."""

    main: tuple[AnalogCard, ...] = ()
    footnotes: tuple[AnalogFootnote, ...] = ()

    def __bool__(self) -> bool:
        """Секция печатается, только если в ней есть хоть одна строка."""
        return bool(self.main or self.footnotes)


def analog_sections(report: Report) -> AnalogSections:
    """Разложить аналоги отчёта на полные карточки и сноски по режиму брифа.

    Порядок внутри каждой группы — как в `report.analog_cards`. Аналог без
    `evidence` и аналог из исключённой страны пропускаются. Ссылка сноски —
    первое подтверждение аналога.
    """
    mode = report.brief.mode
    main: list[AnalogCard] = []
    footnotes: list[AnalogFootnote] = []
    for card in report.analog_cards:
        if not card.evidence:
            continue
        status = country_status(card.country, mode)
        if status is CountryStatus.EXCLUDED:
            continue
        if status is CountryStatus.FOOTNOTE:
            footnotes.append(AnalogFootnote(title=card.title, country=card.country, url=str(card.evidence[0].url)))
        else:
            main.append(card)
    return AnalogSections(main=tuple(main), footnotes=tuple(footnotes))


def analog_fields(card: AnalogCard) -> list[tuple[str, str]]:
    """Подписи и значения текстовых полей карточки аналога — общие для всех форматов.

    Подписи — на утверждение владельцу (§9 п. 4). Пустое распространение
    печатается как «не найдено».
    """
    return [
        ("Страна", card.country),
        ("Бизнес-модель", card.business_model),
        ("Распространение", card.traction.strip() or TRACTION_NOT_FOUND),
        ("Переносимость в РФ", card.transferability_ru),
        ("Что делать в РФ", card.what_to_do_in_ru),
    ]


def analog_evidence(card: AnalogCard) -> list[tuple[str, str]]:
    """Подтверждения аналога: утверждение и ссылка. Первым — страна, затем `evidence`."""
    items = [(f"Страна аналога: {card.country}", str(card.country_evidence_url))]
    items.extend((item.claim, str(item.url)) for item in card.evidence)
    return items


def footnote_line(footnote: AnalogFootnote) -> str:
    """Сноска одной строкой: название, страна, ссылка."""
    return f"{footnote.title} — {footnote.country} — {footnote.url}"


def analog_markdown_lines(sections: AnalogSections) -> list[str]:
    """Строки markdown секции; пустой список, если печатать нечего (заголовок без карточек — обещание впустую)."""
    if not sections:
        return []
    lines = [f"## {ANALOG_SECTION_TITLE}", ""]
    for index, card in enumerate(sections.main, start=1):
        lines.extend([f"### {index}. {card.title}", "", card.essence, ""])
        for label, value in analog_fields(card):
            lines.extend([f"**{label}:** {value}", ""])
        lines.extend(["**Чем подтверждено:**", ""])
        lines.extend(f"- {claim} — [источник]({url})" for claim, url in analog_evidence(card))
        lines.append("")
    if sections.footnotes:
        lines.extend([f"**{ANALOG_FOOTNOTES_TITLE}:**", ""])
        lines.extend(f"- {footnote_line(footnote)}" for footnote in sections.footnotes)
        lines.append("")
    return lines


def analog_payload(sections: AnalogSections) -> dict[str, Any]:
    """Секция для JSON-выгрузки: `{main: [...], footnotes: [{title, country, url}]}`."""
    return {
        "main": [card.model_dump(mode="json", exclude=set(_INTERNAL_FIELDS)) for card in sections.main],
        "footnotes": [
            {"title": footnote.title, "country": footnote.country, "url": footnote.url}
            for footnote in sections.footnotes
        ],
    }
