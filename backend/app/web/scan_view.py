"""Вид экспресс-скана: что видит посетитель публичной страницы и JSON (Э6-6б).

Скан показывается без эксперта (РЕШ-252), поэтому вид строится по белому списку
полей и несёт плашку в каждом выходе. Плашка — свойство класса, а не аргумент
конструктора и не текст шаблона: снять её нельзя ни из кода, ни правкой
страницы. Контакты (телефоны, почта) вычищаются из всех строк (РЕШ-264);
полей `contact_leads`, `developer_team`, `next_step`, конкурсной секции и
сводки в виде просто нет.

В скан идут только карточки и аналоги, прошедшие автосверку
(`pipeline.autoverify`): до трёх тех и других; чего нет — «не найдено».
Режим подачи у скана `business`; аналоги раскладывает та же `analog_sections`,
что и выгрузки (РЕШ-213), поэтому вид не считает страну сам.

Маршрутов и шаблонов здесь нет — их делает Э6-6в. Без сети и без базы.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.export.analogs import analog_sections
from app.pipeline.autoverify import SourceRow, analog_verified, card_verified, source_texts
from app.schemas import AnalogCard, MaturityStage, Report, SolutionCard
from app.web.scan_scrub import scrub_contacts, scrub_url

#: Плашка скана — несъёмная (РЕШ-252). Текст клиенту: в редакции исполнителя (РЕШ-275).
SCAN_BANNER = "Автоматический обзор, не проверен экспертом"

#: Тип и версия схемы JSON скана; версия самостоятельна и с `export.json` не связана.
SCAN_KIND = "express_scan"
SCAN_SCHEMA_VERSION = "1.0"

#: Сколько карточек и аналогов показывает скан (РЕШ-240).
MAX_SCAN_CARDS = 3
MAX_SCAN_ANALOGS = 3

#: Чего нет — «не найдено», а не пропуск строки.
NOT_FOUND = "не найдено"

#: Стадия зрелости словами клиента — как в полном отчёте.
SCAN_MATURITY_RU: dict[MaturityStage, str] = {
    MaturityStage.LAB: "работает в лаборатории",
    MaturityStage.PROTOTYPE: "есть опытный образец",
    MaturityStage.PILOT: "прошла пилот на объекте",
    MaturityStage.SERIES: "применяется серийно",
    MaturityStage.UNKNOWN: "стадия не определена",
}


def _text(value: str | None) -> str:
    """Строка вида: контакты вычищены, пустое — «не найдено»."""
    cleaned = scrub_contacts((value or "").strip())
    return cleaned or NOT_FOUND


@dataclass(frozen=True)
class ScanFact:
    """Подтверждённое утверждение: текст и ссылка на первоисточник."""

    claim: str
    url: str


@dataclass(frozen=True)
class ScanSupport:
    """Инструмент господдержки; в скан идёт только со ссылкой."""

    instrument: str
    url: str


@dataclass(frozen=True)
class ScanCard:
    """Карточка скана — белый список полей `SolutionCard`."""

    title: str
    essence: str
    maturity_stage: str
    developer_org: str
    evidence: tuple[ScanFact, ...]
    applicability: str
    gov_support: tuple[ScanSupport, ...]


@dataclass(frozen=True)
class ScanAnalog:
    """Зарубежный аналог в скане: без `what_to_do_in_ru` и ссылок отбора."""

    title: str
    essence: str
    country: str
    country_evidence_url: str
    business_model: str
    traction: str
    transferability_ru: str
    evidence: tuple[ScanFact, ...]


@dataclass(frozen=True)
class ScanView:
    """Вид экспресс-скана. Плашки в полях нет: это свойство класса."""

    cards: tuple[ScanCard, ...] = ()
    analogs: tuple[ScanAnalog, ...] = ()
    sources_unavailable: tuple[str, ...] = ()

    @property
    def banner(self) -> str:
        """Плашка «Автоматический обзор, не проверен экспертом» — несъёмная."""
        return SCAN_BANNER

    @property
    def cards_note(self) -> str:
        """«не найдено», если ни одна карточка не прошла сверку; иначе пусто."""
        return "" if self.cards else NOT_FOUND

    @property
    def analogs_note(self) -> str:
        """«не найдено», если ни один аналог не прошёл сверку; иначе пусто."""
        return "" if self.analogs else NOT_FOUND


def _facts(card: SolutionCard | AnalogCard) -> tuple[ScanFact, ...]:
    return tuple(ScanFact(claim=_text(item.claim), url=scrub_url(str(item.url))) for item in card.evidence)


def _scan_card(card: SolutionCard) -> ScanCard:
    return ScanCard(
        title=_text(card.title),
        essence=_text(card.essence),
        maturity_stage=SCAN_MATURITY_RU.get(card.maturity_stage, SCAN_MATURITY_RU[MaturityStage.UNKNOWN]),
        developer_org=_text(card.developer_org),
        evidence=_facts(card),
        applicability=_text(card.applicability),
        gov_support=tuple(
            ScanSupport(instrument=_text(item.instrument), url=scrub_url(str(item.url)))
            for item in card.gov_support
            if item.url
        ),
    )


def _scan_analog(card: AnalogCard) -> ScanAnalog:
    return ScanAnalog(
        title=_text(card.title),
        essence=_text(card.essence),
        country=card.country,
        country_evidence_url=scrub_url(str(card.country_evidence_url)),
        business_model=_text(card.business_model),
        traction=_text(card.traction),
        transferability_ru=_text(card.transferability_ru),
        evidence=_facts(card),
    )


def build_scan_view(report: Report, rows: Sequence[SourceRow]) -> ScanView:
    """Собрать вид скана: сверка с текстами `rows`, затем страна, затем лимит три.

    Сверка идёт до лимита: три места занимают только сверенные карточки, а не
    первые три из списка. Конкурсные карточки и сводка отчёта в скан не входят.
    """
    texts = source_texts(rows)
    cards = [card for card in report.cards if card_verified(card, texts)]
    analogs = [analog for analog in report.analog_cards if analog_verified(analog, texts)]
    sections = analog_sections(report.model_copy(update={"analog_cards": analogs}))
    return ScanView(
        cards=tuple(_scan_card(card) for card in cards[:MAX_SCAN_CARDS]),
        analogs=tuple(_scan_analog(card) for card in sections.main[:MAX_SCAN_ANALOGS]),
        sources_unavailable=tuple(scrub_contacts(item) for item in report.sources_unavailable),
    )


def _fact_payload(fact: ScanFact) -> dict[str, str]:
    return {"claim": fact.claim, "url": fact.url}


def _clean_node(node: Any, key: str = "") -> Any:
    """Проход по готовому JSON: ссылки — через `scrub_url`, все прочие строки — через `scrub_contacts`."""
    if isinstance(node, dict):
        return {name: _clean_node(value, name) for name, value in node.items()}
    if isinstance(node, list):
        return [_clean_node(item, key) for item in node]
    if isinstance(node, str):
        return scrub_url(node) if key.endswith("url") else scrub_contacts(node)
    return node


def scan_payload(view: ScanView) -> dict[str, Any]:
    """JSON скана; `banner`, `kind` и `schema_version` присутствуют всегда.

    Вычистка контактов повторяется здесь на готовом словаре: вид, собранный
    руками в обход `build_scan_view`, контакты в публичный JSON не пропустит.
    """
    return _clean_node(_raw_payload(view))


def _raw_payload(view: ScanView) -> dict[str, Any]:
    return {
        "schema_version": SCAN_SCHEMA_VERSION,
        "kind": SCAN_KIND,
        "banner": view.banner,
        "cards": [
            {
                "title": card.title,
                "essence": card.essence,
                "maturity_stage": card.maturity_stage,
                "developer_org": card.developer_org,
                "evidence": [_fact_payload(fact) for fact in card.evidence],
                "applicability": card.applicability,
                "gov_support": [{"instrument": s.instrument, "url": s.url} for s in card.gov_support],
            }
            for card in view.cards
        ],
        "cards_note": view.cards_note,
        "analogs": [
            {
                "title": analog.title,
                "essence": analog.essence,
                "country": analog.country,
                "country_evidence_url": analog.country_evidence_url,
                "business_model": analog.business_model,
                "traction": analog.traction,
                "transferability_ru": analog.transferability_ru,
                "evidence": [_fact_payload(fact) for fact in analog.evidence],
            }
            for analog in view.analogs
        ],
        "analogs_note": view.analogs_note,
        "sources_unavailable": list(view.sources_unavailable),
    }
