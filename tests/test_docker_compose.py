"""Проверки связки docker-compose.yml: тома, ожидание живости, копия базы.

Часть тестов разбирает файл через ``yaml.safe_load`` (инвариант №10) или
``ast`` — структурный анализ скрипта сайдкара без магических подстрок. Часть
исполняет тело скрипта сайдкара как обычный Python-код на временных файлах —
так же, как аудитор Э5 воспроизводил отказы («прогонов тела скрипта»).
Демона Docker и сети нет (инвариант №7): исполнение локальное, in-process.
"""

from __future__ import annotations

import ast
import contextlib
import io
import re
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

import yaml

COMPOSE_PATH = Path(__file__).resolve().parents[1] / "docker" / "docker-compose.yml"


def _load() -> dict:
    """Разбирает docker-compose.yml через yaml.safe_load."""
    with COMPOSE_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _text() -> str:
    """Возвращает содержимое docker-compose.yml как текст."""
    return COMPOSE_PATH.read_text(encoding="utf-8")


def _backup_script() -> str:
    """Возвращает исходный текст Python-скрипта сервиса backup."""
    command = _load()["services"]["backup"]["command"]
    scripts = [part for part in command if isinstance(part, str) and "\n" in part]
    assert len(scripts) == 1, "ожидается ровно один многострочный аргумент -c"
    return scripts[0]


def _script_constant(script: str, name: str) -> int:
    """Возвращает точное целое значение присваивания ``name = <число>`` в скрипте.

    Значение берётся разбором дерева, а не поиском по тексту, и это защита
    сразу от двух бед. Подстрока слишком слаба: "14" входит и в "140", то есть
    предел вдесятеро слабее заявленного прошёл бы незамеченным. Регулярное
    выражение с якорями на всю строку слишком хрупко: оно роняет проверку на
    законной записи того же числа — с хвостовым комментарием или разделителем
    разрядов (``86_400``). Дерево не видит ни пробелов, ни комментариев, ни
    формы литерала — только значение.
    """
    tree = ast.parse(script)
    values = [
        ast.literal_eval(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id == name and isinstance(node.value, ast.Constant)
    ]
    assert len(values) == 1, f"ожидалось ровно одно присваивание {name} = <число>, найдено {len(values)}"
    assert isinstance(values[0], int), f"{name} присвоено не целое число: {values[0]!r}"
    return values[0]


def _try_node_containing(tree: ast.Module, marker: str) -> ast.Try:
    """Находит блок try, в теле (не в except) которого встречается ``marker``.

    Различает две независимые ветки отказа сайдкара — саму копию и ротацию —
    по содержимому их тела, а не по порядку в тексте.
    """
    candidates = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            body_source = "\n".join(ast.unparse(stmt) for stmt in node.body)
            if marker in body_source:
                candidates.append(node)
    assert len(candidates) == 1, f"ожидался ровно один try с '{marker}' в теле, найдено {len(candidates)}"
    return candidates[0]


def _handlers_print_to_stderr(node: ast.Try) -> bool:
    """Проверяет, что хотя бы один обработчик except печатает в stderr."""
    for handler in node.handlers:
        source = "\n".join(ast.unparse(stmt) for stmt in handler.body)
        if "print(" in source and "sys.stderr" in source:
            return True
    return False


def _find_calls(tree: ast.AST, attr_name: str) -> list[ast.Call]:
    """Возвращает все вызовы вида ``<что-то>.attr_name(...)`` в поддереве."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attr_name
    ]


class _StopAfterFirstIterationError(Exception):
    """Сигнал для выхода из ``while True`` тела сайдкара после одного прохода."""


def _run_backup_body(
    data_dir: Path,
    backups_dir: Path,
    timeout_override: int | None = None,
) -> tuple[threading.Thread, io.StringIO]:
    """Запускает тело скрипта сайдкара один раз в отдельном daemon-потоке.

    Пути ``/data`` и ``/backups`` подменяются на временные каталоги теста;
    ``time.sleep`` в конце цикла подменяется исключением — иначе ``while
    True`` дождался бы настоящих суток. Поток — daemon, чтобы зависший
    сайдкар (ровно тот отказ, который здесь и проверяется) не подвесил
    тест: вызывающий код сам решает, сколько ждать через ``join(timeout=)``.
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

    stderr_buffer = io.StringIO()

    def _target() -> None:
        with (
            patch("time.sleep", side_effect=_StopAfterFirstIterationError),
            patch("sys.stderr", stderr_buffer),
            contextlib.suppress(_StopAfterFirstIterationError),
        ):
            # Тело сайдкара исполняется как код теста (без Docker и сети) —
            # тем же способом аудит Э5 воспроизвёл все три отказа. exec() —
            # не бизнес-логика продукта, а инструмент теста над извлечённым
            # текстом скрипта из docker-compose.yml.
            exec(compile(script, "<backup-script>", "exec"), {"__name__": "__backup_script_test__"})  # noqa: S102

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


def test_yaml_разбирается_без_ошибок():
    """Инвариант №10: значения с двоеточием не ломают разбор YAML."""
    data = _load()
    assert isinstance(data, dict)
    assert "services" in data


def test_состав_сервисов_ровно_три():
    """В связке ровно три сервиса: scout, caddy, backup."""
    data = _load()
    assert set(data["services"].keys()) == {"scout", "caddy", "backup"}


def test_scout_имеет_env_file_и_не_публикует_порт():
    """У scout обязателен env_file и нет прямой публикации порта наружу."""
    scout = _load()["services"]["scout"]
    assert scout["env_file"] == ".env"
    assert "ports" not in scout
    assert scout["expose"] == ["8000"]


def test_scout_тома_смонтированы_по_путям_из_контракта():
    """Пути внутри контейнера scout закреплены контрактом плана."""
    scout = _load()["services"]["scout"]
    assert "scout-data:/srv/scout/data" in scout["volumes"]
    assert "scout-out:/srv/scout/out" in scout["volumes"]


def test_caddy_ждёт_scout_живым():
    """caddy стартует только после того, как healthcheck scout стал зелёным."""
    caddy = _load()["services"]["caddy"]
    depends_on = caddy["depends_on"]
    assert depends_on["scout"]["condition"] == "service_healthy"


def test_caddy_образ_закреплён_версией():
    """M8 аудита: образ прокси должен быть закреплён конкретной версией.

    Замена ``caddy:2-alpine`` на произвольный образ (например ``nginx``)
    ничем в связке не поймана — Caddyfile рассчитан именно на Caddy.
    """
    caddy = _load()["services"]["caddy"]
    assert caddy["image"] == "caddy:2-alpine"


def test_backup_том_базы_только_на_чтение():
    """Сайдкар не может повредить рабочую базу — том примонтирован :ro."""
    backup = _load()["services"]["backup"]
    data_mounts = [v for v in backup["volumes"] if v.startswith("scout-data:")]
    assert data_mounts, "том базы не найден у сервиса backup"
    assert data_mounts[0].endswith(":ro")


def test_backup_том_копий_отдельный_от_тома_базы():
    """Копия на том же томе, что и база, не переживёт повреждение тома."""
    backup = _load()["services"]["backup"]
    backup_mounts = [v for v in backup["volumes"] if not v.startswith("scout-data:")]
    assert backup_mounts, "том копий не найден у сервиса backup"
    for mount in backup_mounts:
        volume_name = mount.split(":", 1)[0]
        assert volume_name != "scout-data"


def test_backup_не_ставит_пакеты_и_не_alpine():
    """РЕШ-229: сайдкар не собирается из alpine с apk add — сеть на старте под запретом."""
    backup = _load()["services"]["backup"]
    assert "alpine" not in backup.get("image", "")
    text = _text()
    assert "apk add" not in text
    assert "apk" not in backup.get("image", "")


def test_backup_использует_образ_приложения_без_второй_сборки():
    """backup ссылается на тот же тег образа, что и build сервиса scout."""
    services = _load()["services"]
    scout = services["scout"]
    backup = services["backup"]
    assert "build" not in backup, "сборка сайдкара отдельно от приложения не нужна"
    assert backup["image"] == scout["image"]


def test_backup_ротация_копий_14_дней():
    """Ротация — удаление копий старше 14 суток, не 140 и не 1400.

    Сравнение точным значением int(), а не подстрокой: "14" — подстрока
    "140", и текстовый поиск не отличил бы предел в 10 раз слабее заявленного.
    """
    script = _backup_script()
    assert _script_constant(script, "RETENTION_DAYS") == 14


def test_backup_период_суточный():
    """Цикл копирования — раз в сутки, ровно 86400 секунд, не 186400.

    Та же ловушка, что и с ротацией: "86400" — подстрока "186400", поэтому
    значение сравнивается как целое число, а не через "in".
    """
    script = _backup_script()
    assert _script_constant(script, "DAY") == 86400


def test_backup_цикл_бесконечный():
    """Копия выполняется постоянно, а не один раз при старте контейнера."""
    script = _backup_script()
    assert "while True" in script


def test_backup_отказ_копии_печатается_в_stderr():
    """Молча пропущенная копия базы хуже отсутствующей (принцип инварианта №5).

    Проверяется именно ветка try/except вокруг sqlite3-копии — независимо от
    того, печатает ли что-то соседняя ветка ротации. Если бы печать отказа
    осталась только в ротации, эта проверка должна упасть, а прежняя
    (поиск подстроки "file=sys.stderr" где угодно в тексте) — нет.
    """
    tree = ast.parse(_backup_script())
    backup_try = _try_node_containing(tree, "src.backup(dst")
    assert _handlers_print_to_stderr(backup_try), "отказ копии базы не печатается в stderr"


def test_backup_отказ_ротации_печатается_в_stderr():
    """Молча пропущенная ротация — тот же принцип, отдельная от копии проверка.

    Изолирует ветку try/except вокруг удаления устаревших файлов: пропажа
    печати именно здесь не должна маскироваться печатью в ветке копии.
    """
    tree = ast.parse(_backup_script())
    rotation_try = _try_node_containing(tree, "os.remove(path)")
    assert _handlers_print_to_stderr(rotation_try), "отказ ротации копий не печатается в stderr"


def test_backup_работает_от_root_для_записи_в_чужой_том():
    """Именованный том создаётся от root; uid 10001 приложения писать в него не может."""
    backup = _load()["services"]["backup"]
    assert str(backup["user"]) == "0:0"


def test_все_сервисы_перезапускаются_сами():
    """restart: unless-stopped обязателен для всех трёх сервисов связки."""
    services = _load()["services"]
    for name, service in services.items():
        assert service["restart"] == "unless-stopped", name


def test_объявлены_все_пять_томов():
    """volumes содержит все именованные тома, используемые сервисами."""
    volumes = _load()["volumes"]
    assert set(volumes.keys()) == {
        "scout-data",
        "scout-out",
        "caddy-data",
        "caddy-config",
        "scout-backups",
    }


def test_секретов_нет_в_файле():
    """Инвариант №3: в тексте файла нет присваиваний ключей/паролей/токенов."""
    text = _text()
    assert not re.search(r"\b[\w-]*(API_KEY|PASSWORD|TOKEN)\s*=\s*\S+", text)


def test_env_file_не_ослаблен():
    """Ослабление env_file до required: false запрещено контрактом задачи."""
    text = _text()
    assert "required: false" not in text


def test_имя_проекта_зафиксировано():
    """Совет 5 аудита: без name compose берёт имя из каталога развёртывания,
    контейнеры выходят docker-scout-1 вместо предсказуемых имён.
    """
    data = _load()
    assert data.get("name") == "techmost-scout"


def test_backup_ждёт_живости_scout():
    """Правка 1 аудита: без ожидания сайдкар стартует раньше базы, первая
    копия проваливается на "unable to open database file", и цикл уходит в
    сутки ожидания следующей попытки.

    condition=service_healthy — не любое ожидание: healthcheck scout зелёный
    только после ``init_db()`` в lifespan (см. backend/app/main.py), то есть
    к этому моменту файл базы уже существует.
    """
    backup = _load()["services"]["backup"]
    depends_on = backup.get("depends_on")
    assert depends_on is not None, "у backup нет depends_on — правка 1 аудита не применена"
    assert depends_on["scout"]["condition"] == "service_healthy"


def test_backup_не_тянет_образ_из_реестра():
    """Совет 4 аудита: у backup нет своего build, и без pull_policy: never
    compose штатно попытается сначала стянуть тег из реестра — тега там нет.
    """
    backup = _load()["services"]["backup"]
    assert backup.get("pull_policy") == "never"


def test_backup_журнал_ограничен_как_у_остальных_сервисов():
    """M14 аудита / правка 4: у scout и caddy logging с ротацией есть, у
    backup — умолчание демона без ротации, хотя docs/deploy.md обещает
    ротацию для всех трёх сервисов.
    """
    backup = _load()["services"]["backup"]
    logging_cfg = backup.get("logging")
    assert logging_cfg is not None, "у backup нет секции logging"
    assert logging_cfg["driver"] == "json-file"
    assert logging_cfg["options"]["max-size"] == "10m"
    assert logging_cfg["options"]["max-file"] == "3"


def test_backup_скрипт_читает_боевую_базу_только_на_чтение():
    """M12 аудита: mode=ro в URI-строке подключения к источнику — второй,
    независимый от точки монтирования тома, рубеж защиты рабочей базы.
    Подмена на mode=rw ничем, кроме этого теста, не поймана.
    """
    script = _backup_script()
    tree = ast.parse(script)
    connect_calls = [
        call
        for call in _find_calls(tree, "connect")
        if call.args and isinstance(call.args[0], ast.Constant) and "scout.db" in str(call.args[0].value)
    ]
    assert len(connect_calls) == 1, "ожидалось ровно одно подключение к файлу боевой базы"
    uri = connect_calls[0].args[0].value
    assert uri == "file:/data/scout.db?mode=ro"


def test_backup_путь_к_боевой_базе_соответствует_точке_монтирования():
    """M13 аудита: путь /data/scout.db жёстко связан с volumes сервиса —
    подмена на соседний файл (data/other.db) копирует не ту базу, а
    настоящая остаётся вовсе без копий.
    """
    backup = _load()["services"]["backup"]
    data_mount = next(v for v in backup["volumes"] if v.startswith("scout-data:"))
    mount_path = data_mount.split(":")[1]
    script = _backup_script()
    assert f"{mount_path}/scout.db" in script


def test_backup_соединения_закрываются_в_finally():
    """Правка 2 аудита: на исключении соединения оставались открытыми —
    закрытие обязано быть в finally, а не только в успешной ветке try.
    """
    tree = ast.parse(_backup_script())
    backup_try = _try_node_containing(tree, "src.backup(dst")
    assert backup_try.finalbody, "у try-блока копии нет секции finally"
    finally_source = "\n".join(ast.unparse(stmt) for stmt in backup_try.finalbody)
    assert finally_source.count(".close()") >= 2, "в finally закрыто меньше двух соединений"
    assert "src" in finally_source and "dst" in finally_source


def test_backup_предел_ожидания_блокировки_в_разумных_границах():
    """Правка 3 аудита: без предела copy() ждёт снятия блокировки бесконечно
    (в прогоне аудита — больше ста секунд до принудительного обрыва).

    Сравнение диапазоном, а не подстрокой или точным числом: ловушка "14"
    внутри "140" уже дважды ловила тесты на этом этапе — здесь важно, что
    предел вообще есть и вменяем (не секунда, не сутки), а не конкретная
    цифра, которую можно будет один раз обсудить и поменять.
    """
    timeout = _script_constant(_backup_script(), "LOCK_WAIT_TIMEOUT_SECONDS")
    assert 5 <= timeout <= 120, f"предел ожидания {timeout} с вне разумных границ"


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
