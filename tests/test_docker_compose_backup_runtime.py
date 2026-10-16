"""Сквозной прогон тела скрипта сайдкара docker-compose.yml (сервис backup).

Вынесено из ``test_docker_compose.py``, чтобы тот не переваливал за 500 строк
(страж ``tests/test_tests_layout.py``): здесь — исполняющие помощники и
тесты, которые запускают тело скрипта сайдкара как обычный Python-код на
временных файлах, тем же способом, каким аудитор Э5 воспроизводил отказы.
Статические и структурные проверки самого ``docker-compose.yml`` остаются в
``test_docker_compose.py`` и переиспользуются отсюда прямым импортом — в
``tests/`` нет ``__init__.py``, поэтому pytest уже добавил каталог в
``sys.path`` как обычный модуль.
Демона Docker и сети нет (инвариант №7): исполнение локальное, in-process.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import io
import os
import re
import sqlite3
import sys
import threading
import time
import types
from pathlib import Path

from test_docker_compose import _backup_script, _find_calls


class _StopAfterFirstIterationError(Exception):
    """Сигнал для выхода из ``while True`` тела сайдкара после одного прохода."""


def _namespace_local_module(real_module: types.ModuleType, **overrides: object) -> types.ModuleType:
    """Копирует модуль и подменяет отдельные атрибуты только в копии.

    Совет 2 повторного аудита Э5: прежняя версия подменяла ``time.sleep`` и
    ``sys.stderr`` через ``unittest.mock.patch`` на настоящих модулях внутри
    ``with``-блока daemon-потока. Пока поток жив, подмена действует глобально
    на весь процесс — а ровно в том отказе, ради которого тест написан
    (проход не завершился), поток daemon остаётся жить вечно, ``with`` не
    выходит, и патч не снимается: все последующие тесты сессии получают
    чужой ``time.sleep`` и перехваченный ``sys.stderr``. Копия модуля живёт
    только в пространстве имён исполняемого скрипта (см. ``_local_import``
    в ``_run_backup_body``) и не трогает реальные ``time``/``sys`` даже при
    вечном зависании.
    """
    fake = types.ModuleType(real_module.__name__)
    for name in dir(real_module):
        if not name.startswith("__"):
            setattr(fake, name, getattr(real_module, name))
    for name, value in overrides.items():
        setattr(fake, name, value)
    return fake


def _run_backup_body(
    data_dir: Path,
    backups_dir: Path,
    timeout_override: int | None = None,
    pages_override: int | None = None,
) -> tuple[threading.Thread, io.StringIO]:
    """Запускает тело скрипта сайдкара один раз в отдельном daemon-потоке.

    Пути ``/data`` и ``/backups`` подменяются на временные каталоги теста;
    ``time.sleep`` в конце цикла подменяется исключением — иначе ``while
    True`` дождался бы настоящих суток. Поток — daemon, чтобы зависший
    сайдкар (ровно тот отказ, который здесь и проверяется) не подвесил
    тест: вызывающий код сам решает, сколько ждать через ``join(timeout=)``.

    Подмены ``time.sleep`` и ``sys.stderr`` передаются скрипту через
    локальный ``__import__`` в его собственном пространстве имён — не через
    ``unittest.mock.patch`` реальных модулей (см. ``_namespace_local_module``).
    """
    script = _backup_script()
    script = script.replace("/data/scout.db", str(data_dir / "scout.db"))
    script = script.replace("/backups", str(backups_dir))
    if timeout_override is not None:
        script, count = re.subn(
            r"LOCK_WAIT_TIMEOUT_SECONDS = -?\d+",
            f"LOCK_WAIT_TIMEOUT_SECONDS = {timeout_override}",
            script,
        )
        assert count == 1, "константа предела ожидания не найдена для подмены в тесте"
    if pages_override is not None:
        script, count = re.subn(r"pages=-?\d+", f"pages={pages_override}", script)
        assert count == 1, "аргумент pages= не найден для подмены в тесте"

    stderr_buffer = io.StringIO()

    def _sleep_and_stop(_seconds: float) -> None:
        raise _StopAfterFirstIterationError

    fake_time = _namespace_local_module(time, sleep=_sleep_and_stop)
    fake_sys = _namespace_local_module(sys, stderr=stderr_buffer)

    def _local_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "time":
            return fake_time
        if name == "sys":
            return fake_sys
        return builtins.__import__(name, globals, locals, fromlist, level)

    namespace_builtins = dict(vars(builtins))
    namespace_builtins["__import__"] = _local_import

    def _target() -> None:
        namespace = {"__name__": "__backup_script_test__", "__builtins__": namespace_builtins}
        with contextlib.suppress(_StopAfterFirstIterationError):
            # Тело сайдкара исполняется как код теста (без Docker и сети) —
            # тем же способом аудит Э5 воспроизвёл все три отказа. exec() —
            # не бизнес-логика продукта, а инструмент теста над извлечённым
            # текстом скрипта из docker-compose.yml.
            exec(compile(script, "<backup-script>", "exec"), namespace)  # noqa: S102

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    return thread, stderr_buffer


def _make_source_database(path: Path) -> None:
    """Создаёт крошечную боевую базу для теста — одна таблица, одна строка."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()


def _make_large_source_database(path: Path, size_bytes: int) -> None:
    """Создаёт базу с одним большим BLOB — быстрый способ получить файл из
    многих страниц (по 4 КБ), не гоняя тысячи отдельных INSERT из Python.
    Нужна тестам совета 4 аудита: разница pages=1 и pages=-1 видна только
    на источнике с достаточным числом страниц для копирования.
    """
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (?)", (b"\0" * size_bytes,))
    conn.commit()
    conn.close()


def test_backup_успешный_проход_создаёт_годную_копию(tmp_path):
    """Сквозной прогон тела скрипта (без Docker и сети): при доступной базе
    и снятой блокировке копия должна появиться под итоговым именем и быть
    непустой.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    _make_source_database(data_dir / "scout.db")

    thread, stderr_buffer = _run_backup_body(data_dir, backups_dir, timeout_override=10)
    thread.join(timeout=10)

    assert not thread.is_alive(), "проход не завершился за разумное время"
    assert stderr_buffer.getvalue() == "", f"неожиданный отказ: {stderr_buffer.getvalue()}"
    copies = list(backups_dir.glob("scout-*.db"))
    assert len(copies) == 1, "ожидалась ровно одна готовая копия"
    assert copies[0].stat().st_size > 0, "копия создана пустой"
    assert not list(backups_dir.glob("*.part")), "временный файл не удалён после успеха"


def test_backup_прерванный_проход_не_оставляет_файл_пустышку(tmp_path):
    """Сквозной прогон правки 2: принудительный обрыв копирования (через тот
    же предел ожидания, выставленный в отрицательное значение — копия
    прерывается на первом же шаге) не должен оставлять в каталоге копий ни
    временного файла, ни файла-пустышки с именем годной копии.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    _make_source_database(data_dir / "scout.db")

    thread, stderr_buffer = _run_backup_body(data_dir, backups_dir, timeout_override=-1)
    thread.join(timeout=10)

    assert not thread.is_alive(), "проход не завершился за разумное время"
    assert "копия базы не выполнена" in stderr_buffer.getvalue()
    assert list(backups_dir.iterdir()) == [], "после отказа в каталоге копий не должно остаться файлов"


def test_backup_ожидание_снятия_блокировки_ограничено(tmp_path):
    """Сквозной прогон правки 3 при настоящей удерживаемой эксклюзивной
    транзакции: аудит воспроизвёл зависание больше чем на сто секунд до
    принудительного обрыва. Здесь предел явно занижен (2 с), и проход
    обязан завершиться за разумное время, а не повиснуть.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    db_path = data_dir / "scout.db"
    _make_source_database(db_path)

    writer = sqlite3.connect(db_path, timeout=0)
    writer.execute("BEGIN EXCLUSIVE")
    writer.execute("INSERT INTO t VALUES (2)")
    try:
        start = time.monotonic()
        thread, stderr_buffer = _run_backup_body(data_dir, backups_dir, timeout_override=2)
        thread.join(timeout=15)
        elapsed = time.monotonic() - start

        assert not thread.is_alive(), (
            "проход не завершился за 15 с при удерживаемой блокировке — "
            "предел ожидания не работает, повторяется отказ аудита (>100 с)"
        )
        assert elapsed < 15, f"проход занял {elapsed:.1f} с — предел ожидания не ограничивает время"
        assert "копия базы не выполнена" in stderr_buffer.getvalue()
        assert list(backups_dir.iterdir()) == [], "при отказе из-за блокировки не должно остаться файлов"
    finally:
        writer.rollback()
        writer.close()


def test_backup_зависший_проход_не_подменяет_настоящие_time_и_sys(tmp_path):
    """Совет 2 повторного аудита, сквозной прогон: сценарий с зависанием
    (реальная удерживаемая эксклюзивная блокировка и заведомо огромный
    предел ожидания, который точно не сработает за время короткого join)
    не должен подменять настоящие ``time.sleep`` и ``sys.stderr`` — даже
    пока daemon-поток всё ещё жив внутри такого «отказа».

    До правки подмены жили в ``unittest.mock.patch`` на настоящих модулях
    внутри ``with``-блока daemon-потока: пока поток жив (а именно так и
    остаётся в этом сценарии), подмена действует на весь процесс, и
    следующий тест в этой же сессии получил бы чужой ``time.sleep`` и
    перехваченный ``sys.stderr``. После правки подмены живут только в
    пространстве имён исполняемого скрипта, поэтому даже вечно висящий
    поток не портит настоящие модули.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    db_path = data_dir / "scout.db"
    _make_source_database(db_path)

    real_sleep = time.sleep
    real_stderr = sys.stderr

    writer = sqlite3.connect(db_path, timeout=0)
    writer.execute("BEGIN EXCLUSIVE")
    writer.execute("INSERT INTO t VALUES (2)")
    try:
        # Огромный предел ожидания — сценарий гарантированно "виснет" в
        # пределах короткого join(): именно так выглядел отказ аудита,
        # который сломал остаток тестовой сессии.
        thread, _ = _run_backup_body(data_dir, backups_dir, timeout_override=3600)
        thread.join(timeout=0.5)
        assert thread.is_alive(), "поток должен ещё висеть на блокировке — это и есть проверяемый отказ"

        assert time.sleep is real_sleep, "настоящий time.sleep оказался подменён во время зависания"
        assert sys.stderr is real_stderr, "настоящий sys.stderr оказался подменён во время зависания"
        # Реальный time.sleep обязан работать как обычно, а не поднимать
        # исключение, которым помечен конец итерации внутри скрипта.
        real_sleep(0)
    finally:
        writer.rollback()
        writer.close()


def test_backup_после_зависшего_прохода_time_sleep_работает_как_обычно():
    """Следующий тест сессии после сценария с зависанием (совет 2 аудита):
    настоящий ``time.sleep`` не должен быть унаследованной подменой из
    предыдущего теста. Порядок тестов в файле — определения (в проекте нет
    плагина случайного порядка), поэтому этот тест идёт сразу за тем, что
    создаёт зависший поток.
    """
    started = time.monotonic()
    time.sleep(0.01)
    assert time.monotonic() - started >= 0.01, "time.sleep не проспал положенное время — модуль подменён"


def test_backup_ротация_учитывает_и_part_файлы():
    """Совет 3 аудита: при жёстком убийстве контейнера между записью
    временного файла и его переименованием (``os.rename`` в скрипте)
    огрызок ``scout-<дата>.db.part`` не попадает под шаблон обычных копий
    и без второго шаблона остаётся в томе навсегда.
    """
    tree = ast.parse(_backup_script())
    glob_calls = _find_calls(tree, "glob")
    patterns = {call.args[0].value for call in glob_calls if call.args and isinstance(call.args[0], ast.Constant)}
    assert "/backups/scout-*.db" in patterns, "ротация не ищет обычные копии"
    assert "/backups/scout-*.db.part" in patterns, "ротация не ищет огрызки .part"


def test_backup_ротация_удаляет_старые_part_файлы(tmp_path):
    """Сквозной прогон совета 3: устаревший огрызок ``.part`` должен быть
    удалён той же ротацией, что и обычные копии.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    _make_source_database(data_dir / "scout.db")

    orphan = backups_dir / "scout-20200101-000000.db.part"
    orphan.write_bytes(b"orphan")
    old_time = time.time() - 15 * 86400
    os.utime(orphan, (old_time, old_time))

    thread, stderr_buffer = _run_backup_body(data_dir, backups_dir, timeout_override=10)
    thread.join(timeout=10)

    assert not thread.is_alive(), "проход не завершился за разумное время"
    assert stderr_buffer.getvalue() == "", f"неожиданный отказ: {stderr_buffer.getvalue()}"
    assert not orphan.exists(), "устаревший .part файл должен быть удалён ротацией"


def test_backup_копирует_постранично_а_не_одним_шагом():
    """Совет 4 аудита: ``pages=1`` обязателен, чтобы предел ожидания успевал
    прерывать медленное (но не заблокированное) копирование — при
    ``pages=-1`` весь файл копируется одним шагом, и callback срабатывает
    только после того, как копирование уже закончилось (см. сквозной тест
    ниже: на медленном источнике предел в этом случае перестаёт что-либо
    прерывать, оставаясь рабочим только на блокировке).
    """
    tree = ast.parse(_backup_script())
    calls = [call for call in _find_calls(tree, "backup") if any(kw.arg == "pages" for kw in call.keywords)]
    assert len(calls) == 1, "ожидался ровно один вызов backup() с именованным аргументом pages"
    pages_kw = next(kw for kw in calls[0].keywords if kw.arg == "pages")
    assert isinstance(pages_kw.value, ast.Constant), "pages= обязан быть числовым литералом"
    assert pages_kw.value.value == 1, "pages должен быть 1 — постраничное копирование"


def test_backup_pages_1_позволяет_прервать_медленное_копирование(tmp_path):
    """Сквозной прогон совета 4: на источнике без блокировки (никто не
    держит транзакцию) с ``pages=1`` предел ожидания обрывает копирование
    почти мгновенно, а с ``pages=-1`` — только после того, как файл уже
    скопирован целиком одним шагом. Порог сравнения — не абсолютное время
    (оно зависит от диска), а кратное отличие: p1 «спотыкается» о первую же
    страницу, p(-1) — ждёт весь файл.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    db_path = data_dir / "scout.db"
    # ~50 МБ даёт тысячи страниц — заметную разницу между "одна страница"
    # и "весь файл одним шагом" даже на быстром диске (аудит измерил на
    # почти пятикратно меньшей базе 0,03 с без писателя).
    _make_large_source_database(db_path, size_bytes=50_000_000)

    def _measure(pages: int) -> tuple[float, str]:
        run_backups_dir = backups_dir / f"pages_{pages}"
        run_backups_dir.mkdir()
        started = time.monotonic()
        # LOCK_WAIT_TIMEOUT_SECONDS=0 — предел просрочен уже на первом
        # вызове callback, поэтому дальше решает исключительно частота его
        # вызовов, то есть pages.
        thread, stderr_buffer = _run_backup_body(data_dir, run_backups_dir, timeout_override=0, pages_override=pages)
        thread.join(timeout=30)
        elapsed = time.monotonic() - started
        assert not thread.is_alive(), f"проход pages={pages} не завершился за разумное время"
        return elapsed, stderr_buffer.getvalue()

    elapsed_page_1, stderr_1 = _measure(1)
    elapsed_page_all, stderr_all = _measure(-1)

    assert "копия базы не выполнена" in stderr_1
    assert "копия базы не выполнена" in stderr_all
    assert elapsed_page_1 < 0.5, f"pages=1 обязан прерваться почти сразу, заняло {elapsed_page_1:.3f} с"
    assert elapsed_page_all > elapsed_page_1 * 3, (
        "pages=-1 обязан копировать файл целиком одним шагом, прежде чем предел "
        f"вообще заметит просрочку: pages=1 занял {elapsed_page_1:.3f} с, "
        f"pages=-1 занял {elapsed_page_all:.3f} с — разница слишком мала"
    )
