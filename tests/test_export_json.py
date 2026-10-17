"""JSON-выгрузка отчёта (`app.export.json`, План Э6-API, А2; РЕШ-250).

Данные — тот же `build_export_view`, что у печати; замок черновика тот же, что
у PDF/DOCX (РЕШ-199), только без обхода. База — временный файл SQLite, сеть не
задействована.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.export.json import (
    SCHEMA_VERSION,
    ReportNotValidatedError,
    build_report_payload,
    export_report_json,
)
from app.export.markdown import SUMMARY_NOT_PRINTABLE_NO_TABLE_NOTICE
from app.export.view import ExportView, build_export_view
from app.models import Base, RequestStatus
from app.models import Request as RequestRow
from app.models import SolutionCard as SolutionCardRow
from app.models import Verdict as VerdictRow
from app.schemas import Brief, Evidence, Report, ReportSummary, SolutionCard
from app.schemas import Verdict as VerdictDecision
from app.web import brief_store


@pytest.fixture
async def factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'export-json.db'}", future=True)
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


def _card(title: str, *, evidence: bool = True) -> SolutionCard:
    return SolutionCard(
        title=title,
        essence="Продление срока службы катализатора присадкой.",
        applicability="Закрывает задачу продления цикла.",
        next_step="Запросить образец у разработчика.",
        evidence=[Evidence(claim="Испытан на установке", url="https://example.org/proof")] if evidence else [],
        candidate_urls=["https://example.org/candidate"],
    )


def _report(brief: Brief, cards: list[SolutionCard], **overrides) -> Report:
    fields = {
        "request_id": 1,
        "brief": brief,
        "cards": cards,
        "contest_cards": [_card("Команда хакатона")],
        "summary": ReportSummary(recommendation="Начать с варианта №1.", caveats=["Оговорка модели."]),
        "sources_unavailable": ["rospatent"],
        "generated_at": datetime(2026, 10, 17, 9, 0),
        "cost_rub": 12.34,
    }
    fields.update(overrides)
    return Report(**fields)


async def _seed(factory, report: Report, *, status: RequestStatus, brief: Brief | None = None) -> tuple[int, list[int]]:
    async with factory() as session:
        row = RequestRow(
            raw_text="задача", status=status, cost_rub=report.cost_rub, report_json=report.model_dump_json()
        )
        session.add(row)
        await session.flush()
        request_id = row.id
        card_ids = []
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
            )
            session.add(card_row)
            await session.flush()
            card_ids.append(card_row.id)
        if brief is not None:
            await brief_store.save_brief(session, request_id, brief, confirmed=False)
    return request_id, card_ids


async def _verdict(factory, card_id: int, decision: VerdictDecision) -> None:
    async with factory() as session:
        session.add(VerdictRow(card_id=card_id, reviewer_name="Иван Иванов", decision=decision.value))


async def _validated_payload(factory, brief: Brief, cards: list[SolutionCard], **report_overrides):
    marked = brief.model_copy(update={"region_code": "86", "need_id": "restaurants_cafes", "mode": "gov"})
    report = _report(brief, cards, **report_overrides)
    request_id, card_ids = await _seed(factory, report, status=RequestStatus.VALIDATED, brief=marked)
    return request_id, card_ids


async def test_черновик_не_выгружается(factory, brief: Brief):
    """Тот же замок, что у PDF/DOCX: без вердикта эксперта выгрузки нет."""
    request_id, _ = await _seed(factory, _report(brief, [_card("Вариант")]), status=RequestStatus.DRAFTED)

    async with factory() as session:
        view = await build_export_view(session, request_id)
        with pytest.raises(ReportNotValidatedError):
            build_report_payload(view)
        with pytest.raises(ReportNotValidatedError):
            await export_report_json(session, request_id)


def test_черновой_view_не_выгружается_даже_собранный_вручную(brief: Brief):
    view = ExportView(
        request_id=5,
        report=_report(brief, [_card("Вариант")]),
        card_ids={},
        is_draft=True,
        reviewer_name="",
        validated_at=None,
    )

    with pytest.raises(ReportNotValidatedError):
        build_report_payload(view)


async def test_корень_выгрузки(factory, brief: Brief):
    request_id, card_ids = await _validated_payload(factory, brief, [_card("Вариант 1")])
    await _verdict(factory, card_ids[0], VerdictDecision.APPROVE)

    async with factory() as session:
        payload = await export_report_json(session, request_id)

    assert payload["schema_version"] == SCHEMA_VERSION == "1.0"
    assert payload["request_id"] == request_id
    assert payload["sources_unavailable"] == ["rospatent"]
    assert [card["title"] for card in payload["contest_cards"]] == ["Команда хакатона"]
    assert payload["summary"]["recommendation"] == "Начать с варианта №1."
    assert payload["validation"]["validated"] is True
    assert payload["validation"]["reviewer_name"] == "Иван Иванов"
    assert "Иван Иванов" in payload["validation"]["signature"]
    json.dumps(payload)  # сериализуется без своих кодировщиков


async def test_бриф_несёт_поля_экосистемы(factory, brief: Brief):
    request_id, _ = await _validated_payload(factory, brief, [_card("Вариант 1")])

    async with factory() as session:
        payload = await export_report_json(session, request_id)

    assert payload["brief"]["region_code"] == "86"
    assert payload["brief"]["need_id"] == "restaurants_cafes"
    assert payload["brief"]["mode"] == "gov"
    assert payload["brief"]["task_summary"] == brief.task_summary


async def test_видимость_по_вердиктам_и_evidence_как_у_печати(factory, brief: Brief):
    """reject и карточка без единого подтверждения в выгрузку не попадают; у остальных у каждого факта URL."""
    cards = [_card("Оставленная"), _card("Отклонённая"), _card("Без подтверждений", evidence=False)]
    request_id, card_ids = await _validated_payload(factory, brief, cards)
    await _verdict(factory, card_ids[1], VerdictDecision.REJECT)

    async with factory() as session:
        payload = await export_report_json(session, request_id)

    assert [card["title"] for card in payload["cards"]] == ["Оставленная"]
    for card in payload["cards"] + payload["contest_cards"]:
        assert card["evidence"], "карточка без подтверждений не выгружается"
        assert all(item["url"].startswith("https://") for item in card["evidence"])
        assert "candidate_urls" not in card
    # Карточка скрыта — рекомендация модели с прежней нумерацией не печатается (РЕШ-211).
    assert payload["summary"]["recommendation"] is None
    assert payload["summary"]["recommendation_notice"] == SUMMARY_NOT_PRINTABLE_NO_TABLE_NOTICE
    assert "Оговорка модели." not in payload["summary"]["limitations"]


async def test_стоимость_в_выгрузку_не_попадает(factory, brief: Brief):
    request_id, _ = await _validated_payload(factory, brief, [_card("Вариант 1")])

    async with factory() as session:
        payload = await export_report_json(session, request_id)

    text = json.dumps(payload, ensure_ascii=False)
    assert "cost" not in text
    assert "12.34" not in text
