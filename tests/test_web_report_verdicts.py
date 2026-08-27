"""Отчёт с учётом вердиктов эксперта (Э4-И2): фильтр reject, подпись, статус.

Отдельный файл, а не дописывание в `tests/test_web_report.py` (465 строк из
500) — правило задачи. Фикстуры дублируют `test_web_report.py` по форме
(временная SQLite на `tmp_path`, подмена `app.db.session_scope`), сеть не
задействована (инвариант №7).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import main
from app.models import Base, RequestStatus
from app.models import Request as RequestRow
from app.models import SolutionCard as SolutionCardRow
from app.models import Verdict as VerdictRow
from app.schemas import Evidence, Report, ReportSummary, SolutionCard
from app.schemas import Verdict as VerdictDecision

_AUTH = ("scout", "parol123")


@pytest.fixture
async def db_session_factory(tmp_path: Path):
    """Фабрика сессий на временном файле SQLite — форма `app.db.session_scope`."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'report-verdicts-test.db'}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    yield factory
    await engine.dispose()


@pytest.fixture(autouse=True)
def _wire_test_db(monkeypatch: pytest.MonkeyPatch, db_session_factory):
    """Тот же приём, что в `tests/test_web_report.py`: подмена `session_scope`
    и данных доступа под временную базу запроса."""
    monkeypatch.setattr("app.db.session_scope", db_session_factory)

    async def fake_init_db() -> None:
        return None

    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(main.settings, "web_auth_user", _AUTH[0])
    monkeypatch.setattr(main.settings, "web_auth_password", SecretStr(_AUTH[1]))
    yield db_session_factory


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


def _make_card(**overrides) -> SolutionCard:
    fields = {
        "title": "Катализатор с присадкой молибдена",
        "essence": "Присадка продлевает межрегенерационный цикл катализатора гидроочистки.",
        "applicability": "Закрывает задачу продления срока службы катализатора.",
        "next_step": "Запросить протокол испытаний.",
        "evidence": [Evidence(claim="Испытан на действующей установке", url="https://example.org/a")],
    }
    fields.update(overrides)
    return SolutionCard(**fields)


def _make_report(cards: list[SolutionCard], **overrides) -> Report:
    fields = {
        "request_id": 1,
        "brief": overrides.pop("brief"),
        "cards": cards,
        "contest_cards": [],
        "summary": ReportSummary(recommendation="Начать с варианта №1."),
        "generated_at": datetime(2026, 8, 27, 9, 0),
    }
    fields.update(overrides)
    return Report(**fields)


async def _seed(session_factory, *, report: Report, status: RequestStatus) -> tuple[int, list[int]]:
    async with session_factory() as session:
        row = RequestRow(
            raw_text="катализатор теряет активность раньше срока",
            status=status,
            cost_rub=report.cost_rub,
            report_json=report.model_dump_json(),
        )
        session.add(row)
        await session.flush()
        request_id = int(row.id)
        card_ids: list[int] = []
        for position, card in enumerate(report.cards):
            card_row = SolutionCardRow(
                request_id=request_id,
                position=position,
                is_contest=False,
                title=card.title,
                essence=card.essence,
                maturity_stage=card.maturity_stage.value,
                applicability=card.applicability,
                next_step=card.next_step,
                evidence=[item.model_dump(mode="json") for item in card.evidence],
                confidence=card.confidence.value,
            )
            session.add(card_row)
            await session.flush()
            card_ids.append(int(card_row.id))
    return request_id, card_ids


async def _add_verdict(session_factory, card_id: int, decision: VerdictDecision, *, reviewer_name: str) -> None:
    async with session_factory() as session:
        session.add(VerdictRow(card_id=card_id, reviewer_name=reviewer_name, decision=decision.value))


def test_черновик_на_странице_drafted(client, db_session_factory, brief):
    """На странице `drafted` — плашка ЧЕРНОВИК, подписи ещё нет."""
    report = _make_report([_make_card()], brief=brief)
    request_id, _ = asyncio.run(_seed(db_session_factory, report=report, status=RequestStatus.DRAFTED))

    with client:
        response = client.get(f"/requests/{request_id}/report", auth=_AUTH)

    assert "ЧЕРНОВИК — не проверено экспертом" in response.text
    assert "проверено:" not in response.text


def test_подпись_и_отсутствие_черновика_после_validated(client, db_session_factory, brief):
    """После вердикта и статуса `validated` — строка подписи, плашки ЧЕРНОВИК нет."""
    report = _make_report([_make_card()], brief=brief)
    request_id, card_ids = asyncio.run(_seed(db_session_factory, report=report, status=RequestStatus.VALIDATED))
    asyncio.run(_add_verdict(db_session_factory, card_ids[0], VerdictDecision.APPROVE, reviewer_name="Иван Иванов"))

    with client:
        response = client.get(f"/requests/{request_id}/report", auth=_AUTH)

    assert "ЧЕРНОВИК — не проверено экспертом" not in response.text
    assert "Подготовлено системой ТехМост Скаут, проверено: Иван Иванов," in response.text


def test_reject_карточка_не_видна_approve_видна(client, db_session_factory, brief):
    """Заголовка reject-карточки в HTML нет, заголовок approve-карточки есть."""
    approved = _make_card(title="Одобренная карточка")
    rejected = _make_card(title="Отклонённая карточка")
    report = _make_report([approved, rejected], brief=brief)
    request_id, card_ids = asyncio.run(_seed(db_session_factory, report=report, status=RequestStatus.VALIDATED))
    asyncio.run(_add_verdict(db_session_factory, card_ids[0], VerdictDecision.APPROVE, reviewer_name="Иван Иванов"))
    asyncio.run(_add_verdict(db_session_factory, card_ids[1], VerdictDecision.REJECT, reviewer_name="Иван Иванов"))

    with client:
        response = client.get(f"/requests/{request_id}/report", auth=_AUTH)

    assert "Одобренная карточка" in response.text
    assert "Отклонённая карточка" not in response.text


def test_edit_после_validated_возвращает_303_и_статус_не_меняется(client, db_session_factory, brief):
    """`POST /cards/{id}/edit` при `validated` — 303, `requests.status` не сбрасывается (РЕШ-196 п. 6)."""
    report = _make_report([_make_card()], brief=brief)
    request_id, card_ids = asyncio.run(_seed(db_session_factory, report=report, status=RequestStatus.VALIDATED))

    with client:
        response = client.post(
            f"/cards/{card_ids[0]}/edit",
            data={"title": "Правка после валидации"},
            auth=_AUTH,
            follow_redirects=False,
        )

    assert response.status_code == 303

    async def _status() -> str:
        async with db_session_factory() as session:
            row = (await session.execute(select(RequestRow).where(RequestRow.id == request_id))).scalars().one()
            return str(row.status)

    assert asyncio.run(_status()) == RequestStatus.VALIDATED.value


def test_подсказка_карточка_уже_проверена_после_validated(client, db_session_factory, brief):
    """У формы правки при `validated` — подсказка «карточка уже проверена экспертом»."""
    report = _make_report([_make_card()], brief=brief)
    request_id, card_ids = asyncio.run(_seed(db_session_factory, report=report, status=RequestStatus.VALIDATED))
    asyncio.run(_add_verdict(db_session_factory, card_ids[0], VerdictDecision.APPROVE, reviewer_name="Иван Иванов"))

    with client:
        response = client.get(f"/requests/{request_id}/report", auth=_AUTH)

    assert "Карточка уже проверена экспертом." in response.text
