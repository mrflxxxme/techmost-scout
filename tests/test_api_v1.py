"""JSON-API `/api/v1` (План Э6-API, А3; РЕШ-250).

Пайплайн и интейк подменены: живой `ScoutPipeline` и модели не вызываются ни
разу (инвариант №7). База — временный файл SQLite, `app.db.session_scope`
подменён, поэтому и маршруты, и фоновая задача видят одну базу (тот же приём,
что `tests/test_web_run.py`). Ключи — заведомо фиктивные.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import main
from app.config import get_settings
from app.llm import CostLimitExceededError
from app.models import Base, RequestStatus
from app.models import Request as RequestRow
from app.models import SolutionCard as SolutionCardRow
from app.models import Verdict as VerdictRow
from app.pipeline.intake_flow import IntakeStageOutcome
from app.schemas import Brief, Evidence, Report, ReportSummary, SolutionCard
from app.web import api_access, api_run, brief_store, progress
from app.web.routes import run

KEY_A = "test-key-alpha-0000"
KEY_B = "test-key-bravo-1111"
_TASK = "Катализатор гидроочистки теряет активность за 8 месяцев вместо 24."


class _FakePipeline:
    """Заглушка `ScoutPipeline`: запоминает вызов и пишет статус `drafted`."""

    calls: ClassVar[list[dict[str, Any]]] = []
    gate: asyncio.Event | None = None

    def __init__(self, *, session_factory=None, progress=None, router=None, **_: Any) -> None:
        self._session_factory = session_factory
        self._router = router

    async def run(self, brief: Brief, *, request_id: int = 0, **kwargs: Any):
        type(self).calls.append({"brief": brief, "request_id": request_id, **kwargs})
        if type(self).gate is not None:
            await type(self).gate.wait()
        async with self._session_factory() as session:
            row = await session.get(RequestRow, request_id)
            row.status = RequestStatus.DRAFTED

    async def aclose(self) -> None:
        return None


async def _fake_intake(router, task, *, industry_hint, session_factory, stage_log):
    return IntakeStageOutcome(
        intake=None, brief=Brief(industry=industry_hint, task_summary=task[:60]), caveats=[], warning=None
    )


async def _fake_constraints(router, brief, task, *, industry_hint, session_factory, stage_log):
    return brief, False


@pytest.fixture(autouse=True)
def _api_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCOUT_API_KEYS", f"alpha:{KEY_A},bravo:{KEY_B}")
    monkeypatch.setenv("API_RATE_LIMIT_RUNS_PER_HOUR", "5")
    monkeypatch.setenv("API_RATE_LIMIT_READS_PER_MINUTE", "60")
    get_settings.cache_clear()
    api_access.limiter.reset()
    run._active_runs.clear()
    # Замок привязывается к циклу событий первого использования, а брокер
    # прогресса копит события по номеру запроса до перезапуска процесса: без
    # подмены события `done` этих прогонов достались бы тестам веба с теми же
    # номерами запросов во временной базе.
    monkeypatch.setattr(run, "_start_lock", asyncio.Lock())
    monkeypatch.setattr(progress, "_broker", progress.ProgressBroker())
    _FakePipeline.calls = []
    _FakePipeline.gate = None
    monkeypatch.setattr(run, "ScoutPipeline", _FakePipeline)
    monkeypatch.setattr(api_run, "run_intake_stage", _fake_intake)
    monkeypatch.setattr(api_run, "resolve_constraints_stage", _fake_constraints)
    yield
    run._active_runs.clear()
    api_access.limiter.reset()
    get_settings.cache_clear()


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'api-v1.db'}", future=True)
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


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as http:
        yield http


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _body(**context: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"schema_version": "1.0", "task": _TASK}
    if context:
        body["context"] = context
    return body


async def _finish_runs() -> None:
    for task in list(run._active_runs.values()):
        await task


async def _seed_report(factory, *, client_name: str | None, status: RequestStatus, verdict: bool) -> int:
    """Строка запроса с готовым отчётом из одной карточки с подтверждением."""
    brief = Brief(industry="нефтепереработка", task_summary="Катализатор теряет активность.", region_code="86")
    card = SolutionCard(
        title="Вариант",
        essence="Суть.",
        applicability="Закрывает задачу.",
        next_step="Связаться.",
        evidence=[Evidence(claim="Испытан", url="https://example.org/proof")],
    )
    report = Report(
        request_id=0,
        brief=brief,
        cards=[card],
        summary=ReportSummary(recommendation="Начать с варианта №1."),
        generated_at=datetime(2026, 10, 17, 9, 0),
        cost_rub=7.77,
    )
    async with factory() as session:
        row = RequestRow(
            raw_text="задача",
            status=status,
            api_client=client_name,
            report_json=report.model_dump_json(),
            cost_rub=7.77,
        )
        session.add(row)
        await session.flush()
        card_row = SolutionCardRow(
            request_id=row.id,
            position=0,
            title=card.title,
            essence=card.essence,
            maturity_stage=card.maturity_stage.value,
            applicability=card.applicability,
            next_step=card.next_step,
            evidence=[item.model_dump(mode="json") for item in card.evidence],
        )
        session.add(card_row)
        await session.flush()
        if verdict:
            session.add(VerdictRow(card_id=card_row.id, reviewer_name="Иван Иванов", decision="approve"))
        await brief_store.save_brief(session, row.id, brief, confirmed=False)
        return row.id


# ── Ключи ─────────────────────────────────────────────────────────────────────
async def test_пустые_ключи_выключают_api(monkeypatch: pytest.MonkeyPatch, client, db):
    monkeypatch.setenv("SCOUT_API_KEYS", "")
    get_settings.cache_clear()

    for method, path in (
        ("post", "/api/v1/requests"),
        ("get", "/api/v1/requests/1"),
        ("get", "/api/v1/requests/1/report"),
    ):
        response = await getattr(client, method)(path, headers=_bearer(KEY_A))
        assert response.status_code == 503, path
        assert response.json()["detail"]["error"] == "api_disabled"


@pytest.mark.parametrize(
    "headers",
    [{}, _bearer("wrong-key"), {"X-API-Key": "wrong-key"}, {"Authorization": f"Basic {KEY_A}"}, _bearer(KEY_A[:-1])],
)
async def test_без_действующего_ключа_401(client, db, headers: dict[str, str]):
    response = await client.get("/api/v1/requests/1", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("headers", [_bearer(KEY_A), {"X-API-Key": KEY_A}])
async def test_ключ_принимается_bearer_и_x_api_key(client, db, headers: dict[str, str]):
    response = await client.get("/api/v1/requests/999", headers=headers)

    assert response.status_code == 404  # ключ принят, запроса нет


def test_разбор_ключей_и_сравнение():
    keys = api_access.parse_api_keys(f" alpha:{KEY_A} , :пусто, без_двоеточия, bravo:{KEY_B},")

    assert [name for name, _ in keys] == ["alpha", "bravo"]
    assert api_access.match_api_key(KEY_B, keys) == "bravo"
    assert api_access.match_api_key(KEY_A + "x", keys) is None
    assert api_access.match_api_key("ключ-кириллицей", keys) is None


async def test_ключ_не_попадает_в_лог_и_ответы(client, db, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG)
    texts = []
    created = await client.post("/api/v1/requests", json=_body(), headers=_bearer(KEY_A))
    texts.append(created.text)
    await _finish_runs()
    for path in ("/api/v1/requests/{id}", "/api/v1/requests/{id}/report"):
        texts.append((await client.get(path.format(id=created.json()["request_id"]), headers=_bearer(KEY_A))).text)
    texts.append((await client.get("/api/v1/requests/1", headers=_bearer("wrong-" + KEY_B))).text)

    for text in [*texts, caplog.text]:
        assert KEY_A not in text
        assert KEY_B not in text


# ── Маршруты не под паролем интерфейса ────────────────────────────────────────
async def test_api_не_под_basic_auth_а_интерфейс_под_ним(client, db):
    assert (await client.get("/api/v1/requests/999", headers=_bearer(KEY_A))).status_code == 404
    web = await client.get("/requests")
    assert web.status_code == 401
    assert web.headers["www-authenticate"] == "Basic"


# ── Запуск ────────────────────────────────────────────────────────────────────
async def test_запуск_202_и_поля_экосистемы_доходят_до_брифа(client, db):
    body = _body(
        mode="gov",
        region={"code_fns": "89", "name": "ЯНАО"},
        need={"id": "restaurants_cafes", "label": "кафе"},
        source={"product": "bot", "placement": "start_menu"},
        signals=[{"metric": "m", "value": 1, "measured": True}],
        id="00000000-0000-0000-0000-000000000000",
    )

    response = await client.post("/api/v1/requests", json=body, headers=_bearer(KEY_A))

    assert response.status_code == 202
    payload = response.json()
    request_id = payload["request_id"]
    assert payload["schema_version"] == "1.0"
    assert payload["links"] == {
        "status": f"/api/v1/requests/{request_id}",
        "report": f"/api/v1/requests/{request_id}/report",
    }
    assert response.headers["location"] == f"/api/v1/requests/{request_id}"
    assert request_id in run._active_runs, "прогон регистрируется в реестре веба"
    await _finish_runs()

    assert len(_FakePipeline.calls) == 1
    call = _FakePipeline.calls[0]
    assert call["request_id"] == request_id
    assert (call["brief"].region_code, call["brief"].need_id, call["brief"].mode) == ("89", "restaurants_cafes", "gov")
    async with db() as session:
        row = await session.get(RequestRow, request_id)
        assert row.api_client == "alpha"
        assert row.status == RequestStatus.DRAFTED
        stored = await brief_store.load_brief(session, request_id)
    assert (stored.region_code, stored.need_id, stored.mode) == ("89", "restaurants_cafes", "gov")


@pytest.mark.parametrize(
    "body",
    [
        {"task": _TASK},
        {"schema_version": "2.0", "task": _TASK},
        {"schema_version": "1.0", "task": ""},
        _body(region={"code_fns": "8"}),
        _body(region={"code_fns": "123"}),
        _body(need={"id": "Bad-Id"}),
        _body(mode="world"),
        _body(source={"product": "unknown", "placement": "x"}),
    ],
)
async def test_тело_не_по_контракту_422(client, db, body: dict[str, Any]):
    response = await client.post("/api/v1/requests", json=body, headers=_bearer(KEY_A))

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "invalid_body"
    assert not run._active_runs


async def test_риск_обезличивания_422_без_эха(client, db):
    task = "Для ООО «Ромашка» нужно продлить срок службы катализатора гидроочистки."
    response = await client.post(
        "/api/v1/requests", json={"schema_version": "1.0", "task": task}, headers=_bearer(KEY_A)
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "deidentification_risk"
    assert "Ромашка" not in response.text
    assert not run._active_runs
    async with db() as session:
        assert await session.get(RequestRow, 1) is None, "строка запроса не заводится"


async def test_занятая_система_409_без_номера_чужого_прогона(client, db):
    _FakePipeline.gate = asyncio.Event()
    first = await client.post("/api/v1/requests", json=_body(), headers=_bearer(KEY_A))
    assert first.status_code == 202

    second = await client.post("/api/v1/requests", json=_body(), headers=_bearer(KEY_B))

    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "busy"
    assert str(first.json()["request_id"]) not in second.text
    _FakePipeline.gate.set()
    await _finish_runs()


async def test_стоп_кран_на_интейке_помечает_запрос_отказом(monkeypatch: pytest.MonkeyPatch, client, db):
    async def _costly_intake(*_: Any, **__: Any):
        raise CostLimitExceededError("стоп-кран")

    monkeypatch.setattr(api_run, "run_intake_stage", _costly_intake)

    response = await client.post("/api/v1/requests", json=_body(), headers=_bearer(KEY_A))
    await _finish_runs()

    status = await client.get(f"/api/v1/requests/{response.json()['request_id']}", headers=_bearer(KEY_A))
    assert status.json()["status"] == "failed"
    assert _FakePipeline.calls == []
    assert not run._active_runs


# ── Статус и отчёт ────────────────────────────────────────────────────────────
async def test_статус_без_стоимости(client, db):
    request_id = await _seed_report(db, client_name="alpha", status=RequestStatus.DRAFTED, verdict=False)

    response = await client.get(f"/api/v1/requests/{request_id}", headers=_bearer(KEY_A))

    payload = response.json()
    assert payload["schema_version"] == "1.0"
    assert payload["status"] == "drafted"
    assert payload["status_label"] == "Черновик готов"
    assert payload["validated"] is False
    assert payload["report_ready"] is False
    assert "cost" not in response.text
    assert "7.77" not in response.text


async def test_отчёт_до_вердикта_409_awaiting_validation(client, db):
    request_id = await _seed_report(db, client_name="alpha", status=RequestStatus.DRAFTED, verdict=False)

    response = await client.get(f"/api/v1/requests/{request_id}/report", headers=_bearer(KEY_A))

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "awaiting_validation"
    assert "Вариант" not in response.text, "черновик не отдаётся ни частью"


async def test_отчёт_после_вердикта_отдаётся_json_выгрузкой(client, db):
    request_id = await _seed_report(db, client_name="alpha", status=RequestStatus.VALIDATED, verdict=True)

    status = (await client.get(f"/api/v1/requests/{request_id}", headers=_bearer(KEY_A))).json()
    response = await client.get(f"/api/v1/requests/{request_id}/report", headers=_bearer(KEY_A))

    assert status["validated"] is True and status["report_ready"] is True
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "1.0"
    assert payload["request_id"] == request_id
    assert [card["title"] for card in payload["cards"]] == ["Вариант"]
    assert payload["brief"]["region_code"] == "86"
    assert "7.77" not in json.dumps(payload)


# ── Изоляция приложений ───────────────────────────────────────────────────────
@pytest.mark.parametrize("suffix", ["", "/report"])
async def test_чужой_и_несуществующий_номер_одинаковый_404(client, db, suffix: str):
    foreign = await _seed_report(db, client_name="alpha", status=RequestStatus.VALIDATED, verdict=True)
    web_made = await _seed_report(db, client_name=None, status=RequestStatus.VALIDATED, verdict=True)
    missing = web_made + 100

    responses = {}
    for name, request_id in (("foreign", foreign), ("web", web_made), ("missing", missing)):
        response = await client.get(f"/api/v1/requests/{request_id}{suffix}", headers=_bearer(KEY_B))
        responses[name] = (response.status_code, response.json()["detail"]["error"])

    assert set(responses.values()) == {(404, "not_found")}
    own = await client.get(f"/api/v1/requests/{foreign}{suffix}", headers=_bearer(KEY_A))
    assert own.status_code == 200


# ── Ограничение частоты ───────────────────────────────────────────────────────
async def test_лимит_запусков_429_с_retry_after(monkeypatch: pytest.MonkeyPatch, client, db):
    monkeypatch.setenv("API_RATE_LIMIT_RUNS_PER_HOUR", "2")
    get_settings.cache_clear()

    for _ in range(2):
        assert (await client.post("/api/v1/requests", json=_body(), headers=_bearer(KEY_A))).status_code == 202
        await _finish_runs()
    third = await client.post("/api/v1/requests", json=_body(), headers=_bearer(KEY_A))

    assert third.status_code == 429
    assert 1 <= int(third.headers["retry-after"]) <= 3600
    assert not run._active_runs
    other_app = await client.post("/api/v1/requests", json=_body(), headers=_bearer(KEY_B))
    assert other_app.status_code == 202, "лимит считается на приложение"
    await _finish_runs()


async def test_отказ_занятостью_не_расходует_лимит_запусков(monkeypatch: pytest.MonkeyPatch, client, db):
    monkeypatch.setenv("API_RATE_LIMIT_RUNS_PER_HOUR", "1")
    get_settings.cache_clear()
    run._active_runs[-1] = asyncio.get_running_loop().create_future()  # чужой прогон «идёт»

    assert (await client.post("/api/v1/requests", json=_body(), headers=_bearer(KEY_A))).status_code == 409
    run._active_runs.clear()
    assert (await client.post("/api/v1/requests", json=_body(), headers=_bearer(KEY_A))).status_code == 202
    await _finish_runs()


async def test_лимит_чтений_429_с_retry_after(monkeypatch: pytest.MonkeyPatch, client, db):
    monkeypatch.setenv("API_RATE_LIMIT_READS_PER_MINUTE", "3")
    get_settings.cache_clear()

    codes = [(await client.get("/api/v1/requests/999", headers=_bearer(KEY_A))).status_code for _ in range(3)]
    limited = await client.get("/api/v1/requests/999/report", headers=_bearer(KEY_A))

    assert codes == [404, 404, 404]
    assert limited.status_code == 429
    assert 1 <= int(limited.headers["retry-after"]) <= 60
    assert (await client.get("/api/v1/requests/999", headers=_bearer(KEY_B))).status_code == 404


def test_скользящее_окно_освобождается_со_временем():
    now = [1000.0]
    limiter = api_access.SlidingWindowLimiter(clock=lambda: now[0])
    key = ("alpha", "runs")

    assert limiter.hit(key, limit=2, window=60) is None
    now[0] += 10
    assert limiter.hit(key, limit=2, window=60) is None
    assert limiter.hit(key, limit=2, window=60) == 50
    now[0] += 50
    assert limiter.hit(key, limit=2, window=60) is None
