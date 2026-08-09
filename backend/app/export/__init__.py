"""Выгрузка отчётов.

markdown — рабочий формат (готов), pdf и docx — клиентские (этап 4).
"""

from app.export.markdown import render_markdown

__all__ = ["render_markdown"]
