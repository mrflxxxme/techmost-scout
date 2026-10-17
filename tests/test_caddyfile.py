"""Проверки конфигурации Caddy как контракта деплоя.

`docker/Caddyfile` живёт вне обычного тестового прогона (это конфигурация
внешнего процесса, не код приложения), поэтому единственный способ закрепить
её содержимое — читать файл как текст. `caddy validate` в этой задаче не
запускается: без подставленного боевого хэша пароля он не проходит, а
подставлять боевой хэш в репозиторий запрещено инвариантом №3.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CADDYFILE = REPO_ROOT / "docker" / "Caddyfile"

#: Три плейсхолдера деплоя, закреплённые контрактом плана этапа Э5.
EXPECTED_PLACEHOLDERS = (
    "scout.example.ru",
    "admin@example.ru",
    "ХЭШ_ПАРОЛЯ_ЗАМЕНИТЬ_ПРИ_ДЕПЛОЕ",
)

#: Пять заголовков безопасности, обязательных в блоке `header`.
EXPECTED_HEADERS = (
    'Strict-Transport-Security "max-age=31536000"',
    'X-Content-Type-Options "nosniff"',
    'X-Frame-Options "DENY"',
    'Referrer-Policy "no-referrer"',
    'X-Robots-Tag "noindex, nofollow"',
)

#: Признаки настоящего bcrypt-хэша: строке с ними в репозитории не место.
BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")


def _read_caddyfile() -> str:
    """Читает содержимое Caddyfile как текст (без сети, без вызова caddy)."""
    return CADDYFILE.read_text(encoding="utf-8")


def test_caddyfile_существует() -> None:
    """Файл конфигурации должен физически присутствовать в репозитории."""
    assert CADDYFILE.is_file(), "docker/Caddyfile не найден"


def test_глобальный_блок_с_email() -> None:
    """Глобальный блок с почтой ACME обязателен: без него Let's Encrypt
    выпускает сертификат без контакта для уведомлений об истечении."""
    text = _read_caddyfile()
    global_block_match = re.search(r"^\{\s*$.*?^\}\s*$", text, re.MULTILINE | re.DOTALL)
    assert global_block_match is not None, "в файле нет глобального блока `{ ... }` в начале"
    assert "email admin@example.ru" in global_block_match.group(0), (
        "в глобальном блоке нет директивы `email` с плейсхолдером почты"
    )


def test_все_три_плейсхолдера_на_месте() -> None:
    """Все три плейсхолдера деплоя из контракта плана должны присутствовать."""
    text = _read_caddyfile()
    for placeholder in EXPECTED_PLACEHOLDERS:
        assert placeholder in text, f"плейсхолдер {placeholder!r} отсутствует в Caddyfile"


def test_reverse_proxy_и_basic_auth_настроены() -> None:
    """Прокси должен пробрасывать трафик на приложение и требовать пароль."""
    text = _read_caddyfile()
    assert "reverse_proxy scout:8000" in text, "нет директивы reverse_proxy на сервис приложения"
    assert "basic_auth" in text, "нет блока basic_auth — второй рубеж пароля отсутствует"


def test_basic_auth_пропускает_только_api_v1() -> None:
    """РЕШ-250: `/api/v1/*` защищён ключом приложения и идёт мимо пароля
    прокси; всё остальное — по-прежнему под `basic_auth`. Пароль навешен через
    именованный матчер, исключающий ровно `/api/v1/*`: голый `basic_auth {`
    без матчера закрыл бы API, а матчер шире (`/api*`) открыл бы схему API
    `/api/docs` и всё, что когда-нибудь ляжет рядом."""
    text = _read_caddyfile()
    matcher = re.search(r"^\s*@(\w+)\s+not\s+path\s+(\S+)\s*$", text, re.MULTILINE)
    assert matcher is not None, "нет матчера `@имя not path …` для basic_auth"
    assert matcher.group(2) == "/api/v1/*", f"матчер исключает {matcher.group(2)!r}, а не ровно /api/v1/*"
    assert re.search(rf"^\s*basic_auth\s+@{matcher.group(1)}\s*\{{", text, re.MULTILINE), (
        "basic_auth не привязан к матчеру — либо закрыт API, либо пароль снят со всего сайта"
    )
    assert len(re.findall(r"^\s*basic_auth\b", text, re.MULTILINE)) == 1, "basic_auth должен быть ровно один"


def test_пять_заголовков_безопасности() -> None:
    """В блоке `header` должны присутствовать все пять заголовков защиты."""
    text = _read_caddyfile()
    header_block_match = re.search(r"header\s*\{(.*?)\}", text, re.DOTALL)
    assert header_block_match is not None, "в файле нет блока `header { ... }`"
    header_block = header_block_match.group(1)
    for header in EXPECTED_HEADERS:
        assert header in header_block, f"заголовок {header!r} отсутствует в блоке header"


def test_нет_настоящего_bcrypt_хэша() -> None:
    """Инвариант №3: в репозитории не должно быть подстрок, характерных для
    настоящего bcrypt-хэша. Сегодняшний плейсхолдер их не содержит, но и не
    выглядит как заведомый плейсхолдер — этот критерий закрывает обе беды."""
    text = _read_caddyfile()
    for prefix in BCRYPT_PREFIXES:
        assert prefix not in text, f"в Caddyfile найдена подстрока {prefix!r} — похоже на настоящий bcrypt-хэш"


def test_объём_файла_в_бюджете() -> None:
    """Файл остаётся в пределах бюджета плана — не больше 45 строк."""
    lines = _read_caddyfile().splitlines()
    assert len(lines) <= 45, f"Caddyfile вырос до {len(lines)} строк, бюджет — 45"
