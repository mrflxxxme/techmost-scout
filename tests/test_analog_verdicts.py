"""Поштучный вердикт эксперта на зарубежный аналог (Э6-7, РЕШ-277).

Отчёт с тремя аналогами (`CN`, `US`, `DE`, у всех `evidence`) записан в базу,
режим «Гос» лежит в `briefs.mode`: `CN` — полная карточка, `US` и `DE` — сноски.
Вердикт `reject` через `POST` обязан убрать аналог из всех выходов сразу —
страницы отчёта, печатного HTML, текста DOCX, JSON-выгрузки, `/api/v1` и
markdown клиентского вида: видимость считает одна `verdicts.hidden_reason`.
База — временный файл SQLite, сеть не задействована (инвариант №7).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import main
from app.config import get_settings
from app.export.docx import export_docx
from app.export.json import export_report_json
from app.export.markdown import render_markdown
from app.export.pdf import render_print_html
from app.export.view import build_export_view
from app.models import AnalogVerdict as AnalogVerdictRow
from app.models import Base, RequestStatus
from app.models import Request as RequestRow
from app.models import SolutionCard as SolutionCardRow
from app.schemas import AnalogCard, Brief, Evidence, Report, ReportSummary, SolutionCard
from app.verdicts import (
    all_cards_reviewed,
    analog_key,
    effective_analog_verdicts,
    hidden_reason,
    visible_analogs,
)
from app.web import api_access, brief_store

_AUTH = ("scout", "parol123")
_API_KEY = "test-key-alpha-0000"

TITLES = {"cn": "Аналог из Китая", "us": "Аналог из США", "de": "Аналог из Германии"}
COUNTRIES = {"cn": "CN", "us": "US", "de": "DE"}
PROOF = {key: f"https://{key}.example.org/proof" for key in TITLES}


@pytest.fixture
async def factory(tmp_path: Path):
    """Фабрика сессий на временном файле SQLite — форма `app.db.session_scope`."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'analog-verdicts.db'}", future=True)
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

    scope.engine = engine  # type: ignore[attr-defined]
    yield scope
    await engine.dispose()


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, factory):
    """Маршруты идут через `app.db.session_scope` — подмена и есть подключение базы."""
    monkeypatch.setattr("app.db.session_scope", factory)

    async def fake_init_db() -> None:
        return None

    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(main.settings, "web_auth_user", _AUTH[0])
    monkeypatch.setattr(main.settings, "web_auth_password", SecretStr(_AUTH[1]))
    monkeypatch.setenv("SCOUT_API_KEYS", f"alpha:{_API_KEY}")
    get_settings.cache_clear()
    api_access.limiter.reset()
    yield
    api_access.limiter.reset()
    get_settings.cache_clear()


def _analog(key: str, *, with_evidence: bool = True) -> AnalogCard:
    return AnalogCard(
        title=TITLES[key],
        essence=f"Суть решения: {TITLES[key]}.",
        country=COUNTRIES[key],
        country_evidence_url=f"https://country.example.org/{key}",
        business_model="Подписка для предприятий",
        traction="Сеть из 40 точек",
        transferability_ru="Ограничений по поставкам не установлено",
        what_to_do_in_ru="Изучить условия лицензирования",
        evidence=[Evidence(claim=f"Факт про {TITLES[key]}", url=PROOF[key])] if with_evidence else [],
        candidate_urls=[PROOF[key]],
    )


async def _seed(factory, brief: Brief, *, status: RequestStatus = RequestStatus.VALIDATED, analogs=None) -> int:
    """Отчёт с аналогами и одной карточкой в `requests`, режим «Гос» — в `briefs`."""
    cards = analogs if analogs is not None else [_analog("cn"), _analog("us"), _analog("de")]
    main_card = SolutionCard(
        title="Основной вариант",
        essence="Основной вариант решения.",
        applicability="Закрывает задачу.",
        next_step="Запросить образец.",
        evidence=[Evidence(claim="Испытан", url="https://example.org/main")],
    )
    report = Report(
        request_id=1,
        brief=brief,
        cards=[main_card],
        analog_cards=cards,
        summary=ReportSummary(recommendation="Начать с варианта №1."),
        generated_at=datetime(2026, 10, 21, 9, 0),
    )
    async with factory() as session:
        row = RequestRow(
            raw_text="задача",
            status=status,
            cost_rub=0.0,
            api_client="alpha",
            report_json=report.model_dump_json(),
        )
        session.add(row)
        await session.flush()
        session.add(
            SolutionCardRow(
                request_id=row.id,
                position=0,
                title=main_card.title,
                essence=main_card.essence,
                maturity_stage=main_card.maturity_stage.value,
                applicability=main_card.applicability,
                next_step=main_card.next_step,
                evidence=[item.model_dump(mode="json") for item in main_card.evidence],
            )
        )
        stored = brief.model_copy(update={"mode": "gov", "solution_kind": "product"})
        await brief_store.save_brief(session, row.id, stored, confirmed=False)
        return int(row.id)


def _run(coro):
    return asyncio.run(coro)


async def _view(factory, request_id: int):
    async with factory() as session:
        return await build_export_view(session, request_id)


async def _rows(factory) -> int:
    async with factory() as session:
        return int((await session.execute(select(func.count()).select_from(AnalogVerdictRow))).scalar_one())


async def _status(factory, request_id: int) -> str:
    async with factory() as session:
        return str((await session.get(RequestRow, request_id)).status)


def _url(request_id: int, card: AnalogCard | str) -> str:
    key = card if isinstance(card, str) else analog_key(card)
    return f"/requests/{request_id}/analogs/{key}/verdict"


def _post(request_id: int, card: AnalogCard | str, decision: str, *, name: str = "Мария Петрова", comment: str = ""):
    with TestClient(main.app) as client:
        return client.post(
            _url(request_id, card),
            data={"decision": decision, "reviewer_name": name, "comment": comment},
            auth=_AUTH,
        )


def _outputs(factory, request_id: int, tmp_path: Path) -> dict[str, str]:
    """Шесть выходов: страница, markdown, печать, DOCX, JSON-выгрузка и `/api/v1`."""
    view = _run(_view(factory, request_id))
    docx_path = export_docx(view, tmp_path / "report.docx")

    async def exported() -> dict:
        async with factory() as session:
            return await export_report_json(session, request_id)

    with TestClient(main.app) as client:
        page = client.get(f"/requests/{request_id}/report", auth=_AUTH)
        api = client.get(f"/api/v1/requests/{request_id}/report", headers={"Authorization": f"Bearer {_API_KEY}"})
    assert page.status_code == 200 and api.status_code == 200
    return {
        "страница": page.text,
        "markdown": render_markdown(view.client_report),
        "печать": render_print_html(view),
        "docx": "\n".join(p.text for p in Document(str(docx_path)).paragraphs),
        "json": json.dumps(_run(exported()), ensure_ascii=False),
        "api": api.text,
    }


def _assert_hidden(outputs: dict[str, str], *keys: str) -> None:
    for name, text_ in outputs.items():
        for key in keys:
            assert TITLES[key] not in text_, f"{name}: {TITLES[key]} должен быть скрыт"
            assert PROOF[key] not in text_, f"{name}: ссылка {PROOF[key]} должна быть скрыта"


def _assert_shown(outputs: dict[str, str], *keys: str) -> None:
    for name, text_ in outputs.items():
        for key in keys:
            assert TITLES[key] in text_, f"{name}: {TITLES[key]} должен быть виден"
            assert PROOF[key] in text_, f"{name}: нет ссылки {PROOF[key]}"


def test_до_вердиктов_видны_все_три_аналога(factory, brief: Brief, tmp_path: Path):
    request_id = _run(_seed(factory, brief))

    outputs = _outputs(factory, request_id, tmp_path)

    _assert_shown(outputs, "cn", "us", "de")
    for name, text_ in outputs.items():
        assert text_.count(TITLES["us"]) == 1, f"{name}: США — одна строка сноски"


def test_reject_на_cn_и_us_убирает_их_из_всех_выходов_de_остаётся_сноской(factory, brief: Brief, tmp_path: Path):
    request_id = _run(_seed(factory, brief))

    assert _post(request_id, _analog("cn"), "reject").status_code == 200
    assert _post(request_id, _analog("us"), "reject", comment="нет данных").status_code == 200

    outputs = _outputs(factory, request_id, tmp_path)
    _assert_hidden(outputs, "cn", "us")
    _assert_shown(outputs, "de")
    for name, text_ in outputs.items():
        assert text_.count(TITLES["de"]) == 1, f"{name}: Германия — одна строка сноски"
    assert f"{TITLES['de']} — DE — {PROOF['de']}" in outputs["markdown"]
    assert _run(_rows(factory)) == 2


def test_поздний_approve_возвращает_аналог(factory, brief: Brief, tmp_path: Path):
    request_id = _run(_seed(factory, brief))
    _post(request_id, _analog("cn"), "reject")
    _assert_hidden(_outputs(factory, request_id, tmp_path), "cn")

    assert _post(request_id, _analog("cn"), "approve").status_code == 200

    _assert_shown(_outputs(factory, request_id, tmp_path), "cn")
    assert _run(_rows(factory)) == 2, "прежняя строка не удалена и не обновлена"


def test_rework_аналог_не_скрывает(factory, brief: Brief, tmp_path: Path):
    request_id = _run(_seed(factory, brief))

    assert _post(request_id, _analog("cn"), "rework").status_code == 200

    _assert_shown(_outputs(factory, request_id, tmp_path), "cn", "us", "de")


def test_вердикт_одного_аналога_не_задевает_другие(factory, brief: Brief):
    request_id = _run(_seed(factory, brief))
    _post(request_id, _analog("cn"), "reject")

    view = _run(_view(factory, request_id))

    assert [card.title for card in view.client_report.analog_cards] == [TITLES["us"], TITLES["de"]]
    assert view.analog_sections.main == ()


@pytest.mark.parametrize("key", ["0" * 16, "abc", "A" * 16, "z" * 16, "0" * 17])
def test_неизвестный_и_кривой_ключ_404_без_записи(factory, brief: Brief, key: str):
    request_id = _run(_seed(factory, brief))

    response = _post(request_id, key, "reject")

    assert response.status_code == 404
    assert _run(_rows(factory)) == 0


def test_несуществующий_запрос_404_без_записи(factory, brief: Brief):
    request_id = _run(_seed(factory, brief))

    response = _post(request_id + 100, _analog("cn"), "reject")

    assert response.status_code == 404
    assert _run(_rows(factory)) == 0


def test_ключ_чужого_запроса_404(factory, brief: Brief):
    first = _run(_seed(factory, brief))
    second = _run(_seed(factory, brief, analogs=[_analog("de")]))

    assert _post(second, _analog("cn"), "reject").status_code == 404
    assert _post(first, _analog("cn"), "reject").status_code == 200
    assert _run(_rows(factory)) == 1


@pytest.mark.parametrize("name", ["", "   "])
def test_пустое_имя_400_без_записи(factory, brief: Brief, name: str):
    request_id = _run(_seed(factory, brief))

    response = _post(request_id, _analog("cn"), "reject", name=name)

    assert response.status_code == 400
    assert _run(_rows(factory)) == 0


def test_неизвестное_решение_400_без_записи(factory, brief: Brief):
    request_id = _run(_seed(factory, brief))

    response = _post(request_id, _analog("cn"), "delete")

    assert response.status_code == 400
    assert _run(_rows(factory)) == 0


def test_вердикты_аналогов_не_меняют_статус_запроса(factory, brief: Brief):
    request_id = _run(_seed(factory, brief, status=RequestStatus.DRAFTED))

    for key in ("cn", "us", "de"):
        assert _post(request_id, _analog(key), "approve").status_code == 200

    assert _run(_status(factory, request_id)) == RequestStatus.DRAFTED
    view = _run(_view(factory, request_id))
    assert view.is_draft is True
    assert view.reviewer_name == "", "вердикты аналогов не дают подписи"
    assert all_cards_reviewed({(False, 0): 1}, {}) is False


def test_валидированный_запрос_остаётся_валидированным(factory, brief: Brief):
    request_id = _run(_seed(factory, brief))

    _post(request_id, _analog("cn"), "reject")

    assert _run(_status(factory, request_id)) == RequestStatus.VALIDATED
    assert _run(_view(factory, request_id)).is_draft is False


def test_экран_ревью_показывает_все_аналоги_и_причину_у_отклонённых(factory, brief: Brief):
    request_id = _run(_seed(factory, brief))
    _post(request_id, _analog("cn"), "reject")
    _post(request_id, _analog("us"), "reject")

    with TestClient(main.app) as client:
        response = client.get(f"/requests/{request_id}/review", auth=_AUTH)

    assert response.status_code == 200
    for key in TITLES:
        assert TITLES[key] in response.text
        assert f"/requests/{request_id}/analogs/{analog_key(_analog(key))}/verdict" in response.text
    assert response.text.count("отклонена экспертом") == 2
    assert "Оценка (1–5)" not in response.text.split("Что делают в мире")[1]


def test_экран_ревью_без_аналогов_секции_не_имеет(factory, brief: Brief):
    request_id = _run(_seed(factory, brief, analogs=[]))

    with TestClient(main.app) as client:
        response = client.get(f"/requests/{request_id}/review", auth=_AUTH)

    assert response.status_code == 200
    assert "Что делают в мире" not in response.text


def test_бессылочный_аналог_скрыт_тем_же_проходом_и_объяснён_на_экране(factory, brief: Brief):
    bare = _analog("de", with_evidence=False)
    request_id = _run(_seed(factory, brief, analogs=[_analog("cn"), bare]))

    view = _run(_view(factory, request_id))
    with TestClient(main.app) as client:
        page = client.get(f"/requests/{request_id}/review", auth=_AUTH).text

    assert [card.title for card in view.client_report.analog_cards] == [TITLES["cn"]]
    assert TITLES["de"] in page
    assert "нет ни одного подтверждения" in page


def test_таблица_создаётся_init_db_в_базе_без_неё(monkeypatch: pytest.MonkeyPatch, factory):
    import app.db as db_module

    engine = factory.engine
    probe = text("SELECT name FROM sqlite_master WHERE name='analog_verdicts'")

    async def scenario() -> tuple[bool, bool]:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE analog_verdicts"))
        async with engine.connect() as conn:
            before = bool((await conn.execute(probe)).all())
        monkeypatch.setattr(db_module, "engine", engine)
        await db_module.init_db()
        async with engine.connect() as conn:
            after = bool((await conn.execute(probe)).all())
        return before, after

    assert _run(scenario()) == (False, True)


# ── Чистые функции слоя вердиктов ─────────────────────────────────────────────
def test_ключ_аналога_стабилен_16_hex_и_зависит_от_полей_отпечатка():
    card = _analog("cn")

    assert analog_key(card) == analog_key(card.model_copy(deep=True))
    assert len(analog_key(card)) == 16 and int(analog_key(card), 16) >= 0
    assert analog_key(card) != analog_key(card.model_copy(update={"title": "Другое"}))
    assert analog_key(card) != analog_key(card.model_copy(update={"country": "US"}))
    other_url = card.model_copy(update={"country_evidence_url": "https://country.example.org/other"})
    assert analog_key(card) != analog_key(other_url)
    # Суть и подтверждения в отпечаток не входят: правка текста ключ не меняет.
    assert analog_key(card) == analog_key(card.model_copy(update={"essence": "Новая суть"}))


def _row(key: str, decision: str, created_at: datetime, row_id: int) -> AnalogVerdictRow:
    return AnalogVerdictRow(
        id=row_id, request_id=1, analog_key=key, reviewer_name="Эксперт", decision=decision, created_at=created_at
    )


def test_действующий_вердикт_аналога_последний_по_времени_и_id():
    moment = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
    rows = [
        _row("a" * 16, "reject", moment, 2),
        _row("a" * 16, "approve", moment + timedelta(minutes=1), 3),
        _row("a" * 16, "reject", moment - timedelta(minutes=1), 4),
        _row("b" * 16, "approve", moment, 5),
        _row("b" * 16, "reject", moment, 6),
    ]

    effective = effective_analog_verdicts(rows)

    assert effective["a" * 16].decision == "approve"
    assert effective["b" * 16].decision == "reject", "при равном времени — больший id"
    assert effective_analog_verdicts(list(reversed(rows)))["a" * 16].decision == "approve"


def test_visible_analogs_зовёт_hidden_reason_и_сохраняет_порядок():
    cn, us, bare = _analog("cn"), _analog("us"), _analog("de", with_evidence=False)
    moment = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
    effective = {analog_key(us): _row(analog_key(us), "reject", moment, 1)}

    assert [card.title for card in visible_analogs([cn, us, bare], effective)] == [cn.title]
    assert [card.title for card in visible_analogs([cn, us, bare], effective, require_evidence=False)] == [
        cn.title,
        bare.title,
    ]
    assert hidden_reason(us, effective[analog_key(us)]) == "rejected"
    assert hidden_reason(bare, None, require_evidence=True) == "no_evidence"
    rework = {analog_key(cn): _row(analog_key(cn), "rework", moment, 2)}
    assert visible_analogs([cn], rework) == [cn]
