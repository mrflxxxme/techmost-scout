"""Секция «Что делают в мире» во всех выходах (Э6-4, РЕШ-244, РЕШ-241).

Один отчёт с аналогами `CN`, `US`, `TW` и одним без подтверждений записан в
базу, режим лежит в `briefs.mode` (в `report_json` его нет: `Brief.mode`
исключён из сериализации). Страница отчёта, markdown, печатный HTML, текст DOCX
и JSON обязаны сойтись: при «Гос» `CN` — в секции, `US` — только сноской, `TW` и
аналога без `evidence` нет в тексте вовсе; при «Бизнес» три страны — в секции.
База — временный файл SQLite, сеть не задействована (инвариант №7).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import main
from app.export.analogs import ANALOG_SECTION_TITLE, analog_fields, analog_sections
from app.export.docx import export_docx
from app.export.json import build_report_payload
from app.export.markdown import render_markdown
from app.export.pdf import render_print_html
from app.export.view import build_export_view
from app.models import Base, RequestStatus
from app.models import Request as RequestRow
from app.schemas import AnalogCard, Brief, Evidence, Report, ReportSummary, SolutionCard
from app.web import brief_store

_AUTH = ("scout", "parol123")

CN_TITLE = "Аналог из Китая"
US_TITLE = "Аналог из США"
TW_TITLE = "Аналог с Тайваня"
BARE_TITLE = "Аналог без ссылок"
TITLES = {"cn": CN_TITLE, "us": US_TITLE, "tw": TW_TITLE, "bare": BARE_TITLE}
URLS = {
    "cn": "https://cn.example.org/proof",
    "us": "https://us.example.org/proof",
    "tw": "https://tw.example.org/proof",
}


@pytest.fixture
async def factory(tmp_path: Path):
    """Фабрика сессий на временном файле SQLite — форма `app.db.session_scope`."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'export-analogs.db'}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    yield scope
    await engine.dispose()


@pytest.fixture(autouse=True)
def _wire_test_db(monkeypatch: pytest.MonkeyPatch, factory):
    """Маршруты страницы идут через `app.db.session_scope` — подмена и есть подключение базы."""
    monkeypatch.setattr("app.db.session_scope", factory)

    async def fake_init_db() -> None:
        return None

    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(main.settings, "web_auth_user", _AUTH[0])
    monkeypatch.setattr(main.settings, "web_auth_password", SecretStr(_AUTH[1]))


def _analog(key: str, country: str, *, with_evidence: bool = True, traction: str = "Сеть из 40 точек") -> AnalogCard:
    url = URLS.get(key, "https://bare.example.org/page")
    return AnalogCard(
        title=TITLES[key],
        essence=f"Суть решения: {TITLES[key]}.",
        country=country,
        country_evidence_url=f"https://country.example.org/{key}",
        business_model="Подписка для предприятий",
        traction=traction,
        transferability_ru="Ограничений по поставкам не установлено",
        what_to_do_in_ru="Изучить условия лицензирования",
        evidence=[Evidence(claim=f"Факт про {TITLES[key]}", url=url)] if with_evidence else [],
        candidate_urls=[url],
    )


def _report(brief: Brief, analogs: list[AnalogCard]) -> Report:
    return Report(
        request_id=1,
        brief=brief,
        cards=[
            SolutionCard(
                title="Основной вариант",
                essence="Основной вариант решения.",
                applicability="Закрывает задачу.",
                next_step="Запросить образец.",
                evidence=[Evidence(claim="Испытан", url="https://example.org/main")],
            )
        ],
        analog_cards=analogs,
        summary=ReportSummary(recommendation="Начать с варианта №1."),
        generated_at=datetime(2026, 10, 18, 9, 0),
    )


def _full_set() -> list[AnalogCard]:
    return [
        _analog("cn", "CN"),
        _analog("us", "US"),
        _analog("tw", "TW"),
        _analog("bare", "CN", with_evidence=False),
    ]


async def _seed(factory, brief: Brief, analogs: list[AnalogCard], *, mode: str | None = "gov") -> int:
    """Отчёт в `requests.report_json`, режим и вид решения — в `briefs` (как в рабочем потоке)."""
    report = _report(brief, analogs)
    async with factory() as session:
        row = RequestRow(
            raw_text="задача",
            status=RequestStatus.VALIDATED,
            cost_rub=0.0,
            report_json=report.model_dump_json(),
        )
        session.add(row)
        await session.flush()
        request_id = int(row.id)
        if mode is not None:
            stored = brief.model_copy(update={"mode": mode, "solution_kind": "product"})
            await brief_store.save_brief(session, request_id, stored, confirmed=False)
    return request_id


async def _view(factory, request_id: int):
    async with factory() as session:
        return await build_export_view(session, request_id)


def _docx_text(view, tmp_path: Path) -> str:
    path = export_docx(view, tmp_path / "report.docx")
    return "\n".join(paragraph.text for paragraph in Document(str(path)).paragraphs)


def _page_text(request_id: int) -> str:
    with TestClient(main.app) as client:
        response = client.get(f"/requests/{request_id}/report", auth=_AUTH)
    assert response.status_code == 200
    return response.text


def _all_outputs(factory, request_id: int, tmp_path: Path) -> dict[str, str]:
    """Пять выходов одного запроса: страница, markdown, печатный HTML, текст DOCX, JSON."""
    view = asyncio.run(_view(factory, request_id))
    return {
        "страница": _page_text(request_id),
        "markdown": render_markdown(view.client_report),
        "печать": render_print_html(view),
        "docx": _docx_text(view, tmp_path),
        "json": json.dumps(build_report_payload(view), ensure_ascii=False),
    }


def _footnote(title: str, country: str, url: str) -> str:
    return f"{title} — {country} — {url}"


def test_гос_cn_в_секции_us_сноской_tw_и_бессылочный_нигде(factory, brief: Brief, tmp_path: Path):
    request_id = asyncio.run(_seed(factory, brief, _full_set(), mode="gov"))

    outputs = _all_outputs(factory, request_id, tmp_path)

    for name, text in outputs.items():
        assert ANALOG_SECTION_TITLE in text or name == "json", name
        assert CN_TITLE in text, f"{name}: Китай должен быть в секции"
        assert URLS["cn"] in text, f"{name}: у Китая нет ссылки"
        assert text.count(US_TITLE) == 1, f"{name}: США — ровно одна строка сноски"
        assert URLS["us"] in text, f"{name}: у сноски США нет ссылки"
        for hidden in (TW_TITLE, BARE_TITLE, URLS["tw"], "bare.example.org", "country.example.org/tw"):
            assert hidden not in text, f"{name}: {hidden!r} не должен печататься"


def test_гос_сноска_одной_строкой_название_страна_ссылка(factory, brief: Brief, tmp_path: Path):
    request_id = asyncio.run(_seed(factory, brief, _full_set(), mode="gov"))
    line = _footnote(US_TITLE, "US", URLS["us"])

    outputs = _all_outputs(factory, request_id, tmp_path)

    assert f"- {line}" in outputs["markdown"]
    assert line in outputs["docx"]
    assert f"{US_TITLE} — US — " in outputs["печать"]
    assert f"{US_TITLE} — US — " in outputs["страница"]
    footnotes = json.loads(outputs["json"])["analog_cards"]["footnotes"]
    assert footnotes == [{"title": US_TITLE, "country": "US", "url": URLS["us"]}]


def test_гос_полная_карточка_только_у_cn(factory, brief: Brief, tmp_path: Path):
    """У сноски нет сути и бизнес-модели: это одна строка, а не карточка."""
    request_id = asyncio.run(_seed(factory, brief, _full_set(), mode="gov"))

    outputs = _all_outputs(factory, request_id, tmp_path)

    for name, text in outputs.items():
        assert f"Суть решения: {CN_TITLE}." in text, name
        assert f"Суть решения: {US_TITLE}." not in text, name
    payload = json.loads(outputs["json"])["analog_cards"]
    assert [card["title"] for card in payload["main"]] == [CN_TITLE]
    assert all("candidate_urls" not in card for card in payload["main"])


def test_бизнес_все_три_страны_со_ссылками_в_секции(factory, brief: Brief, tmp_path: Path):
    request_id = asyncio.run(_seed(factory, brief, _full_set(), mode="business"))

    outputs = _all_outputs(factory, request_id, tmp_path)

    for name, text in outputs.items():
        for key in ("cn", "us", "tw"):
            assert TITLES[key] in text, f"{name}: {TITLES[key]} должен быть в секции"
            assert URLS[key] in text, f"{name}: у {TITLES[key]} нет ссылки"
            assert f"Суть решения: {TITLES[key]}." in text, f"{name}: {TITLES[key]} без полной карточки"
        assert BARE_TITLE not in text, name
    payload = json.loads(outputs["json"])["analog_cards"]
    assert [card["title"] for card in payload["main"]] == [CN_TITLE, US_TITLE, TW_TITLE]
    assert payload["footnotes"] == []


def test_режим_берётся_из_briefs_а_не_из_умолчания_report_json(factory, brief: Brief):
    """В `report_json` режима нет: без наложения полей `briefs` Тайвань прошёл бы в «Гос»."""
    request_id = asyncio.run(_seed(factory, brief, _full_set(), mode="gov"))

    view = asyncio.run(_view(factory, request_id))

    assert view.report.brief.mode == "gov"
    assert view.client_report.brief.mode == "gov"
    assert view.report.brief.solution_kind == "product"
    assert view.client_report.brief.solution_kind == "product"
    assert [card.title for card in view.analog_sections.main] == [CN_TITLE]
    assert [note.title for note in view.analog_sections.footnotes] == [US_TITLE]


def test_строка_briefs_без_режима_читается_как_бизнес(factory, brief: Brief):
    """Строки, записанные до РЕШ-250, режима не несут: умолчание — `business`."""
    request_id = asyncio.run(_seed(factory, brief, _full_set(), mode=None))

    view = asyncio.run(_view(factory, request_id))

    assert view.client_report.brief.mode == "business"
    assert [card.title for card in view.analog_sections.main] == [CN_TITLE, US_TITLE, TW_TITLE]


def test_старый_отчёт_без_аналогов_секции_не_имеет(factory, brief: Brief, tmp_path: Path):
    request_id = asyncio.run(_seed(factory, brief, [], mode="gov"))

    outputs = _all_outputs(factory, request_id, tmp_path)

    for name, text in outputs.items():
        if name != "json":
            assert ANALOG_SECTION_TITLE not in text, name
    assert json.loads(outputs["json"])["analog_cards"] == {"main": [], "footnotes": []}
    assert json.loads(outputs["json"])["schema_version"] == "1.0"


def test_секция_после_основных_карточек(factory, brief: Brief, tmp_path: Path):
    request_id = asyncio.run(_seed(factory, brief, _full_set(), mode="business"))

    outputs = _all_outputs(factory, request_id, tmp_path)

    for name in ("страница", "markdown", "печать", "docx"):
        text = outputs[name]
        assert text.index("Основной вариант") < text.index(ANALOG_SECTION_TITLE), name


def test_разбор_исключение_сильнее_сноски_и_бессылочный_не_печатается(brief: Brief):
    gov = brief.model_copy(update={"mode": "gov"})
    report = _report(gov, _full_set())

    sections = analog_sections(report)

    assert [card.country for card in sections.main] == ["CN"]
    assert [note.country for note in sections.footnotes] == ["US"]
    assert analog_sections(report.model_copy(update={"brief": brief})).footnotes == ()


def test_пустое_распространение_печатается_как_не_найдено():
    fields = dict(analog_fields(_analog("cn", "CN", traction="  ")))

    assert fields["Распространение"] == "не найдено"
