"""Сквозной путь оператора одним ASGI-клиентом (задача И-13, SPEC Э3-1…Э3-5).

Семь шагов подряд: форма задачи → уточняющие вопросы → бриф без подтверждения
→ подтверждённый бриф → запуск прогона → поток событий прогресса → отчёт →
правка карточки → отчёт после перезагрузки страницы. Один тест на весь путь,
а не набор изолированных — так проверяется именно стык экранов волны 2
(И-10/И-11/И-12), а не поведение каждого маршрута по отдельности (это уже
покрыто `tests/test_web_intake.py`, `tests/test_web_run.py`,
`tests/test_web_report.py`, откуда позаимствованы приёмы подмен).

Подмены (инвариант №7, сети нет):
- `app.pipeline.stages.run_intake`/`refine_brief` — тем же приёмом, что
  `tests/test_web_intake.py`: маршрут интейка вызывает их косвенно, поэтому
  `LLMRouter` ни разу не доходит до `complete_json`;
- `app.web.routes.run.ScoutPipeline` — заглушка `_FakeScoutPipeline` (приём
  `tests/test_web_run.py`): сама пишет `report_json`/`solution_cards` и статус
  `drafted`, зовёт поддельный `ProgressHook` по стадиям планировщика, поиска,
  скоринга, карточек, сводки и завершения — ровно тот набор стадий, который
  ворота этапа проверяют по логу SSE-потока;
- база — временный файл SQLite на `tmp_path` (форма `app.db.session_scope`),
  подмена `app.db.session_scope` даёт один и тот же движок обработчикам через
  `Depends(get_session)` и фоновой задаче прогона (отложенный импорт в
  `run._run_pipeline`, РЕШ-189 п. 10).

Одна из двух карточек отчёта намеренно без `evidence` — так шаг «отчёт»
проверяет инвариант №1 (плашка «Нельзя показывать клиенту: нет
подтверждений», стадия зрелости и TRL скрыты) в самом сквозном сценарии, а не
только в изолированном тесте И-12.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import main
from app.llm.base import Usage
from app.models import Base, RequestStatus
from app.models import Request as RequestRow
from app.models import SolutionCard as SolutionCardRow
from app.schemas import (
    Brief,
    Evidence,
    IntakeQuestion,
    IntakeResult,
    MaturityStage,
    Report,
    ReportSummary,
    SolutionCard,
)
from app.web.routes import run

_AUTH = (main.settings.web_auth_user, main.settings.web_auth_password.get_secret_value())
_TASK_TEXT = "Катализатор гидроочистки теряет активность быстрее ожидаемого, нужна альтернатива."

#: Стадии, которые ворота этапа сверяют по логу SSE-потока (`docs/SPEC.md`,
#: Э3-4 и намётка ворот Э3) — фейковый пайплайн зовёт хук ровно этим набором.
_PIPELINE_STAGES: list[tuple[str, str, float]] = [
    ("planner", "Составляю план поиска", 0.1),
    ("search", "Публикации: 14", 0.4),
    ("scorer", "Оцениваю кандидатов", 0.6),
    ("cards", "Собираю карточки", 0.8),
    ("summary", "Готовлю сводку", 0.95),
]


def _draft_brief() -> Brief:
    return Brief(industry="нефтепереработка", task_summary=_TASK_TEXT, constraints=[])


def _fake_report(request_id: int, brief: Brief) -> Report:
    """Отчёт с двумя карточками: одна с подтверждением, другая — без.

    Вторая карточка нужна именно для проверки инварианта №1 внутри сквозного
    сценария (см. докстроку модуля), а не потому что реалистичный прогон
    обычно отдаёт пустой `evidence`.
    """
    with_evidence = SolutionCard(
        title="Катализатор с присадкой молибдена",
        essence="Присадка продлевает межрегенерационный цикл катализатора гидроочистки.",
        maturity_stage=MaturityStage.PILOT,
        trl_estimate=6,
        developer_org="Институт катализа",
        applicability="Закрывает задачу продления срока службы катализатора.",
        next_step="Запросить протокол испытаний.",
        evidence=[Evidence(claim="Испытан на действующей установке", url="https://example.org/a")],
    )
    without_evidence = SolutionCard(
        title="Экспериментальная присадка на основе вольфрама",
        essence="Лабораторный образец без опубликованных испытаний.",
        maturity_stage=MaturityStage.PILOT,
        trl_estimate=6,
        applicability="Потенциально закрывает ту же задачу.",
        next_step="Найти данные полевых испытаний.",
        evidence=[],
    )
    return Report(
        request_id=request_id,
        brief=brief,
        cards=[with_evidence, without_evidence],
        contest_cards=[],
        summary=ReportSummary(
            recommendation="Начать с варианта №1.",
            comparison_rows=[{"Вариант": with_evidence.title, "Зрелость": "пилот"}],
        ),
        generated_at=datetime(2026, 8, 24, 10, 0, tzinfo=UTC),
        cost_rub=3.21,
    )


class _FakeScoutPipeline:
    """Заглушка пайплайна: пишет отчёт и строки карточек напрямую в базу,
    зовёт прогресс-хук по стадиям `_PIPELINE_STAGES`, живой поиск и модель
    не вызывает ни разу (инвариант №7).

    Стоимость — через настоящий `router.cost.add` того `LLMRouter`, что ей
    передал `_run_pipeline` (РЕШ-194, вариант А ревью цикла 1 правки аудита
    Э3): роутер посеян `initial_spent_rub=row.cost_rub` (интейк и сбор брифа
    из прошлых шагов сценария), `report.cost_rub` из фикстуры — это
    собственная трата фазы прогона, а `router.cost.add` делает её
    кумулятивной тем же способом, что и настоящий пайплайн
    (`run_stages.py`: `report.cost_rub = self._router.cost.spent_rub`).
    `row.cost_rub` получает уже кумулятивное значение присваиванием — второй
    раз тот же перенос не делаем (единственный механизм, П-1 аудита Э3)."""

    def __init__(self, *, session_factory=None, progress=None, router=None, **_ignored: Any) -> None:
        self._session_factory = session_factory
        self._progress = progress
        self._router = router

    async def run(self, brief: Brief, *, request_id: int = 0, **_ignored: Any) -> None:
        for stage, message, fraction in _PIPELINE_STAGES:
            if self._progress:
                self._progress(stage, message, fraction)

        report = _fake_report(request_id, brief)
        if self._router is not None:
            self._router.cost.add(
                Usage(tokens_in=10, tokens_out=10, cost_rub=report.cost_rub, provider="deepseek", model="fake")
            )
            report.cost_rub = self._router.cost.spent_rub
        async with self._session_factory() as session:
            row = await session.get(RequestRow, request_id)
            row.status = RequestStatus.DRAFTED
            row.cost_rub = report.cost_rub
            row.report_json = report.model_dump_json()
            for position, card in enumerate(report.cards):
                session.add(
                    SolutionCardRow(
                        request_id=request_id,
                        position=position,
                        is_contest=False,
                        title=card.title,
                        essence=card.essence,
                        maturity_stage=card.maturity_stage.value,
                        trl_estimate=card.trl_estimate,
                        developer_org=card.developer_org,
                        applicability=card.applicability,
                        next_step=card.next_step,
                        evidence=[item.model_dump(mode="json") for item in card.evidence],
                        confidence=card.confidence.value,
                    )
                )

        if self._progress:
            self._progress("done", f"Отчёт готов. Стоимость запроса: {report.cost_rub:.2f} ₽", 1.0)


@pytest.fixture(autouse=True)
def _fake_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run, "ScoutPipeline", _FakeScoutPipeline)


@pytest.fixture(autouse=True)
def _clear_registry() -> None:
    """Реестр задач — модульный синглтон, общий на весь прогон тестов (тот же
    приём, что `tests/test_web_run.py`)."""
    run._active_runs.clear()
    yield
    run._active_runs.clear()


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Временная база вместо `data/scout.db` — форма `app.db.session_scope`."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'web-flow-e2e.db'}", future=True)
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

    monkeypatch.setattr("app.db.session_scope", factory)
    yield factory
    await engine.dispose()


@pytest.fixture
async def client():
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _request_id(fragment_text: str) -> int:
    """Номер запроса из текста фрагмента — везде написан «Запрос №N» (тот же
    разбор, что в `tests/test_web_intake.py`)."""
    return int(fragment_text.split("Запрос №", 1)[1].split(":", 1)[0].split(" ", 1)[0].split("<", 1)[0])


async def _card_ids(db, request_id: int) -> list[int]:
    async with db() as session:
        query = (
            select(SolutionCardRow).where(SolutionCardRow.request_id == request_id).order_by(SolutionCardRow.position)
        )
        rows = (await session.execute(query)).scalars().all()
        return [int(row.id) for row in rows]


async def test_full_operator_path_form_to_report_and_edit(client: AsyncClient, db, monkeypatch: pytest.MonkeyPatch):
    """Все семь шагов одним клиентом: форма → вопросы → бриф → подтверждение →
    запуск → поток событий → отчёт → правка → отчёт после перезагрузки."""

    async def fake_run_intake(router, task, *, industry_hint="не указана"):
        return IntakeResult(
            questions=[IntakeQuestion(question="Какая температура процесса?", why="нужна для отбора решений")],
            draft_brief=_draft_brief(),
        )

    async def fake_refine_brief(router, task, answers, *, industry_hint="не указана"):
        assert answers == {"Какая температура процесса?": "380 градусов"}
        return _draft_brief()

    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)
    monkeypatch.setattr("app.pipeline.stages.refine_brief", fake_refine_brief)

    # ── 1. Форма задачи ──────────────────────────────────────────────────
    created = await client.post("/requests", data={"raw_text": _TASK_TEXT}, auth=_AUTH)
    assert created.status_code == 200
    assert "Уточняющие вопросы" in created.text
    assert "Какая температура процесса?" in created.text
    request_id = _request_id(created.text)

    # ── 2. Ответ на уточняющий вопрос → бриф на подтверждение (без confirm) ─
    answered = await client.post(
        f"/requests/{request_id}/brief",
        data={"question": "Какая температура процесса?", "answer": "380 градусов"},
        auth=_AUTH,
    )
    assert answered.status_code == 200
    assert "Проверьте бриф перед поиском" in answered.text
    assert "Начать поиск" not in answered.text

    # ── 3. Подтверждение брифа ───────────────────────────────────────────
    confirmed = await client.post(
        f"/requests/{request_id}/brief",
        data={
            "confirm": "1",
            "industry": "нефтепереработка",
            "task_summary": _TASK_TEXT,
            "constraints": "",
            "geography": "РФ",
            "keywords_ru": "",
            "keywords_en": "",
        },
        auth=_AUTH,
    )
    assert confirmed.status_code == 200
    assert "Начать поиск" in confirmed.text
    assert "Бриф подтверждён" in confirmed.text

    # ── 4. Запуск прогона ────────────────────────────────────────────────
    started = await client.post(f"/requests/{request_id}/start", auth=_AUTH)
    assert started.status_code == 200
    assert "Идёт поиск" in started.text

    # Фоновая задача запущена `asyncio.create_task` внутри маршрута — ждём
    # её завершения явно (тот же приём, что `tests/test_web_run.py`), иначе
    # следующий шаг может застать отчёт ещё не собранным.
    await run._active_runs[request_id]

    async with db() as session:
        request_row = await session.get(RequestRow, request_id)
    assert request_row.status == RequestStatus.DRAFTED

    # ── 5. Поток событий прогресса ───────────────────────────────────────
    progress_response = await client.get(f"/requests/{request_id}/progress", auth=_AUTH)
    assert progress_response.status_code == 200
    assert progress_response.headers["content-type"].startswith("text/event-stream")
    events = [
        json.loads(line[len("data:") :].strip())
        for line in progress_response.text.splitlines()
        if line.startswith("data:")
    ]
    seen_stages = [event["stage"] for event in events]
    for expected_stage in ("planner", "search", "scorer", "cards", "summary", "done"):
        assert expected_stage in seen_stages, f"стадия {expected_stage} не встретилась в потоке: {seen_stages}"
    assert seen_stages[-1] == "done"

    # ── 6. Отчёт ──────────────────────────────────────────────────────────
    report_response = await client.get(f"/requests/{request_id}/report", auth=_AUTH)
    assert report_response.status_code == 200
    report_text = report_response.text
    assert "ЧЕРНОВИК — не проверено экспертом" in report_text
    assert "Катализатор с присадкой молибдена" in report_text
    assert "Экспериментальная присадка на основе вольфрама" in report_text
    # Инвариант №1: карточка без evidence прячет зрелость и TRL, даёт плашку.
    assert "Нельзя показывать клиенту: нет подтверждений" in report_text
    assert "style=" not in report_text

    card_ids = await _card_ids(db, request_id)
    assert len(card_ids) == 2
    editable_card_id = card_ids[0]

    # ── 7. Правка карточки и перезагрузка страницы ───────────────────────
    edit_response = await client.post(
        f"/cards/{editable_card_id}/edit",
        data={"title": "Катализатор нового поколения"},
        auth=_AUTH,
    )
    assert edit_response.status_code in (200, 303)

    reloaded = await client.get(f"/requests/{request_id}/report", auth=_AUTH)
    assert reloaded.status_code == 200
    assert "Катализатор нового поколения" in reloaded.text
    # Правка не снимает пометку черновика (РЕШ-189 п. 6) — на месте и после
    # перезагрузки.
    assert "ЧЕРНОВИК — не проверено экспертом" in reloaded.text
