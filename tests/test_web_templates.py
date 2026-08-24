"""Страж вёрстки: инлайновые `style=` и hex-цвета запрещены в шаблонах.

Все цвета и размеры живут в `backend/app/web/static/theme.css` — страницы
пользуются только классами (SPEC Э3-2, РЕШ-189 п. 14). Единственное временное
исключение — `_stage_todo.html`: заглушку нереализованной стадии убирает
задача И-10 вместе с этим исключением из стража (см. `.planning/PLAN.md`,
раздел И-10, критерий 2).

Дополнительно проверяется, что три готовые страницы рендерятся без ошибок
через ASGI-клиент FastAPI (обёртку над httpx) с basic auth — без обращения
в сеть.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import main

_TEMPLATES_DIR = Path(main.WEB_DIR) / "templates"

#: Заглушка нереализованной стадии — её инлайновые `style=` не в зоне
#: ответственности задачи И-8, исключение снимает И-10 вместе с файлом.
_EXEMPT_INLINE_STYLE = {"_stage_todo.html"}

_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{3,6}\b")


def _template_files() -> list[Path]:
    """Все HTML-шаблоны интерфейса, отсортированные для стабильного вывода."""
    return sorted(_TEMPLATES_DIR.glob("*.html"))


def test_no_inline_style_outside_exempt_file():
    """Ни один шаблон, кроме заглушки стадии, не содержит атрибута `style=`."""
    violations: list[str] = []
    for path in _template_files():
        if path.name in _EXEMPT_INLINE_STYLE:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, start=1):
            if "style=" in line:
                violations.append(f"{path.name}:{lineno}")
    assert not violations, "инлайновый style= найден: " + ", ".join(violations)


def test_no_hex_colors_in_templates():
    """Цвета живут только в `theme.css` — в шаблонах не должно быть hex-литералов."""
    violations: list[str] = []
    for path in _template_files():
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, start=1):
            if _HEX_COLOR.search(line):
                violations.append(f"{path.name}:{lineno}")
    assert not violations, "hex-цвет найден в шаблоне: " + ", ".join(violations)


@pytest.mark.parametrize("path", ["/", "/requests", "/showcase"])
def test_page_renders_without_errors(monkeypatch: pytest.MonkeyPatch, path: str):
    """Страницы отдают 200 при пустой базе и не содержат инлайновых `style=`.

    Пароль — латиницей: `HTTPBasic` декодирует заголовок `Authorization` не в
    UTF-8, кириллица через настоящий HTTP-запрос до сравнения не доходит
    (в отличие от прямого вызова `require_auth` в `test_web_auth.py`).
    """
    monkeypatch.setattr(main.settings, "web_auth_user", "scout")
    monkeypatch.setattr(main.settings, "web_auth_password", SecretStr("parol123"))
    with TestClient(main.app) as client:
        response = client.get(path, auth=("scout", "parol123"))
    assert response.status_code == 200
    assert "style=" not in response.text
