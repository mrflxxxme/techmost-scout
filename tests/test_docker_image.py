"""Машинные стражи содержимого `docker/Dockerfile` и `.dockerignore`.

Демона Docker в среде разработки нет — образ не собирается и не запускается
(§ «Чем этап проверяется без демона Docker» в `.planning/PLAN.md`). Единственный
доступный способ закрепить состав боевого образа — разобрать `Dockerfile` как
текст и проверить наличие обязательных директив построчно.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


def _lines() -> list[str]:
    return DOCKERFILE.read_text(encoding="utf-8").splitlines()


def test_base_image_is_python_slim() -> None:
    """Базовый образ — `python:3.12-slim`, а не полнообразный `python:3.12`."""
    lines = _lines()
    from_lines = [line for line in lines if line.startswith("FROM ")]
    assert from_lines, "в Dockerfile должна быть директива FROM"
    assert from_lines[0].strip() == "FROM python:3.12-slim"


def test_install_uses_export_extra() -> None:
    """Ставится группа зависимостей `export` (docx и playwright), а не голый пакет."""
    content = DOCKERFILE.read_text(encoding="utf-8")
    assert ".[export]" in content, "установка должна включать группу export"


def test_playwright_installs_chromium_with_deps() -> None:
    """Chromium ставится вместе с системными библиотеками (`--with-deps`)."""
    content = DOCKERFILE.read_text(encoding="utf-8")
    assert "playwright install --with-deps chromium" in content


def test_playwright_browsers_path_outside_root_home() -> None:
    """Каталог браузеров задан явно и не лежит в домашней папке root."""
    lines = _lines()
    matches = [line for line in lines if "PLAYWRIGHT_BROWSERS_PATH" in line]
    assert matches, "должна быть переменная PLAYWRIGHT_BROWSERS_PATH"
    value = matches[0].split("=", 1)[1].strip()
    assert value, "у PLAYWRIGHT_BROWSERS_PATH должно быть значение"
    assert not value.startswith("/root"), "каталог браузера не должен быть под /root"


def test_pythonpath_points_to_backend_tree() -> None:
    """`PYTHONPATH` указывает на дерево исходников внутри /srv/scout."""
    content = DOCKERFILE.read_text(encoding="utf-8")
    assert "ENV PYTHONPATH=/srv/scout/backend" in content


def test_user_directive_is_non_root_and_after_last_run() -> None:
    """Последняя директива `USER` — не root, и она стоит после последнего `RUN`."""
    lines = _lines()
    user_indices = [i for i, line in enumerate(lines) if line.strip().startswith("USER ")]
    run_indices = [i for i, line in enumerate(lines) if line.strip().startswith("RUN ")]
    assert user_indices, "в Dockerfile должна быть директива USER"
    assert run_indices, "в Dockerfile должна быть хотя бы одна директива RUN"

    last_user_line = lines[user_indices[-1]].strip()
    last_user = last_user_line.removeprefix("USER ").strip()
    assert last_user != "root", "процесс приложения не должен работать от root"

    assert user_indices[-1] > run_indices[-1], (
        "последний USER должен стоять после последнего RUN — "
        "иначе смена владельца каталогов в RUN выполнится уже под непривилегированным "
        "пользователем и упадёт"
    )


def test_healthcheck_present_and_checks_health_endpoint() -> None:
    """Есть `HEALTHCHECK`, и его команда обращается к `/health`."""
    content = DOCKERFILE.read_text(encoding="utf-8")
    assert "HEALTHCHECK" in content
    healthcheck_start = content.index("HEALTHCHECK")
    # Команда здоровья может занимать несколько строк из-за переноса `\`,
    # поэтому ищем /health в остатке файла начиная с директивы.
    tail = content[healthcheck_start:]
    assert "/health" in tail


def test_dockerignore_file_exists() -> None:
    """`.dockerignore` существует в корне репозитория."""
    assert DOCKERIGNORE.exists(), ".dockerignore должен лежать в корне репозитория"


def test_dockerignore_excludes_secrets_and_dev_artifacts() -> None:
    """Секреты, боевая база и служебные каталоги не попадают в контекст сборки."""
    lines = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    required_exact = [".env", ".git", ".planning", "tests", "out"]
    for entry in required_exact:
        assert entry in lines, f"в .dockerignore должна быть строка {entry!r}"

    db_patterns = [line for line in lines if "db" in line and "*" in line]
    assert db_patterns, "должен быть шаблон, закрывающий файлы боевой базы (data/*.db)"
