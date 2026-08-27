"""Реализация команды `scout export` (Э4-И6, SPEC Э4-2, РЕШ-199, РЕШ-202).

Точка входа typer (`app.command()`) остаётся в `cli.py` — здесь только тяжёлая
асинхронная реализация, тем же приёмом дробления, что и `cli_run.py`/
`cli_doctor.py` (докстрока `app.cli`). `console`, `get_settings` читаются
ОТЛОЖЕННЫМ импортом из `app.cli` внутри `_export` — так тестовая подмена
`monkeypatch.setattr(cli, "get_settings", ...)` доезжает до места
использования, а не остаётся висеть на неиспользуемой ссылке (тот же приём,
каким `cli_run._run` сохраняет подмену `get_settings`).

Данные собираются строго через `export.view.build_export_view` — второго
пути правды (свой разбор `report_json`, повторное наложение `editor_overrides`
или свой фильтр reject-карточек) этот модуль не заводит (РЕШ-196 п. 8).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import typer

from app.export.docx import export_docx
from app.export.pdf import export_pdf
from app.export.view import build_export_view
from app.pipeline.persistence import ReportNotFoundError

__all__ = ["_export", "export_filename"]


def export_filename(request_id: int, extension: str) -> str:
    """Имя файла выгрузки (РЕШ-196 п. 9): `report-<id>-<ГГГГ-ММ-ДД>.<расширение>`.

    Дата — дата выгрузки (сегодняшняя), а не дата генерации отчёта: повторная
    выгрузка того же запроса в другой день получает новое имя, а не тихо
    перезаписывает вчерашний файл. Общая точка для команды и веб-маршрута
    (`web/routes/report.py`) — второго счётчика имени файла не заводится.
    """
    return f"report-{request_id}-{date.today():%Y-%m-%d}.{extension}"


async def _export(
    request_id: int,
    *,
    pdf: bool,
    docx: bool,
    draft: bool,
    out: Path | None,
) -> None:
    """Собрать `ExportView` запроса и сохранить его в PDF или DOCX.

    Без указания формата — человеческий отказ (код возврата 2): угадывать
    формат по умолчанию рискованно, оператор получил бы файл, которого не
    просил. По запросу в статусе черновика без `--draft` — отказ «отчёт не
    проверен экспертом» (РЕШ-199, инвариант №2), код возврата 1, ни один
    экспортёр не вызывается и файл не создаётся. `--out` задаёт только каталог
    — имя файла всегда строит `export_filename`.
    """
    from app.cli import console, get_settings

    if pdf and docx:
        console.print("[red]Выберите один формат:[/red] --pdf или --docx, не оба сразу.")
        raise typer.Exit(code=2)
    if not pdf and not docx:
        console.print("[red]Не указан формат выгрузки:[/red] добавьте --pdf или --docx.")
        raise typer.Exit(code=2)

    from app.db import init_db, session_scope

    try:
        await init_db()
    except Exception as exc:  # база недоступна — причина не наша, отчёт человеку и выход
        console.print(f"[red]Экспорт не выполнен:[/red] база данных недоступна ({exc}).")
        raise typer.Exit(code=1) from exc

    try:
        async with session_scope() as session:
            view = await build_export_view(session, request_id)
    except ReportNotFoundError as exc:
        # Текст `ReportNotFoundError` уже человеческий и без внутренностей
        # схемы (см. докстроку класса) — печатается как есть.
        console.print(f"[red]Экспорт не выполнен:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if view.is_draft and not draft:
        console.print(
            "[red]Экспорт не выполнен:[/red] отчёт ещё не проверен экспертом. "
            "Чтобы выгрузить черновик, повторите команду с флагом --draft."
        )
        raise typer.Exit(code=1)

    out_dir = out or get_settings().out_dir
    extension = "pdf" if pdf else "docx"
    out_path = out_dir / export_filename(request_id, extension)

    if pdf:
        result_path = await export_pdf(view, out_path)
    else:
        result_path = export_docx(view, out_path)

    console.print(f"[green]Отчёт выгружен:[/green] {result_path}")
