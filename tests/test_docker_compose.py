"""Проверки связки docker-compose.yml: тома, ожидание живости, копия базы.

Тесты читают файл как текст и разбирают через ``yaml.safe_load`` (инвариант
№10). Демона Docker и сети нет (инвариант №7) — только статический анализ.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

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
    backup_try = _try_node_containing(tree, "src.backup(dst)")
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
