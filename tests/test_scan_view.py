"""Вид экспресс-скана (Э6-6б, РЕШ-252, РЕШ-264).

Строится из готового отчёта и строк кандидатов без сети и базы (инвариант №7).
Проверяется то, что обещает вид: плашка несъёмна и есть в каждом JSON, полей
`contact_leads` / `developer_team` / `next_step` нет нигде, телефоны и почта
вычищены из строк, берутся только сверенные карточки и не больше трёх, пустое —
«не найдено», аналог исключённой страны отсутствует.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, dataclass
from datetime import datetime

import pytest

from app.schemas import AnalogCard, Brief, Evidence, GovSupportMatch, Report, ReportSummary, SolutionCard
from app.web.scan_view import (
    CONTACT_HIDDEN,
    MAX_SCAN_ANALOGS,
    MAX_SCAN_CARDS,
    NOT_FOUND,
    SCAN_BANNER,
    ScanView,
    build_scan_view,
    scan_payload,
    scrub_contacts,
)

PHONE = "+7 (495) 123-45-67"
EMAIL = "ivanov@lab.example.org"
LEAD = "Позвонить главному инженеру Петрову"
TEAM = "Группа Сидорова"
NEXT_STEP = "Запросить коммерческое предложение"


@dataclass
class Row:
    """Те же поля, что у строки `candidates`."""

    url: str
    title: str = ""
    snippet: str = ""
    org: str | None = None
    year: int | None = None


def _url(index: int) -> str:
    return f"https://source.example.org/{index}"


def _rows(count: int = 8) -> list[Row]:
    return [Row(_url(i), title=f"Источник {i}", snippet=f"Запуск в {2020 + i} году, 35 %") for i in range(count)]


def _card(index: int, **changes) -> SolutionCard:
    data = {
        "title": f"Решение {index}",
        "essence": "Снижает затраты",
        "applicability": "Подходит для задачи",
        "next_step": NEXT_STEP,
        "developer_org": "Институт Ж",
        "developer_team": TEAM,
        "contact_leads": [LEAD, PHONE],
        "evidence": [Evidence(claim="Затраты ниже на 35%", url=_url(index))],
    }
    data.update(changes)
    return SolutionCard(**data)


def _analog(index: int, country: str = "US", **changes) -> AnalogCard:
    data = {
        "title": f"Аналог {index}",
        "essence": "Сервис подписки",
        "country": country,
        "country_evidence_url": _url(index),
        "business_model": "Подписка",
        "traction": "",
        "transferability_ru": "Перенос возможен",
        "what_to_do_in_ru": "Изучить условия",
        "evidence": [Evidence(claim="Сервис работает", url=_url(index))],
    }
    data.update(changes)
    return AnalogCard(**data)


def _report(cards=(), analogs=(), mode: str = "business", **changes) -> Report:
    brief = Brief(industry="Энергетика", task_summary="Снизить затраты")
    brief.mode = mode
    data = {
        "request_id": 1,
        "brief": brief,
        "cards": list(cards),
        "analog_cards": list(analogs),
        "contest_cards": [_card(7, title="Конкурсная карточка")],
        "summary": ReportSummary(recommendation="Сводка отчёта: начать с первого."),
        "generated_at": datetime(2026, 10, 21, 9, 0),
    }
    data.update(changes)
    return Report(**data)


def _all_keys(node) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {key for value in node.values() for key in _all_keys(value)}
    if isinstance(node, list):
        return {key for item in node for key in _all_keys(item)}
    return set()


def test_плашка_не_аргумент_конструктора():
    with pytest.raises(TypeError):
        ScanView(banner="другая плашка")  # type: ignore[call-arg]


def test_плашка_свойство_класса_и_не_присваивается():
    view = ScanView()
    assert view.banner == SCAN_BANNER == "Автоматический обзор, не проверен экспертом"
    assert isinstance(ScanView.banner, property)
    with pytest.raises((AttributeError, FrozenInstanceError)):
        view.banner = "иначе"  # type: ignore[misc]


@pytest.mark.parametrize("full", [False, True])
def test_scan_payload_всегда_с_плашкой_видом_и_версией(full: bool):
    view = build_scan_view(_report([_card(0)], [_analog(1)]) if full else _report(), _rows())
    payload = scan_payload(view)
    assert payload["banner"] == SCAN_BANNER
    assert payload["kind"] == "express_scan"
    assert payload["schema_version"] == "1.0"
    assert json.loads(json.dumps(payload, ensure_ascii=False))["banner"] == SCAN_BANNER


def test_в_виде_и_json_нет_контактов_шага_и_команды():
    view = build_scan_view(_report([_card(0)], [_analog(1)]), _rows())
    payload = scan_payload(view)
    dumped = json.dumps(payload, ensure_ascii=False) + repr(view)
    assert not _all_keys(payload) & {"contact_leads", "developer_team", "next_step", "what_to_do_in_ru"}
    for forbidden in (LEAD, TEAM, NEXT_STEP, PHONE, "Конкурсная карточка", "Сводка отчёта"):
        assert forbidden not in dumped
    assert not hasattr(view.cards[0], "contact_leads")
    assert not hasattr(view.cards[0], "next_step")
    assert not hasattr(view.cards[0], "developer_team")


def test_телефон_и_почта_в_essence_и_claim_заменены():
    card = _card(
        0,
        essence=f"Суть. Звоните {PHONE}, пишите {EMAIL}.",
        evidence=[Evidence(claim=f"Контакт {EMAIL}, тел. 8 800 555 35 35. Затраты ниже на 35%", url=_url(0))],
    )
    # Контакты лежат и в тексте источника: числа сверены, значит карточка допущена,
    # и вычищать их обязан именно вид.
    rows = _rows()
    rows[0].snippet += f" {PHONE} {EMAIL} 8 800 555 35 35"
    view = build_scan_view(_report([card]), rows)
    assert len(view.cards) == 1
    payload = scan_payload(view)
    dumped = json.dumps(payload, ensure_ascii=False) + repr(view)
    for secret in (PHONE, EMAIL, "123-45-67", "555 35 35"):
        assert secret not in dumped
    assert CONTACT_HIDDEN in view.cards[0].essence
    assert CONTACT_HIDDEN in view.cards[0].evidence[0].claim
    assert "Затраты ниже на 35%" in view.cards[0].evidence[0].claim


def test_scrub_contacts_не_трогает_годы_и_суммы():
    text = "С 2019 2020 2021 годов, выручка 12 000 000 руб., ГОСТ 12.1.005-88, 35 %"
    assert scrub_contacts(text) == text
    assert scrub_contacts("тел. 123-45-67") == f"тел. {CONTACT_HIDDEN}"
    assert scrub_contacts("a.b+c@x-y.example.com") == CONTACT_HIDDEN


def test_пять_сверенных_карточек_дают_три():
    view = build_scan_view(_report([_card(i, title=f"Решение {i}") for i in range(5)]), _rows())
    assert len(view.cards) == MAX_SCAN_CARDS == 3
    assert [card.title for card in view.cards] == ["Решение 0", "Решение 1", "Решение 2"]
    assert view.cards_note == ""


def test_лимит_отсчитывается_после_сверки():
    bad = [_card(i, unsourced_claims=["не подтверждено"]) for i in range(3)]
    good = [_card(i, title=f"Сверенная {i}") for i in range(3, 6)]
    view = build_scan_view(_report(bad + good), _rows())
    assert [card.title for card in view.cards] == ["Сверенная 3", "Сверенная 4", "Сверенная 5"]


def test_ноль_сверенных_карточек_и_аналогов_не_найдено():
    report = _report([_card(0, evidence=[Evidence(claim="Нет в источниках", url="https://stranger.example.org/")])])
    view = build_scan_view(report, _rows())
    payload = scan_payload(view)
    assert view.cards == () and view.analogs == ()
    assert view.cards_note == view.analogs_note == NOT_FOUND == "не найдено"
    assert payload["cards"] == [] and payload["cards_note"] == NOT_FOUND
    assert payload["analogs"] == [] and payload["analogs_note"] == NOT_FOUND


def test_пустые_поля_печатаются_как_не_найдено():
    view = build_scan_view(_report([_card(0, developer_org=None)], [_analog(1, traction="")]), _rows())
    assert view.cards[0].developer_org == NOT_FOUND
    assert view.analogs[0].traction == NOT_FOUND


def test_аналоги_только_сверенные_и_не_больше_трёх():
    analogs = [_analog(i) for i in range(5)] + [_analog(6, evidence=[])]
    view = build_scan_view(_report(analogs=analogs), _rows())
    assert len(view.analogs) == MAX_SCAN_ANALOGS == 3
    unverified = _analog(0, country_evidence_url="https://stranger.example.org/c")
    assert build_scan_view(_report(analogs=[unverified]), _rows()).analogs == ()


def test_аналог_tw_при_gov_отсутствует_cn_остаётся():
    analogs = [_analog(0, "TW", title="Тайваньский"), _analog(1, "CN", title="Китайский")]
    view = build_scan_view(_report(analogs=analogs, mode="gov"), _rows())
    assert [a.title for a in view.analogs] == ["Китайский"]
    assert "Тайваньский" not in json.dumps(scan_payload(view), ensure_ascii=False)


def test_при_business_тайваньский_аналог_остаётся():
    view = build_scan_view(_report(analogs=[_analog(0, "TW")], mode="business"), _rows())
    assert [a.country for a in view.analogs] == ["TW"]


def test_господдержка_только_со_ссылкой():
    card = _card(
        0,
        gov_support=[
            GovSupportMatch(instrument="Грант А", why="подходит", url="https://support.example.org/a"),
            GovSupportMatch(instrument="Грант Б", why="без ссылки"),
        ],
    )
    view = build_scan_view(_report([card]), _rows())
    assert [(s.instrument, s.url) for s in view.cards[0].gov_support] == [("Грант А", "https://support.example.org/a")]
    assert "подходит" not in json.dumps(scan_payload(view), ensure_ascii=False)


def test_sources_unavailable_показываются():
    view = build_scan_view(_report([_card(0)], sources_unavailable=["Росспатент: источник не ответил"]), _rows())
    assert scan_payload(view)["sources_unavailable"] == ["Росспатент: источник не ответил"]


def test_стадия_зрелости_словами_клиента():
    payload = scan_payload(build_scan_view(_report([_card(0)]), _rows()))
    assert payload["cards"][0]["maturity_stage"] == "стадия не определена"


def test_вид_неизменяем():
    view = build_scan_view(_report([_card(0)]), _rows())
    with pytest.raises(FrozenInstanceError):
        view.cards = ()  # type: ignore[misc]
