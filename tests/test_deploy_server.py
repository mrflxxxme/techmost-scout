"""Серверная часть деплоя `scripts/deploy_server.py` (Э6-9, РЕШ-272, РЕШ-287).

Скрипт исполняется на сервере системным `python3`, поэтому тесты держат три
вещи: синтаксис Python 3.8 и только стандартную библиотеку; значения секретов
не выходят ни в stdout, ни в stderr (инв. 3); файлы правятся на месте — тот же
inode, потому что `/opt/edge-caddy/Caddyfile` смонтирован в контейнер по inode.
Сети и docker в тестах нет (инв. 7): хэш пароля подменён, база SQLite — во
временном каталоге.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import stat
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "deploy_server.py"
TEMPLATE = REPO_ROOT / "docker" / "edge" / "scout-sites.caddy"
CADDYFILE = REPO_ROOT / "docker" / "Caddyfile"
DEPLOY_MD = REPO_ROOT / "docs" / "deploy.md"

#: Не настоящий хэш: форма bcrypt, собранная по частям, чтобы в репозитории
#: не лежала ни одна строка, похожая на боевую (инв. 3).
FAKE_HASH = "$2" + "a$14$" + "Z" * 53
#: Заглушки значений: их не должно быть в выводе ни одной команды.
SECRET_A = "stub-deepseek-value-7f3a91"
SECRET_B = "stub-yandex-value-c24d08"
PASSWORD = "stub-web-password-5be1d7"
WORK_HOST = "scout.test.example"
SCAN_HOST = "scan.test.example"


@pytest.fixture(scope="module")
def ds() -> ModuleType:
    """Скрипт как модуль: он лежит вне пакета, импортируется по пути."""
    spec = importlib.util.spec_from_file_location("deploy_server_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_hash(ds, monkeypatch):
    """Хэш пароля подменён: docker и edge-caddy в тестах не вызываются."""
    seen: list[str] = []

    def fake(password: str) -> str:
        seen.append(password)
        return FAKE_HASH

    monkeypatch.setattr(ds, "hash_password", fake)
    return seen


# ── Python 3.8 и стандартная библиотека ─────────────────────────────────────

#: Имена из стандартной библиотеки Python 3.9+: на хосте с 3.8 их нет.
_NEWER_THAN_38 = ("removeprefix", "removesuffix", "zoneinfo", "functools.cache", "math.lcm", "graphlib")


def test_синтаксис_python_3_8() -> None:
    """Хост деплоя — системный python3; разбор по грамматике 3.8 обязан пройти."""
    ast.parse(SCRIPT.read_text(encoding="utf-8"), feature_version=(3, 8))


def test_нет_конструкций_новее_3_8() -> None:
    """Грамматика 3.8 не ловит методы и модули новее: `removeprefix` падает только на хосте."""
    code = "\n".join(
        line for line in SCRIPT.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
    )
    for needle in _NEWER_THAN_38:
        assert needle not in code, f"в скрипте {needle!r}: на python 3.8 это не работает"
    assert "from __future__ import annotations" in code


def test_f_строки_без_вложенных_одинаковых_кавычек() -> None:
    """Парсер 3.12 принимает `f"{", ".join(x)}"`, а 3.8 нет: ловим по тексту узла."""
    source = SCRIPT.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.JoinedStr):
            continue
        whole = ast.get_source_segment(source, node) or ""
        quote = whole.lstrip("fF")[:1]
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                inner = ast.get_source_segment(source, part.value) or ""
                assert quote not in inner, f"вложенная кавычка в f-строке: {whole}"


def test_только_стандартная_библиотека_и_сети_нет() -> None:
    """Импорты — стандартная библиотека; сетевых модулей нет (инв. 6, 7)."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    stdlib = set(sys.stdlib_module_names)
    assert imported <= stdlib, f"не из стандартной библиотеки: {sorted(imported - stdlib)}"
    network = {"socket", "ssl", "http", "httplib", "urllib3", "requests", "httpx", "ftplib", "smtplib"}
    assert not imported & network, f"сетевые модули в серверном скрипте: {sorted(imported & network)}"
    assert "urllib.request" not in SCRIPT.read_text(encoding="utf-8")


def test_файл_в_пределе_строк() -> None:
    """Правило 500 строк (CLAUDE.md) для scripts/."""
    assert len(SCRIPT.read_text(encoding="utf-8").splitlines()) <= 500


# ── env-upsert ──────────────────────────────────────────────────────────────

ENV_BEFORE = (
    "# Секреты сервера\n"
    "APP_ENV=prod\n"
    "WEB_AUTH_USER=scout\n"
    "WEB_AUTH_PASSWORD=" + PASSWORD + "\n"
    "# DEEPSEEK_API_KEY=закомментированная-строка\n"
    "DEEPSEEK_API_KEY=old-deepseek\n"
    "\n"
    "SCOUT_API_KEYS=bot:abc\n"
)


def test_upsert_дописывает_новые_заменяет_прежние_и_не_трогает_чужое(ds) -> None:
    """Новое имя уходит в конец, прежнее заменяется, комментарии и чужие строки целы."""
    pairs = [("DEEPSEEK_API_KEY", SECRET_A), ("YANDEX_SEARCH_API_KEY", SECRET_B)]
    text, report = ds.upsert_env(ENV_BEFORE, pairs)
    assert report["replaced"] == ["DEEPSEEK_API_KEY"]
    assert report["added"] == ["YANDEX_SEARCH_API_KEY"]
    lines = text.splitlines()
    assert f"DEEPSEEK_API_KEY={SECRET_A}" in lines
    assert "DEEPSEEK_API_KEY=old-deepseek" not in lines
    assert "# DEEPSEEK_API_KEY=закомментированная-строка" in lines
    assert lines[-1] == f"YANDEX_SEARCH_API_KEY={SECRET_B}"
    for foreign in ("APP_ENV=prod", "WEB_AUTH_USER=scout", f"WEB_AUTH_PASSWORD={PASSWORD}", "SCOUT_API_KEYS=bot:abc"):
        assert foreign in lines
    # Порядок чужих строк не сдвинулся.
    before = ENV_BEFORE.splitlines()
    kept = [line for line in lines if line in before and not line.startswith("DEEPSEEK_API_KEY=")]
    assert kept == [line for line in before if line in kept]


def test_upsert_пустое_значение_ключ_не_стирает(ds) -> None:
    """Секрет репозитория не задан — прежнее значение на сервере остаётся."""
    text, report = ds.upsert_env(ENV_BEFORE, [("DEEPSEEK_API_KEY", "")])
    assert text == ENV_BEFORE
    assert report["skipped_empty"] == ["DEEPSEEK_API_KEY"]


def test_upsert_имя_вне_списка_отвергнуто(ds) -> None:
    """Пароль входа и ключи приложений через workflow не пишутся."""
    for name in ("WEB_AUTH_PASSWORD", "SCOUT_API_KEYS", "APP_ENV", "PATH"):
        with pytest.raises(ds.DeployError, match=name):
            ds.upsert_env(ENV_BEFORE, [(name, "x")])


@pytest.mark.parametrize("value", ["с пробелом", 'кавычка"', "решётка#хвост", "доллар$X", "косая\\"])
def test_upsert_значение_которое_compose_истолкует_иначе_отвергнуто(ds, value) -> None:
    """Такое значение в `.env` изменилось бы при чтении compose: лучше отказ."""
    with pytest.raises(ds.DeployError, match="DEEPSEEK_API_KEY"):
        ds.upsert_env(ENV_BEFORE, [("DEEPSEEK_API_KEY", value)])


def test_upsert_без_завершающего_перевода_строки(ds) -> None:
    """Файл без `\\n` в конце: новая строка не склеивается с последней."""
    text, _ = ds.upsert_env("APP_ENV=prod", [("YANDEX_CATALOG_ID", "b1g")])
    assert text == "APP_ENV=prod\nYANDEX_CATALOG_ID=b1g\n"


def test_upsert_повторный_прогон_ничего_не_меняет(ds) -> None:
    """То же значение — «без изменений», текст прежний."""
    once, _ = ds.upsert_env(ENV_BEFORE, [("DEEPSEEK_API_KEY", SECRET_A)])
    twice, report = ds.upsert_env(once, [("DEEPSEEK_API_KEY", SECRET_A)])
    assert twice == once
    assert report["same"] == ["DEEPSEEK_API_KEY"]


def test_env_upsert_вывод_без_значений_копия_600_inode_прежний(ds, tmp_path, capsys) -> None:
    """Команда печатает имена и путь копии; значения — ни в stdout, ни в stderr."""
    env = tmp_path / ".env"
    env.write_text(ENV_BEFORE, encoding="utf-8")
    env.chmod(0o640)
    inode = env.stat().st_ino
    stdin = f"DEEPSEEK_API_KEY={SECRET_A}\nYANDEX_SEARCH_API_KEY={SECRET_B}\nYANDEX_CATALOG_ID=\n"
    assert ds.main(["env-upsert", str(env)], stdin_text=stdin) == 0
    captured = capsys.readouterr()
    shown = captured.out + captured.err
    assert SECRET_A not in shown and SECRET_B not in shown, "значение ключа попало в вывод"
    assert PASSWORD not in shown and "old-deepseek" not in shown
    assert "DEEPSEEK_API_KEY" in shown and "YANDEX_SEARCH_API_KEY" in shown
    assert env.stat().st_ino == inode, "файл подменён, а не переписан на месте"
    assert stat.S_IMODE(env.stat().st_mode) == 0o640, "права существующего файла изменены"
    backups = sorted(tmp_path.glob(".env.bak-*"))
    assert len(backups) == 1, "перед записью обязана быть одна копия"
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert backups[0].read_text(encoding="utf-8") == ENV_BEFORE, "копия снята не с прежнего содержимого"


def test_env_upsert_без_изменений_не_пишет_и_копии_нет(ds, tmp_path, capsys) -> None:
    """Нечего менять — файл не тронут, лишних копий нет."""
    env = tmp_path / ".env"
    env.write_text(ENV_BEFORE, encoding="utf-8")
    assert ds.main(["env-upsert", str(env)], stdin_text="DEEPSEEK_API_KEY=\n") == 0
    capsys.readouterr()
    assert env.read_text(encoding="utf-8") == ENV_BEFORE
    assert not list(tmp_path.glob(".env.bak-*"))


def test_env_upsert_нет_файла_создаётся_с_правами_600(ds, tmp_path, capsys) -> None:
    """Первый прогон на чистом сервере: файл рождается закрытым."""
    env = tmp_path / ".env"
    assert ds.main(["env-upsert", str(env)], stdin_text=f"DEEPSEEK_API_KEY={SECRET_A}\n") == 0
    capsys.readouterr()
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert env.read_text(encoding="utf-8") == f"DEEPSEEK_API_KEY={SECRET_A}\n"


def test_env_upsert_ошибка_не_печатает_значение(ds, tmp_path, capsys) -> None:
    """Имя вне списка: в stderr имя; строка без `=` — только номер, она может быть самим секретом."""
    env = tmp_path / ".env"
    env.write_text(ENV_BEFORE, encoding="utf-8")
    assert ds.main(["env-upsert", str(env)], stdin_text=f"WEB_AUTH_PASSWORD={SECRET_A}\n") == 2
    err = capsys.readouterr().err
    assert "WEB_AUTH_PASSWORD" in err and SECRET_A not in err
    assert ds.main(["env-upsert", str(env)], stdin_text=f"{SECRET_B}\n") == 2
    err = capsys.readouterr().err
    assert SECRET_B not in err and "строка 1" in err
    assert env.read_text(encoding="utf-8") == ENV_BEFORE, "после отказа файл обязан остаться прежним"


# ── Caddyfile ───────────────────────────────────────────────────────────────

FOREIGN_A = 'site-a.example {\n    reverse_proxy site-a:80\n    header {\n        X-Note "}{ в кавычках"\n    }\n}\n'
FOREIGN_B = '# соседний сайт\nsite-b.example, www.site-b.example {\n    respond "ok" 200\n}\n'
GLOBAL = "{\n    email admin@example.ru\n}\n"
OLD_WORK = f"{WORK_HOST} {{\n    reverse_proxy techmost-scout:8000\n}}\n"


def _render(ds, scan: str = SCAN_HOST) -> str:
    return ds.render_sites(TEMPLATE.read_text(encoding="utf-8"), WORK_HOST, scan, "scout", FAKE_HASH)


def _block(text: str, host: str) -> str:
    """Тело блока сайта `host { … }` из текста (до закрывающей скобки в начале строки)."""
    match = re.search(rf"^{re.escape(host)} \{{\n.*?^\}}\n", text, re.MULTILINE | re.DOTALL)
    assert match is not None, f"блока {host} нет"
    return match.group(0)


def test_шаблон_рабочий_хост_с_паролем_и_api_мимо_пароля(ds) -> None:
    """Рабочий блок: `@ui not path /api/v1/*`, `basic_auth @ui`, прокси на techmost-scout."""
    block = _block(_render(ds), WORK_HOST)
    assert "@ui not path /api/v1/*" in block
    assert "basic_auth @ui {" in block
    assert f"scout {FAKE_HASH}" in block
    assert "reverse_proxy techmost-scout:8000" in block


def test_шаблон_блок_скана_без_basic_auth(ds) -> None:
    """Публичный хост скана пароля не имеет: посетитель его не знает."""
    block = _block(_render(ds), SCAN_HOST)
    assert "basic_auth" not in block and "basicauth" not in block
    assert "reverse_proxy techmost-scout:8000" in block


def test_шаблон_без_хоста_скана_блока_скана_нет(ds) -> None:
    """Пустой SCOUT_SCAN_HOST: в файле один блок, пометок и следов скана нет."""
    text = _render(ds, scan="")
    assert SCAN_HOST not in text and "[[" not in text and "%%" not in text
    assert text.count("reverse_proxy") == 1
    assert "basic_auth" in text


def test_шаблон_заголовки_как_в_docker_caddyfile(ds) -> None:
    """Пять заголовков безопасности в обоих блоках — те же, что в docker/Caddyfile."""
    names = "Strict-Transport-Security|X-Content-Type-Options|X-Frame-Options|Referrer-Policy|X-Robots-Tag"
    headers = re.findall(
        rf'^\s+((?:{names}) ".*")$',
        CADDYFILE.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert len(headers) == 5
    text = _render(ds)
    for host in (WORK_HOST, SCAN_HOST):
        block = _block(text, host)
        for header in headers:
            assert header in block, f"в блоке {host} нет заголовка {header}"


def test_блок_скана_совпадает_с_разделом_публичный_скан() -> None:
    """Блок шаблона и блок раздела «Публичный скан» docs/deploy.md различаются только именем хоста."""
    section = DEPLOY_MD.read_text(encoding="utf-8").split("## 12. Публичный скан", 1)[1]
    doc_block = re.search(r"```caddyfile\n(.*?)```", section, re.DOTALL)
    assert doc_block is not None, "в разделе скана нет блока caddyfile"
    template = TEMPLATE.read_text(encoding="utf-8")
    tpl_block = re.search(r"^%%SCAN_HOST%% \{\n.*?^\}\n", template, re.MULTILINE | re.DOTALL)
    assert tpl_block is not None
    norm_doc = doc_block.group(1).split("{", 1)[1].strip()
    norm_tpl = tpl_block.group(0).split("{", 1)[1].strip()
    assert norm_doc == norm_tpl


def test_шаблон_подстановки_только_известные() -> None:
    """В шаблоне нет подстановки, которую скрипт не заполнит."""
    names = set(re.findall(r"%%([A-Z_]+)%%", TEMPLATE.read_text(encoding="utf-8")))
    assert names == {"WORK_HOST", "SCAN_HOST", "AUTH_USER", "AUTH_HASH"}


def test_шаблон_отвергает_недопустимые_значения(ds) -> None:
    """Хост с пробелом или скобкой изменил бы разбор Caddyfile; хэш не bcrypt — отказ."""
    tpl = TEMPLATE.read_text(encoding="utf-8")
    for bad_host in ("a b", "x{y}", "evil.example {", "-a.example", ""):
        with pytest.raises(ds.DeployError):
            ds.render_sites(tpl, bad_host, "", "scout", FAKE_HASH)
    with pytest.raises(ds.DeployError):
        ds.render_sites(tpl, WORK_HOST, "bad host", "scout", FAKE_HASH)
    with pytest.raises(ds.DeployError):
        ds.render_sites(tpl, WORK_HOST, WORK_HOST, "scout", FAKE_HASH)
    with pytest.raises(ds.DeployError):
        ds.render_sites(tpl, WORK_HOST, "", "sc out", FAKE_HASH)
    with pytest.raises(ds.DeployError):
        ds.render_sites(tpl, WORK_HOST, "", "scout", "не-хэш")


def test_merge_дописывает_в_конец_и_чужие_блоки_байт_в_байт(ds) -> None:
    """Нет ни маркеров, ни прежнего блока: область в конец, всё прежнее — префиксом файла."""
    original = GLOBAL + "\n" + FOREIGN_A + "\n" + FOREIGN_B
    merged = ds.merge_sites(original, _render(ds), [WORK_HOST, SCAN_HOST])
    assert merged.startswith(original)
    assert merged.count(ds.MARK_BEGIN) == 1 and merged.count(ds.MARK_END) == 1


def test_merge_заменяет_область_между_маркерами(ds) -> None:
    """Вторая раскатка с другим хэшем: меняется только область, чужое — байт в байт."""
    first = ds.merge_sites(GLOBAL + "\n" + FOREIGN_A + "\n", _render(ds), [WORK_HOST, SCAN_HOST])
    after = first + "\n" + FOREIGN_B
    new_hash = "$2" + "a$14$" + "Q" * 53
    region = ds.render_sites(TEMPLATE.read_text(encoding="utf-8"), WORK_HOST, SCAN_HOST, "scout", new_hash)
    second = ds.merge_sites(after, region, [WORK_HOST, SCAN_HOST])
    assert new_hash in second and FAKE_HASH not in second
    for foreign in (GLOBAL, FOREIGN_A, FOREIGN_B):
        assert foreign in second
    assert second.count(ds.MARK_BEGIN) == 1


def test_merge_принимает_немаркированный_блок_рабочего_хоста(ds) -> None:
    """Первый прогон: прежний ручной блок рабочего хоста становится областью Скаута, дубля нет."""
    original = GLOBAL + "\n" + FOREIGN_A + "\n" + OLD_WORK + "\n" + FOREIGN_B
    merged = ds.merge_sites(original, _render(ds), [WORK_HOST, SCAN_HOST])
    assert merged.count(f"{WORK_HOST} {{") == 1, "рабочий хост описан дважды"
    assert OLD_WORK not in merged
    assert ds.MARK_BEGIN in merged
    for foreign in (GLOBAL, FOREIGN_A, FOREIGN_B):
        assert foreign in merged, "чужой блок изменён"
    # Порядок блоков: область стоит там, где стоял принятый блок — между чужими.
    assert merged.index(FOREIGN_A) < merged.index(ds.MARK_BEGIN) < merged.index(FOREIGN_B)


def test_merge_повторный_прогон_ничего_не_меняет(ds) -> None:
    """Тот же вход — тот же текст."""
    original = GLOBAL + "\n" + FOREIGN_A + "\n" + OLD_WORK + "\n" + FOREIGN_B
    once = ds.merge_sites(original, _render(ds), [WORK_HOST, SCAN_HOST])
    assert ds.merge_sites(once, _render(ds), [WORK_HOST, SCAN_HOST]) == once


def test_merge_убирает_блок_скана_когда_хост_очищен(ds) -> None:
    """SCOUT_SCAN_HOST очищен: область переписана без секции скана, чужое цело."""
    with_scan = ds.merge_sites(FOREIGN_A + "\n", _render(ds), [WORK_HOST, SCAN_HOST])
    without = ds.merge_sites(with_scan, _render(ds, scan=""), [WORK_HOST, ""])
    assert SCAN_HOST not in without
    assert FOREIGN_A in without and WORK_HOST in without


def test_merge_блок_где_наш_хост_с_чужим_адресом_отказ(ds) -> None:
    """Общий блок «наш, чужой» автоматически не делится."""
    mixed = f"{WORK_HOST}, other.example {{\n    respond 200\n}}\n"
    with pytest.raises(ds.DeployError, match="чужим"):
        ds.merge_sites(mixed, _render(ds), [WORK_HOST, SCAN_HOST])


@pytest.mark.parametrize(
    "broken",
    [
        "# >>> techmost-scout\nx {\n}\n",
        "# <<< techmost-scout\n",
        "# <<< techmost-scout\n# >>> techmost-scout\n",
        "# >>> techmost-scout\n# <<< techmost-scout\n# >>> techmost-scout\n# <<< techmost-scout\n",
    ],
)
def test_merge_битые_маркеры_отказ(ds, broken) -> None:
    """Непарные, перевёрнутые и двойные маркеры — стоп, а не догадка."""
    with pytest.raises(ds.DeployError, match="маркер"):
        ds.merge_sites(broken, _render(ds), [WORK_HOST, SCAN_HOST])


def test_merge_пустой_файл_и_файл_без_перевода_строки(ds) -> None:
    """Пустой Caddyfile — область целиком; файл без `\\n` на конце не склеивается с областью."""
    region = _render(ds)
    assert ds.merge_sites("", region, [WORK_HOST]) == region
    merged = ds.merge_sites("a.example {\n}", region, [WORK_HOST])
    assert merged.startswith("a.example {\n}\n\n" + ds.MARK_BEGIN)


def test_merge_скобки_в_кавычках_и_комментариях_не_ломают_разбор(ds) -> None:
    """Блок с `}{` в строке и скобкой в комментарии разбирается как один блок."""
    tricky = f"{FOREIGN_A}\n# закрывающая }} в комментарии\n{OLD_WORK}"
    merged = ds.merge_sites(tricky, _render(ds), [WORK_HOST])
    assert FOREIGN_A in merged and merged.count(f"{WORK_HOST} {{") == 1


def _write_env(tmp_path: Path, user: str = "scout", password: str = PASSWORD) -> Path:
    env = tmp_path / ".env"
    env.write_text(f"WEB_AUTH_USER={user}\nWEB_AUTH_PASSWORD={password}\n", encoding="utf-8")
    return env


def test_caddy_block_пишет_на_месте_inode_прежний(ds, fake_hash, tmp_path, capsys) -> None:
    """Запись файла на месте: тот же inode, иначе bind-монт edge-caddy ослепнет."""
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(GLOBAL + "\n" + FOREIGN_A + "\n" + OLD_WORK, encoding="utf-8")
    inode = caddyfile.stat().st_ino
    argv = [
        "caddy-block",
        str(caddyfile),
        str(TEMPLATE),
        "--env-file",
        str(_write_env(tmp_path)),
        "--work-host",
        WORK_HOST,
        "--scan-host",
        SCAN_HOST,
    ]
    assert ds.main(argv) == 0
    out = capsys.readouterr()
    assert caddyfile.stat().st_ino == inode, "Caddyfile подменён новым файлом, а не переписан на месте"
    assert fake_hash == [PASSWORD], "хэш считается от пароля из .env"
    shown = out.out + out.err
    assert PASSWORD not in shown and FAKE_HASH not in shown, "пароль или хэш в выводе"
    text = caddyfile.read_text(encoding="utf-8")
    assert FOREIGN_A in text and f"scout {FAKE_HASH}" in text
    # Повторный прогон: содержимое и inode те же.
    before = caddyfile.read_bytes()
    mtime = caddyfile.stat().st_mtime_ns
    assert ds.main(argv) == 0
    capsys.readouterr()
    assert caddyfile.read_bytes() == before and caddyfile.stat().st_ino == inode
    assert caddyfile.stat().st_mtime_ns == mtime, "без изменений файл не должен переписываться"


def test_caddy_block_логин_из_env_и_умолчание(ds, fake_hash, tmp_path, capsys) -> None:
    """Логин блока — WEB_AUTH_USER; нет переменной — `scout`, как умолчание приложения."""
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text("", encoding="utf-8")
    argv = ["caddy-block", str(caddyfile), str(TEMPLATE), "--work-host", WORK_HOST]
    assert ds.main([*argv, "--env-file", str(_write_env(tmp_path, user="owner"))]) == 0
    assert f"owner {FAKE_HASH}" in caddyfile.read_text(encoding="utf-8")
    env = tmp_path / "bare.env"
    env.write_text(f"WEB_AUTH_PASSWORD={PASSWORD}\n", encoding="utf-8")
    assert ds.main([*argv, "--env-file", str(env)]) == 0
    assert f"scout {FAKE_HASH}" in caddyfile.read_text(encoding="utf-8")
    capsys.readouterr()


@pytest.mark.parametrize("password", ["", "change-me"])
def test_caddy_block_без_пароля_или_с_умолчанием_отказ(ds, fake_hash, tmp_path, capsys, password) -> None:
    """Хэш от пустого пароля или умолчания «change-me» закрыл бы вход известным словом."""
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(FOREIGN_A, encoding="utf-8")
    argv = ["caddy-block", str(caddyfile), str(TEMPLATE), "--work-host", WORK_HOST]
    rc = ds.main([*argv, "--env-file", str(_write_env(tmp_path, password=password))])
    assert rc == 2 and "WEB_AUTH_PASSWORD" in capsys.readouterr().err
    assert caddyfile.read_text(encoding="utf-8") == FOREIGN_A and not fake_hash


def test_hash_password_пароль_только_через_stdin(ds, monkeypatch) -> None:
    """Пароль уходит в `caddy hash-password` через stdin и не попадает в аргументы процесса."""
    calls: list[dict] = []

    class Done:
        returncode = 0
        stdout = (FAKE_HASH + "\n").encode()

    def fake_run(args, **kwargs):
        calls.append({"args": list(args), **kwargs})
        return Done()

    monkeypatch.setattr(ds.subprocess, "run", fake_run)
    assert ds.hash_password(PASSWORD) == FAKE_HASH
    assert calls[0]["input"] == (PASSWORD + "\n").encode()
    assert PASSWORD not in " ".join(calls[0]["args"]) and "--plaintext" not in calls[0]["args"]
    assert calls[0]["args"][:4] == ["docker", "exec", "-i", "edge-caddy"]


def test_hash_password_мусор_вместо_хэша_отказ(ds, monkeypatch) -> None:
    """Ответ, не похожий на bcrypt, в Caddyfile не попадает."""

    class Done:
        returncode = 0
        stdout = b"oops\n"

    monkeypatch.setattr(ds.subprocess, "run", lambda *a, **k: Done())
    with pytest.raises(ds.DeployError, match="bcrypt"):
        ds.hash_password(PASSWORD)


# ── копии и откат ───────────────────────────────────────────────────────────


def test_file_backup_копия_600_с_тем_же_содержимым(ds, tmp_path, capsys) -> None:
    """Копия рядом с файлом, права 600, путь в stdout одной строкой (его читает workflow)."""
    source = tmp_path / "Caddyfile"
    source.write_text(FOREIGN_A, encoding="utf-8")
    assert ds.main(["file-backup", str(source)]) == 0
    printed = capsys.readouterr().out.strip()
    backup = Path(printed)
    assert backup.parent == tmp_path and re.fullmatch(r"Caddyfile\.bak-\d{8}-\d{6}(-\d+)?", backup.name)
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert backup.read_text(encoding="utf-8") == FOREIGN_A


def test_file_backup_два_за_секунду_не_затирают_друг_друга(ds, tmp_path, monkeypatch) -> None:
    """Одинаковая метка времени: вторая копия получает суффикс, первая цела."""
    source = tmp_path / "f"
    source.write_text("one", encoding="utf-8")
    monkeypatch.setattr(ds, "timestamp", lambda: "20261021-000000")
    first = Path(ds.make_backup(str(source)))
    source.write_text("two", encoding="utf-8")
    second = Path(ds.make_backup(str(source)))
    assert first != second and first.read_text(encoding="utf-8") == "one"


def test_file_restore_откат_на_месте(ds, tmp_path, capsys) -> None:
    """Откат возвращает старое содержимое тем же inode."""
    target = tmp_path / "Caddyfile"
    target.write_text(FOREIGN_A, encoding="utf-8")
    inode = target.stat().st_ino
    backup = Path(ds.make_backup(str(target)))
    target.write_text("совсем другое содержимое, длиннее прежнего" * 20, encoding="utf-8")
    assert ds.main(["file-restore", str(backup), str(target)]) == 0
    capsys.readouterr()
    assert target.read_text(encoding="utf-8") == FOREIGN_A and target.stat().st_ino == inode


def test_file_restore_принимает_только_копию(ds, tmp_path, capsys) -> None:
    """Подсунуть произвольный файл как «копию» нельзя."""
    other = tmp_path / "other"
    other.write_text("x", encoding="utf-8")
    target = tmp_path / "t"
    target.write_text("y", encoding="utf-8")
    assert ds.main(["file-restore", str(other), str(target)]) == 2
    capsys.readouterr()
    assert target.read_text(encoding="utf-8") == "y"


def test_db_backup_копия_читается_и_совпадает(ds, tmp_path, capsys) -> None:
    """Копия SQLite через Connection.backup(): открывается и содержит те же строки."""
    import sqlite3

    source = tmp_path / "scout.db"
    con = sqlite3.connect(source)
    con.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, brief TEXT)")
    con.execute("INSERT INTO requests (brief) VALUES ('тест')")
    con.commit()
    con.close()
    target = tmp_path / "scout-predeploy-20261021-000000.db"
    assert ds.main(["db-backup", str(source), str(target)]) == 0
    capsys.readouterr()
    copy = sqlite3.connect(target)
    try:
        assert copy.execute("SELECT brief FROM requests").fetchall() == [("тест",)]
    finally:
        copy.close()
    assert not Path(str(target) + ".part").exists(), "временный файл копии остался"


def test_db_backup_нет_базы_отказ(ds, tmp_path, capsys) -> None:
    """Нет файла базы: ошибка, а не пустая копия под именем годной."""
    target = tmp_path / "copy.db"
    assert ds.main(["db-backup", str(tmp_path / "нет.db"), str(target)]) == 2
    capsys.readouterr()
    assert not target.exists()


def test_имя_копии_базы_попадает_под_ротацию_сайдкара() -> None:
    """Сайдкар чистит `/backups/scout-*.db`: имя `scout-predeploy-…` под эту маску подходит."""
    import fnmatch

    assert fnmatch.fnmatch("scout-predeploy-20261021-000000.db", "scout-*.db")


def test_разрешённые_ключи_не_включают_пароль_и_ключи_приложений(ds) -> None:
    """Закреплённый список: ключи моделей, поиска и переменные скана — и ничего сверх."""
    allowed = set(ds.ALLOWED_ENV_KEYS)
    assert {"DEEPSEEK_API_KEY", "YANDEX_API_KEY", "YANDEX_CATALOG_ID", "YANDEX_SEARCH_API_KEY"} <= allowed
    assert {"PUBLIC_SCAN_HOST", "YANDEX_SEARCH_RUB_PER_QUERY"} <= allowed
    assert not allowed & {"WEB_AUTH_PASSWORD", "WEB_AUTH_USER", "SCOUT_API_KEYS", "APP_ENV", "ROSPATENT_API_KEY"}
