"""Проверки команды `scout export` (Э4-И6, SPEC Э4-2, РЕШ-199, РЕШ-202).

Экспортёры (`export_pdf`, `export_docx`) подменены заглушками — Chromium и
python-docx в наборе не участвуют (инвариант №7). База — временный файл SQLite
на `tmp_path`, той же формы, что у `app.db.session_scope`
(`tests/test_web_report.py`); подмена — `app.db.session_scope`/`app.db.init_db`.

Тестовые функции — синхронные, а асинхронные шаги оборачиваются `asyncio.run`
(тот же приём, что в `tests/test_web_report.py`): `CliRunner.invoke` вызывает
команду, а внутри неё `_export` сама вызывает `asyncio.run` — вложенный цикл
событий внутри уже запущенного event loop упал бы `RuntimeError`, если бы
тестовая функция сама была `async def` под pytest-asyncio.

`typer.testing.CliRunner` гоняет команду точно так же, как её вызывает
оператор в терминале, — включая коды возврата и текст в stdout, который видит
человек, а не только внутренние объекты.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from typer.testing import CliRunner

import app.cli as cli
import app.cli_export as cli_export
from app.models import Base, RequestStatus
from app.models import Request as RequestRow
from app.schemas import Brief, Report, ReportSummary

runner = CliRunner()
_TODAY = f"{date.today():%Y-%m-%d}"


@pytest.fixture
def db_session_factory(tmp_path: Path):
    """Фабрика сессий на временном файле SQLite — форма `app.db.session_scope`."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'export-cli-test.db'}", future=True)

    async def _prepare() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_prepare())
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
    asyncio.run(engine.dispose())


@pytest.fixture(autouse=True)
def _wire_test_db(monkeypatch: pytest.MonkeyPatch, db_session_factory, tmp_path: Path):
    """`_export` идёт через `app.db.session_scope`/`app.db.init_db` — подмена этих
    имён и подключает временную базу (тот же приём, что и в `test_web_report.py`).
    `app.cli.get_settings` возвращает каталог выгрузки внутри `tmp_path`, чтобы
    тест не писал файлы в боевой `out/` репозитория."""
    monkeypatch.setattr("app.db.session_scope", db_session_factory)

    async def fake_init_db() -> None:
        return None

    monkeypatch.setattr("app.db.init_db", fake_init_db)
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(out_dir=tmp_path / "out"))
    yield


@pytest.fixture
def exporters(monkeypatch: pytest.MonkeyPatch):
    """Заглушки `export_pdf`/`export_docx`: запоминают вызовы, пишут байты на диск
    (путь должен существовать для правдоподобия), сеть и внешние процессы не трогают."""
    calls = SimpleNamespace(pdf=[], docx=[])

    async def fake_export_pdf(view, out_path: Path) -> Path:
        calls.pdf.append((view, out_path))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(out_path.write_bytes, b"%PDF-1.4 fake")
        return out_path

    def fake_export_docx(view, out_path: Path) -> Path:
        calls.docx.append((view, out_path))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake docx")
        return out_path

    monkeypatch.setattr(cli_export, "export_pdf", fake_export_pdf)
    monkeypatch.setattr(cli_export, "export_docx", fake_export_docx)
    return calls


async def _seed_request(session_factory, *, status: RequestStatus, brief: Brief) -> int:
    report = Report(
        request_id=1,
        brief=brief,
        cards=[],
        contest_cards=[],
        summary=ReportSummary(recommendation="Начать с варианта №1."),
        generated_at=datetime(2026, 8, 27, 9, 0),
    )
    async with session_factory() as session:
        row = RequestRow(
            raw_text="катализатор теряет активность раньше срока",
            status=status,
            report_json=report.model_dump_json(),
        )
        session.add(row)
        await session.flush()
        return int(row.id)


def _make_request_id(db_session_factory, brief, status: RequestStatus = RequestStatus.VALIDATED) -> int:
    return asyncio.run(_seed_request(db_session_factory, status=status, brief=brief))


def _run(args: list[str]):
    return runner.invoke(cli.app, args)


def _unwrapped(text: str) -> str:
    """Убрать переносы строк, которыми rich заворачивает длинный путь при печати
    в узкий терминал CliRunner (реальный путь пути это не меняет — только вид)."""
    return text.replace("\n", "")


def test_pdf_flag_calls_export_pdf_once_with_expected_path(db_session_factory, exporters, brief):
    request_id = _make_request_id(db_session_factory, brief)

    result = _run(["export", str(request_id), "--pdf"])

    assert result.exit_code == 0, result.output
    assert len(exporters.pdf) == 1
    assert len(exporters.docx) == 0
    view, out_path = exporters.pdf[0]
    assert out_path.name == f"report-{request_id}-{_TODAY}.pdf"
    assert out_path.parent.name == "out"
    assert str(out_path) in _unwrapped(result.output)
    assert view.is_draft is False


def test_docx_flag_calls_export_docx_once_with_expected_path(db_session_factory, exporters, brief):
    request_id = _make_request_id(db_session_factory, brief)

    result = _run(["export", str(request_id), "--docx"])

    assert result.exit_code == 0, result.output
    assert len(exporters.docx) == 1
    assert len(exporters.pdf) == 0
    _, out_path = exporters.docx[0]
    assert out_path.name == f"report-{request_id}-{_TODAY}.docx"
    assert str(out_path) in _unwrapped(result.output)


def test_out_option_changes_directory_not_filename(db_session_factory, exporters, brief, tmp_path: Path):
    request_id = _make_request_id(db_session_factory, brief)
    custom_dir = tmp_path / "мой-каталог"

    result = _run(["export", str(request_id), "--pdf", "--out", str(custom_dir)])

    assert result.exit_code == 0, result.output
    _, out_path = exporters.pdf[0]
    assert out_path.parent == custom_dir
    assert out_path.name == f"report-{request_id}-{_TODAY}.pdf"


def test_missing_format_refuses_without_calling_exporters(db_session_factory, exporters, brief):
    request_id = _make_request_id(db_session_factory, brief)

    result = _run(["export", str(request_id)])

    assert result.exit_code != 0
    assert len(exporters.pdf) == 0
    assert len(exporters.docx) == 0


def test_both_formats_at_once_refuses_without_calling_exporters(db_session_factory, exporters, brief):
    request_id = _make_request_id(db_session_factory, brief)

    result = _run(["export", str(request_id), "--pdf", "--docx"])

    assert result.exit_code != 0
    assert len(exporters.pdf) == 0
    assert len(exporters.docx) == 0


def test_drafted_without_draft_flag_refuses_with_code_1_and_no_export_call(db_session_factory, exporters, brief):
    request_id = _make_request_id(db_session_factory, brief, status=RequestStatus.DRAFTED)

    result = _run(["export", str(request_id), "--pdf"])

    assert result.exit_code == 1
    assert len(exporters.pdf) == 0
    assert "не проверен экспертом" in result.output
    # Никаких внутренностей схемы или трассировки в тексте отказа (§12, инвариант №8).
    assert "Traceback" not in result.output
    assert "ReportNotFoundError" not in result.output


def test_drafted_with_draft_flag_succeeds_and_view_is_draft(db_session_factory, exporters, brief):
    request_id = _make_request_id(db_session_factory, brief, status=RequestStatus.DRAFTED)

    result = _run(["export", str(request_id), "--pdf", "--draft"])

    assert result.exit_code == 0, result.output
    assert len(exporters.pdf) == 1
    view, _ = exporters.pdf[0]
    assert view.is_draft is True


def test_unknown_request_id_refuses_with_human_text(db_session_factory, exporters, brief):
    del brief  # база пуста — таблицы созданы, строк нет
    result = _run(["export", "999999", "--pdf"])

    assert result.exit_code == 1
    assert len(exporters.pdf) == 0
    assert "не найден" in result.output
    assert "Traceback" not in result.output


def test_cli_export_module_has_no_second_source_of_truth():
    """Критерий 4 задачи: сборка данных идёт только через `build_export_view`."""
    import re

    text = Path(cli_export.__file__).read_text(encoding="utf-8")
    assert re.search(r"load_report|apply_overrides", text) is None


def test_cli_export_module_has_no_direct_httpx():
    """Инвариант №6: прямого `httpx` в файле команды выгрузки нет."""
    text = Path(cli_export.__file__).read_text(encoding="utf-8")
    assert "httpx" not in text
