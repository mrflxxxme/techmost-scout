"""Страж РЕШ-267: два рубежа пароля делят один секрет.

Caddy (`basic_auth`) и приложение (`require_auth`, РЕШ-16) читают один и тот
же заголовок `Authorization`: браузер спрашивает пароль один раз и шлёт его
обоим. При разных паролях вход в браузере невозможен — так и случилось на
деплое этапа 5. Боевые значения в репозиторий не попадают (инвариант 3),
поэтому страж держит то, что видно из репозитория: логин `Caddyfile` равен
умолчанию приложения, хэш по инструкции считается от `WEB_AUTH_PASSWORD`, а
проверка деплоя ловит расхождение до того, как его найдёт браузер.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
CADDYFILE = REPO_ROOT / "docker" / "Caddyfile"
DEPLOY_MD = REPO_ROOT / "docs" / "deploy.md"


def _deploy_section(number: int) -> str:
    """Возвращает текст раздела `## N.` из docs/deploy.md до следующего раздела."""
    text = DEPLOY_MD.read_text(encoding="utf-8")
    match = re.search(rf"^## {number}\..*?(?=^## \d+\.|\Z)", text, re.MULTILINE | re.DOTALL)
    assert match is not None, f"в docs/deploy.md нет раздела {number}"
    return match.group(0)


def test_логин_basic_auth_равен_умолчанию_приложения() -> None:
    """Логин в `basic_auth` — тот же, что `WEB_AUTH_USER` по умолчанию: иначе
    рубежи расходятся ещё до пароля."""
    text = CADDYFILE.read_text(encoding="utf-8")
    block = re.search(r"basic_auth\s+@\w+\s*\{\s*(\S+)\s+\S+\s*\}", text)
    assert block is not None, "в Caddyfile не найден блок basic_auth с парой логин–хэш"
    assert block.group(1) == Settings.model_fields["web_auth_user"].default, (
        "логин basic_auth в Caddyfile не совпадает с умолчанием WEB_AUTH_USER"
    )


def test_caddyfile_не_обещает_разных_паролей() -> None:
    """Комментарий у `basic_auth` требует общий секрет и ссылается на РЕШ-267;
    прежняя формулировка «не один и тот же дважды» и привела к двум паролям."""
    text = CADDYFILE.read_text(encoding="utf-8")
    assert "РЕШ-267" in text and "WEB_AUTH_PASSWORD" in text, (
        "комментарий Caddyfile не связывает basic_auth с WEB_AUTH_PASSWORD (РЕШ-267)"
    )
    assert "не один и тот же" not in text, "Caddyfile снова советует разные пароли рубежей"


def test_раздел_caddy_требует_совпадения_и_считает_хэш_от_env() -> None:
    """Раздел 4 требует совпадения логина и пароля, а хэш считается прямо от
    `WEB_AUTH_PASSWORD` из `.env` — переписанный руками пароль и есть путь к
    расхождению."""
    section = _deploy_section(4)
    assert "обязаны совпадать" in section and "РЕШ-267" in section, (
        "раздел 4 docs/deploy.md больше не требует совпадения паролей рубежей"
    )
    assert "`WEB_AUTH_USER`" in section and "`WEB_AUTH_PASSWORD`" in section
    commands = re.findall(r"^.*caddy hash-password.*$", section, re.MULTILINE)
    assert commands, "в разделе 4 нет команды хеширования пароля"
    for command in commands:
        assert "WEB_AUTH_PASSWORD" in command, f"хэш считается не от WEB_AUTH_PASSWORD из .env: {command.strip()!r}"


def test_проверка_деплоя_ловит_расхождение() -> None:
    """Раздел 7 проходит через Caddy к защищённой странице с логином и паролем
    из настроек приложения и ждёт 200 — расхождение рубежей даст 401."""
    section = _deploy_section(7)
    probe = re.search(r"^curl .*web_auth_password.*https://<ДОМЕН>/\s*$", section, re.MULTILINE)
    assert probe is not None, "в разделе 7 нет проверки входа через Caddy паролем приложения (WEB_AUTH_PASSWORD)"
    assert "`200`" in section and "`401`" in section


def test_edge_прокси_тоже_с_общим_паролем() -> None:
    """Раздел 11 (общий сервер): `basic_auth` edge-блока — те же логин и
    пароль приложения."""
    section = _deploy_section(11)
    assert "РЕШ-267" in section and "`WEB_AUTH_PASSWORD`" in section, (
        "раздел 11 docs/deploy.md не требует общего пароля для edge-прокси"
    )
