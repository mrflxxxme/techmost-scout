"""Подсказка эксперту на экране ревью берёт причину скрытия из `verdicts`
(Э4-П13, РЕШ-215).

`web/routes/review.py::_hidden_reason` зовёт публичную `app.verdicts.
hidden_reason` напрямую — единственный источник причины скрытия карточки в
проекте. Единственный тест, который действительно закрывает риск «подсказка
считает сама, а не спрашивает `verdicts`», — показать, что мутация общего
условия видимости в `verdicts` меняет подсказку на экране, а не подтвердить
текст подсказки на неизменном коде (это уже делает `tests/test_web_review.py::
test_summary_и_экран_ревью_честны_с_экспертом`). Мутация подменяет
**публичную** `verdicts.hidden_reason` (а не приватную `_kept_positions`,
как было в предыдущей версии этого теста, ревью Э4-П12, совет 2) — страж не
краснеет от безобидного переименования внутреннего устройства модуля.

Файл отдельный, а не расширение `tests/test_web_review.py` — тот уже на
пределе 500 строк (страж `tests/test_tests_layout.py`). Фикстуры дублируют
`tests/test_web_review.py` по образцу, уже принятому в проекте
(`tests/test_summary_field_gate.py` дублирует фикстуры `tests/
test_summary_overrides.py` тем же приёмом — файлы независимы, дублирование
дешевле связывания). Сеть не задействована (инвариант №7).
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import main
from app.config import get_settings
from app.models import Base, RequestStatus
from app.models import Request as RequestRow
from app.models import SolutionCard as SolutionCardRow
from app.schemas import Evidence, Report, ReportSummary, SolutionCard
from app.schemas import Verdict as VerdictDecision
from app.web.routes import review as review_module

_AUTH = ("scout", "parol123")


@pytest.fixture
async def db_session_factory(tmp_path: Path):
    """Фабрика сессий на временном файле SQLite — форма `app.db.session_scope`."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'review-visibility-source-test.db'}", future=True)
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
    """Подмена `session_scope` и Basic auth под временную базу запроса."""
    monkeypatch.setattr("app.db.session_scope", db_session_factory)

    async def fake_init_db() -> None:
        return None

    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(main.settings, "web_auth_user", _AUTH[0])
    monkeypatch.setattr(main.settings, "web_auth_password", SecretStr(_AUTH[1]))
    monkeypatch.setattr(get_settings(), "reviewer_name", "Мария Петрова")
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


def _make_report(cards: list[SolutionCard], brief) -> Report:
    return Report(
        request_id=1,
        brief=brief,
        cards=cards,
        contest_cards=[],
        summary=ReportSummary(recommendation="Начать с варианта №1."),
        generated_at=datetime(2026, 8, 27, 9, 0),
    )


async def _seed(session_factory, *, report: Report) -> int:
    async with session_factory() as session:
        row = RequestRow(
            raw_text="катализатор теряет активность раньше срока",
            status=RequestStatus.DRAFTED,
            report_json=report.model_dump_json(),
        )
        session.add(row)
        await session.flush()
        request_id = int(row.id)
        for position, card in enumerate(report.cards):
            session.add(
                SolutionCardRow(
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
            )
        await session.flush()
    return request_id


def _reject_only_hidden_reason(card, verdict, *, require_evidence=False):
    """Заглушка публичной `verdicts.hidden_reason` без ветки `require_evidence`.

    Мутация общего условия видимости: карточка без единого подтверждения
    больше не считается скрытой — остаётся только признак `reject`. Патчится
    имя, под которым `review.py` вызывает функцию (`from app.verdicts import
    hidden_reason`, PEP 8 стандартная точка подмены импортированного имени —
    патчить нужно там, где имя используется, а не там, где оно определено).
    Так же ведёт себя правка самого тела `hidden_reason` в `verdicts.py`:
    и `_kept_positions` (клиентский экспорт), и `_hidden_reason` (этот экран)
    вызывают ровно один объект функции — эта подмена и прямая правка условия
    в `verdicts.py` дают на этом экране одинаковый результат (проверено
    прогоном отдельной мутации, см. handoff задачи).
    """
    if verdict is not None and verdict.decision == VerdictDecision.REJECT:
        return "rejected"
    return None


def test_подсказка_экрана_ревью_меняется_вместе_с_общим_признаком_verdicts(
    client, db_session_factory, brief, monkeypatch
):
    """Э4-П13 (РЕШ-215): `_hidden_reason` не считает причину скрытия сама, а
    берёт её у публичной `verdicts.hidden_reason` — единственного источника
    причины в проекте. Мутация общего условия — здесь: `hidden_reason` без
    ветки `require_evidence` — обязана убрать подсказку «нет ни одного
    подтверждения» с экрана ревью. Если подсказка не изменится, `_hidden_
    reason` — снова своя копия условия, а не общий признак."""
    bare = _make_card(title="Присадка молибдена", evidence=[])
    report = _make_report([bare], brief)
    request_id = asyncio.run(_seed(db_session_factory, report=report))

    with client:
        before = client.get(f"/requests/{request_id}/review", auth=_AUTH)

    assert "клиенту не уедет: нет ни одного подтверждения" in before.text

    monkeypatch.setattr(review_module, "hidden_reason", _reject_only_hidden_reason)

    with client:
        after = client.get(f"/requests/{request_id}/review", auth=_AUTH)

    assert "клиенту не уедет: нет ни одного подтверждения" not in after.text
