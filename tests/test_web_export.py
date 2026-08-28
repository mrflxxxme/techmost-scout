"""Проверки маршрута выгрузки PDF и кнопки на странице отчёта (Э4-И6, SPEC Э4-2).

`export_pdf` подменён заглушкой — Chromium в наборе не поднимается (тот же
довод, что у `tests/test_export_pdf.py`, РЕШ-196 п. 12). База — временный файл
SQLite на `tmp_path`, той же формы, что у `tests/test_web_report.py`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import main
from app.models import Base, RequestStatus
from app.models import Request as RequestRow
from app.schemas import Report, ReportSummary
from app.web.routes import report as report_routes

_AUTH = ("scout", "parol123")


@pytest.fixture
async def db_session_factory(tmp_path: Path):
    """Фабрика сессий на временном файле SQLite — форма `app.db.session_scope`."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'export-web-test.db'}", future=True)
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
def _wire_test_db(monkeypatch: pytest.MonkeyPatch, db_session_factory, tmp_path: Path):
    """Тот же приём, что в `tests/test_web_report.py`: маршруты идут через
    `Depends(get_session)` → `app.db.session_scope`, подмена этого имени и
    подключает временную базу. `get_settings` маршрута выгрузки подменён
    отдельно — файл пишется в `tmp_path`, а не в боевой `out/` репозитория."""
    monkeypatch.setattr("app.db.session_scope", db_session_factory)

    async def fake_init_db() -> None:
        return None

    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(main.settings, "web_auth_user", _AUTH[0])
    monkeypatch.setattr(main.settings, "web_auth_password", SecretStr(_AUTH[1]))
    monkeypatch.setattr(report_routes, "get_settings", lambda: SimpleNamespace(out_dir=tmp_path / "out"))
    yield


@pytest.fixture
def fake_export_pdf(monkeypatch: pytest.MonkeyPatch):
    """Заглушка `export_pdf`: пишет настоящие байты на диск (нужно для `FileResponse`),
    считает вызовы, не трогает ни Chromium, ни сеть."""
    calls: list[Path] = []

    async def _fake(view, out_path: Path) -> Path:
        calls.append(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(out_path.write_bytes, b"%PDF-1.4 fake")
        return out_path

    monkeypatch.setattr(report_routes, "export_pdf", _fake)
    return calls


def _seed_request(session_factory, *, status: RequestStatus, brief) -> int:
    report = Report(
        request_id=1,
        brief=brief,
        cards=[],
        contest_cards=[],
        summary=ReportSummary(recommendation="Начать с варианта №1."),
        generated_at=datetime(2026, 8, 27, 9, 0),
    )

    async def _do() -> int:
        async with session_factory() as session:
            row = RequestRow(
                raw_text="катализатор теряет активность раньше срока",
                status=status,
                report_json=report.model_dump_json(),
            )
            session.add(row)
            await session.flush()
            return int(row.id)

    return asyncio.run(_do())


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


def test_export_pdf_returns_200_with_pdf_content_type(client, db_session_factory, fake_export_pdf, brief):
    request_id = _seed_request(db_session_factory, status=RequestStatus.VALIDATED, brief=brief)

    with client:
        response = client.get(f"/requests/{request_id}/export.pdf", auth=_AUTH)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert len(fake_export_pdf) == 1
    assert response.content == b"%PDF-1.4 fake"


def test_export_pdf_unknown_request_returns_human_404_not_json(client, db_session_factory, fake_export_pdf):
    with client:
        response = client.get("/requests/999999/export.pdf", auth=_AUTH)

    assert response.status_code == 404
    assert "application/json" not in response.headers["content-type"]
    assert "не найден" in response.text
    assert len(fake_export_pdf) == 0


def test_export_pdf_drafted_without_draft_param_refuses_without_calling_exporter(
    client, db_session_factory, fake_export_pdf, brief
):
    request_id = _seed_request(db_session_factory, status=RequestStatus.DRAFTED, brief=brief)

    with client:
        response = client.get(f"/requests/{request_id}/export.pdf", auth=_AUTH)

    assert response.status_code == 400
    assert "application/json" not in response.headers["content-type"]
    assert "не проверен экспертом" in response.text
    assert len(fake_export_pdf) == 0


def test_export_pdf_drafted_with_draft_param_succeeds(client, db_session_factory, fake_export_pdf, brief):
    request_id = _seed_request(db_session_factory, status=RequestStatus.DRAFTED, brief=brief)

    with client:
        response = client.get(f"/requests/{request_id}/export.pdf?draft=true", auth=_AUTH)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert len(fake_export_pdf) == 1


def test_report_page_button_is_marked_draft_when_status_is_drafted(client, db_session_factory, brief):
    request_id = _seed_request(db_session_factory, status=RequestStatus.DRAFTED, brief=brief)

    with client:
        response = client.get(f"/requests/{request_id}/report", auth=_AUTH)

    assert "Полный текст (PDF, черновик)" in response.text
    assert f'href="/requests/{request_id}/export.pdf?draft=true"' in response.text


def test_report_page_button_is_plain_when_status_is_validated(client, db_session_factory, brief):
    request_id = _seed_request(db_session_factory, status=RequestStatus.VALIDATED, brief=brief)

    with client:
        response = client.get(f"/requests/{request_id}/report", auth=_AUTH)

    assert "Полный текст (PDF)" in response.text
    assert "черновик" not in response.text
    assert f'href="/requests/{request_id}/export.pdf"' in response.text
