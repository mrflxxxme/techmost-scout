"""Машинные стражи содержимого `docker/Dockerfile` и `.dockerignore`.

Демона Docker в среде разработки нет — образ не собирается и не запускается
(§ «Чем этап проверяется без демона Docker» в `.planning/PLAN.md`). Единственный
доступный способ закрепить состав боевого образа — разобрать `Dockerfile` как
текст и проверить наличие обязательных директив построчно.
"""

from __future__ import annotations

import fnmatch
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


def test_playwright_browsers_path_declared_before_install() -> None:
    """`ENV PLAYWRIGHT_BROWSERS_PATH` стоит раньше `playwright install`.

    Инструкции Dockerfile выполняются по порядку. Если переменная объявлена
    ниже установки, playwright в момент установки её ещё не видит и кладёт
    браузер в домашний кэш root (`/root/.cache/ms-playwright`) — каталог
    `/ms-playwright` не появляется никогда, и следующий `RUN chmod -R a+rX
    /ms-playwright` падает с кодом 1: сборка образа обрывается на первой же
    команде владельца (находка аудита Б1). Тест сравнивает индексы строк, а
    не просто наличие обеих директив — только так порядок закреплён.

    Подстроку `playwright install` ищем только внутри блока команды `RUN`
    (с учётом переноса строк через обратную косую черту в конце строки),
    а не в любой строке файла:
    комментарий с этими же словами, поясняющий находку Б1 и стоящий выше
    `ENV`, не является исполняемой командой и не должен красить тест на
    пустом месте (пост-аудит Э5, обратная хрупкость, совет 6).
    """
    lines = _lines()
    env_indices = [i for i, line in enumerate(lines) if line.strip().startswith("ENV PLAYWRIGHT_BROWSERS_PATH")]
    install_indices: list[int] = []
    in_run_block = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("RUN"):
            in_run_block = True
        if in_run_block and "playwright install" in line:
            install_indices.append(i)
        if not stripped.endswith("\\"):
            in_run_block = False
    assert env_indices, "должна быть директива ENV PLAYWRIGHT_BROWSERS_PATH"
    assert install_indices, "должна быть команда playwright install"
    assert env_indices[0] < install_indices[0], (
        "ENV PLAYWRIGHT_BROWSERS_PATH должен стоять раньше playwright install, "
        "иначе браузер уедет в домашний кэш root, а следующий chmod упадёт"
    )


def test_playwright_browsers_path_outside_root_home() -> None:
    """Каталог браузеров задан явно и не лежит в домашней папке root."""
    lines = _lines()
    matches = [line for line in lines if line.strip().startswith("ENV PLAYWRIGHT_BROWSERS_PATH")]
    assert matches, "должна быть директива ENV PLAYWRIGHT_BROWSERS_PATH"
    value = matches[0].split("=", 1)[1].strip()
    assert value, "у PLAYWRIGHT_BROWSERS_PATH должно быть значение"
    assert not value.startswith("/root"), "каталог браузера не должен быть под /root"


def test_browsers_path_is_readable_by_non_root_user() -> None:
    """После `PLAYWRIGHT_BROWSERS_PATH` каталог браузера открывается на чтение всем.

    Chromium ставится под root в момент сборки, а процесс приложения работает
    от uid 10001 (`USER scout`). Без `chmod -R a+rX` на каталог браузера
    непривилегированный процесс не сможет ни прочитать бинарник, ни зайти в
    подкаталоги — печать PDF (`page.pdf` в export) упадёт на боевом сервере,
    хотя весь остальной набор тестов останется зелёным: playwright там не
    запускается. Пропажа этой строки — тихий дефект, поэтому она закреплена
    отдельным тестом, а не полагается на соседние проверки.
    """
    lines = _lines()
    browsers_path_indices = [
        i for i, line in enumerate(lines) if line.strip().startswith("ENV PLAYWRIGHT_BROWSERS_PATH")
    ]
    assert browsers_path_indices, "должна быть директива ENV PLAYWRIGHT_BROWSERS_PATH"
    browsers_path_value = lines[browsers_path_indices[0]].split("=", 1)[1].strip()

    chmod_lines = [line for line in lines if line.strip().startswith("RUN ") and "chmod" in line and "+rX" in line]
    assert chmod_lines, (
        "должна быть команда chmod, открывающая каталог браузера на чтение и "
        "выполнение для всех пользователей (a+rX), иначе процесс от scout не "
        "прочитает бинарник Chromium, поставленный от root"
    )
    assert browsers_path_value in chmod_lines[0], "chmod должен применяться именно к каталогу PLAYWRIGHT_BROWSERS_PATH"


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


def test_copy_sources_exist_in_build_context() -> None:
    """Каждый источник `COPY` реально существует в дереве репозитория.

    Опечатка в имени каталога-источника (`backend` → `backend_nope`, потеря
    `COPY prompts`) ничем, кроме реальной сборки, не ловится — а демона
    Docker в этой среде нет. Сверяем текстом Dockerfile с содержимым дерева.

    Ведущие флаги вида `--chown=root:root` отбрасываются перед проверкой:
    `COPY --chown=root:root backend ./backend` — законная форма директивы,
    а не опечатка в имени источника; без отбрасывания флаг сам попадал бы
    в список источников и не находился в дереве (пост-аудит Э5, находка L3).
    """
    lines = _lines()
    copy_lines = [line.strip() for line in lines if line.strip().startswith("COPY ")]
    assert copy_lines, "в Dockerfile должна быть хотя бы одна директива COPY"
    for line in copy_lines:
        parts = line.removeprefix("COPY ").split()
        *raw_sources, _dest = parts
        sources = [source for source in raw_sources if not source.startswith("--")]
        assert sources, f"у директивы COPY должен быть хотя бы один источник: {line!r}"
        for source in sources:
            path = REPO_ROOT / source
            assert path.exists(), f"источник COPY не найден в контексте сборки: {source!r}"


def test_all_required_copy_sources_are_present() -> None:
    """Обязательные источники `COPY` никуда не пропали.

    `test_copy_sources_exist_in_build_context` ловит опечатку в имени
    источника, но не ловит пропажу целой директивы `COPY` — удалённая строка
    просто не попадёт в список для проверки. Без `COPY prompts` пайплайн
    падает на первой же стадии, потому что промпты не читаются с диска.
    """
    content = DOCKERFILE.read_text(encoding="utf-8")
    required_sources = ["backend", "prompts", "pyproject.toml", "data/gov_support.yaml"]
    copy_lines = [line.strip() for line in content.splitlines() if line.strip().startswith("COPY ")]
    copied_sources: set[str] = set()
    for line in copy_lines:
        *sources, _dest = line.removeprefix("COPY ").split()
        copied_sources.update(sources)
    for source in required_sources:
        assert source in copied_sources, f"директива COPY {source!r} отсутствует в Dockerfile"


def test_cmd_binds_to_all_interfaces() -> None:
    """`uvicorn` слушает `0.0.0.0`, а не `127.0.0.1`.

    На `127.0.0.1` процесс виден только самому себе внутри контейнера — Caddy
    из соседнего контейнера до него не достучится, и на сайте будет 502.
    """
    content = DOCKERFILE.read_text(encoding="utf-8")
    assert '"--host", "0.0.0.0"' in content, "CMD должен биндиться на 0.0.0.0, не на 127.0.0.1"


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
    required_exact = [".git", ".planning", "tests", "out"]
    for entry in required_exact:
        assert entry in lines, f"в .dockerignore должна быть строка {entry!r}"

    db_patterns = [line for line in lines if "db" in line and "*" in line]
    assert db_patterns, "должен быть шаблон, закрывающий файлы боевой базы (data/*.db*)"


def test_dockerignore_secret_patterns_are_recursive() -> None:
    """Шаблоны секретов рекурсивные (`**/...`), а не только для корня репозитория.

    `docs/deploy.md` §3 велит создавать `docker/.env` — не в корне. Шаблон
    верхнего уровня `.env` его не поймает: файл попадёт в контекст сборки, а
    `.dockerignore` пообещает больше, чем сделает. Нужны `**/.env`,
    `**/.env.*`, `**/*.pem`, `**/*.key`.
    """
    lines = [line.strip() for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()]
    for pattern in ("**/.env", "**/.env.*", "**/*.pem", "**/*.key"):
        assert pattern in lines, f"в .dockerignore должен быть рекурсивный шаблон {pattern!r}"

    recursive_patterns = [line for line in lines if line.startswith("**/")]
    nested_secrets = ["docker/.env", "docker/.env.prod", "certs/server.pem", "certs/id_rsa.key"]
    for nested in nested_secrets:
        basename = nested.rsplit("/", 1)[-1]
        matched = any(fnmatch.fnmatch(basename, pattern.removeprefix("**/")) for pattern in recursive_patterns)
        assert matched, f"рекурсивный шаблон .dockerignore не закрывает вложенный файл {nested!r}"


def test_dockerignore_covers_backup_copies_of_database() -> None:
    """Шаблон закрывает резервные копии базы (`scout.db.bak-*`), а не только саму базу.

    План требовал `data/*.db*`, а в файле долго стоял `data/*.db` — тот не
    совпадает с `scout.db.bak-2026-08-20`: три копии боевой базы с реальными
    прогонами (12,1 МБ) уезжали в контекст сборки вместо ожидаемых 0,2 МБ.
    """
    lines = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    db_patterns = [line.strip() for line in lines if line.strip().startswith("data/") and "db" in line]
    assert db_patterns, "должен быть шаблон, закрывающий файлы боевой базы"

    sample_names = ["scout.db", "scout.db-journal", "scout.db-wal", "scout.db.bak-2026-08-20"]
    for name in sample_names:
        matched = any(fnmatch.fnmatch(f"data/{name}", pattern) for pattern in db_patterns)
        assert matched, f"шаблон .dockerignore не закрывает {name!r}"
