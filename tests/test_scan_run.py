"""Фоновый экспресс-скан (Э6-6в): интейк без вопросов и урезанный прогон.

Интейк, дозаполнение ограничений и пайплайн подменены; модели и сеть не
вызываются ни разу (инвариант №7). База — временный файл SQLite, подмена
`app.db.session_scope` одна на маршруты и фоновую задачу.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, ClassVar

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.llm import CostLimitExceededError
from app.models import Base, RequestStatus
from app.models import Brief as BriefRow
from app.models import Request as RequestRow
from app.pipeline.intake_flow import IntakeStageOutcome
from app.schemas import Brief
from app.web import progress, scan_run
from app.web.routes import run

_TASK = "Катализатор гидроочистки теряет активность за 8 месяцев вместо 24."


class _FakePipeline:
    """Заглушка `ScoutPipeline`: запоминает настройки, роутер и бриф, пишет `drafted`."""

    calls: ClassVar[list[dict[str, Any]]] = []
    fail: ClassVar[bool] = False

    def __init__(self, *, session_factory=None, progress=None, router=None, settings=None, **_: Any) -> None:
        self._session_factory = session_factory
        self._router = router
        self._settings = settings

    async def run(self, brief: Brief, *, request_id: int = 0, **kwargs: Any):
        type(self).calls.append(
            {
                "brief": brief,
                "request_id": request_id,
                "settings": self._settings,
                "router_limit": self._router.cost.limit_rub,
                "router_spent": self._router.cost.spent_rub,
                **kwargs,
            }
        )
        async with self._session_factory() as session:
            row = await session.get(RequestRow, request_id)
            if type(self).fail:
                # Как настоящий `ScoutPipeline.run`: сам пишет `failed` и пробрасывает отказ.
                row.status = RequestStatus.FAILED
                row.error = "секретная причина отказа"
            else:
                row.status = RequestStatus.DRAFTED
        if type(self).fail:
            raise RuntimeError("секретная причина отказа")

    async def aclose(self) -> None:
        return None


class _Intake:
    """Заглушка интейка: запоминает вызовы, возвращает бриф с заданным видом модели."""

    calls: ClassVar[list[dict[str, Any]]] = []
    model_kind: ClassVar[str] = "research"
    stop: ClassVar[bool] = False

    @classmethod
    async def run(cls, router, task, *, industry_hint, session_factory, stage_log, solution_kind="research"):
        cls.calls.append({"limit": router.cost.limit_rub, "task": task, "solution_kind": solution_kind})
        if cls.stop:
            raise CostLimitExceededError("предел исчерпан")
        brief = Brief(industry=industry_hint, task_summary=task[:60], mode="gov", solution_kind=cls.model_kind)
        return IntakeStageOutcome(intake=None, brief=brief, caveats=[], warning=None)


async def _fake_constraints(router, brief, task, *, industry_hint, session_factory, stage_log):
    return brief, False


@pytest.fixture(autouse=True)
def _scan_env(monkeypatch: pytest.MonkeyPatch):
    for name in ("SCAN_COST_LIMIT_RUB", "COST_LIMIT_RUB_PER_REQUEST", "TARGET_CARDS"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    run._active_runs.clear()
    monkeypatch.setattr(run, "_start_lock", asyncio.Lock())
    monkeypatch.setattr(progress, "_broker", progress.ProgressBroker())
    _FakePipeline.calls = []
    _FakePipeline.fail = False
    _Intake.calls = []
    _Intake.model_kind = "research"
    _Intake.stop = False
    monkeypatch.setattr(run, "ScoutPipeline", _FakePipeline)
    monkeypatch.setattr(scan_run, "run_intake_stage", _Intake.run)
    monkeypatch.setattr(scan_run, "resolve_constraints_stage", _fake_constraints)
    yield
    run._active_runs.clear()
    get_settings.cache_clear()


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'scan-run.db'}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    @asynccontextmanager
    async def factory():
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr("app.db.session_scope", factory)
    yield factory
    await engine.dispose()


async def _scan(db, kind: str = "product") -> int:
    request_id, _ = await scan_run.create_scan_request(_TASK)
    run._active_runs[request_id] = asyncio.current_task()  # замок занят, как в маршруте
    await scan_run.run_scan(request_id, _TASK, kind)
    return request_id


async def _row(db, request_id: int) -> RequestRow:
    async with db() as session:
        return await session.get(RequestRow, request_id)


def test_настройки_скана_копия_с_лимитом_10_и_тремя_карточками():
    base = get_settings()
    cfg = scan_run.scan_settings(base)
    assert cfg.cost_limit_rub_per_request == base.scan_cost_limit_rub == 10.0
    assert cfg.target_cards == scan_run.SCAN_TARGET_CARDS == 3
    # Процессные настройки не тронуты: полный отчёт работает по прежним пределам.
    assert base.cost_limit_rub_per_request == 1000.0
    assert base.target_cards == 6
    assert cfg is not base


def test_лимит_скана_берётся_из_настройки(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCAN_COST_LIMIT_RUB", "7.5")
    get_settings.cache_clear()
    assert scan_run.scan_settings().cost_limit_rub_per_request == 7.5


async def test_токен_неугадываем_и_уникален(db):
    first = await scan_run.create_scan_request(_TASK)
    second = await scan_run.create_scan_request(_TASK)
    assert first[1] != second[1]
    assert len(first[1]) >= 43  # token_urlsafe(32)
    row = await _row(db, first[0])
    assert row.scan_token == first[1] and row.status == RequestStatus.INTAKE and row.api_client is None


async def test_роутер_пайплайна_скана_с_лимитом_10_и_тремя_карточками(db):
    await _scan(db)
    [call] = _FakePipeline.calls
    assert call["router_limit"] == 10.0
    assert call["settings"].cost_limit_rub_per_request == 10.0
    assert call["settings"].target_cards == 3
    assert _Intake.calls[0]["limit"] == 10.0  # интейк идёт под тем же стоп-краном


async def test_бриф_режим_business_вид_из_формы(db):
    _Intake.model_kind = "digital"  # модель с видом не согласна — выбор человека сильнее
    await _scan(db, kind="business_model")
    [call] = _FakePipeline.calls
    assert call["brief"].mode == "business"
    assert call["brief"].solution_kind == "business_model"
    assert _Intake.calls[0]["solution_kind"] == "business_model"


@pytest.mark.parametrize(("model_kind", "expected"), [("digital", "digital"), ("research", "product")])
async def test_не_знаю_берёт_вид_интейка_иначе_продукт(db, model_kind: str, expected: str):
    _Intake.model_kind = model_kind
    await _scan(db, kind="unknown")
    [call] = _FakePipeline.calls
    assert call["brief"].solution_kind == expected
    assert call["brief"].mode == "business"  # «Гос» в публичный скан не приходит
    async with db() as session:
        saved = (await session.execute(select(BriefRow))).scalar_one()
    assert saved.solution_kind == expected and saved.confirmed_at is None


async def test_пайплайн_идёт_без_сырого_текста_и_с_потраченным_интейком(db):
    await _scan(db)
    [call] = _FakePipeline.calls
    assert "raw_text" not in call
    assert call["initial_caveats"] == [] and call["constraints_unresolved"] is False
    assert call["router_spent"] == 0.0


async def test_успех_замок_снят_статус_drafted(db):
    request_id = await _scan(db)
    assert (await _row(db, request_id)).status == RequestStatus.DRAFTED
    assert request_id not in run._active_runs


async def test_отказ_пайплайна_замок_снят_запрос_failed(db):
    _FakePipeline.fail = True
    request_id = await _scan(db)
    assert (await _row(db, request_id)).status == RequestStatus.FAILED
    assert run._active_runs == {}


async def test_стоп_кран_на_интейке_failed_с_честной_фразой(db):
    _Intake.stop = True
    request_id = await _scan(db)
    row = await _row(db, request_id)
    assert row.status == RequestStatus.FAILED
    assert row.error == scan_run.SCAN_ERROR_STOP
    assert _FakePipeline.calls == []
    assert run._active_runs == {}


async def test_отказ_до_пайплайна_не_роняет_задачу(db, monkeypatch: pytest.MonkeyPatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("внутренняя ошибка с деталями")

    monkeypatch.setattr(scan_run, "run_intake_stage", broken)
    request_id = await _scan(db)
    row = await _row(db, request_id)
    assert row.status == RequestStatus.FAILED
    assert row.error == scan_run.SCAN_ERROR_FAILED
    assert run._active_runs == {}


async def test_find_scan_только_по_токену(db):
    request_id, token = await scan_run.create_scan_request(_TASK)
    async with db() as session:
        assert (await scan_run.find_scan(session, token)).id == request_id
        assert await scan_run.find_scan(session, token + "x") is None
        assert await scan_run.find_scan(session, "") is None
        plain = RequestRow(raw_text="обычный запрос", status=RequestStatus.INTAKE)
        session.add(plain)
        await session.flush()
        assert await scan_run.find_scan(session, "None") is None  # не-скан токена не имеет
