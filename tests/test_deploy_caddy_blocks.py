"""Заголовки блоков edge-Caddyfile и запасной хэш пароля (Э6-9, цикл 2 ревью, блокирующая 1).

Caddy склеивает адреса, перенесённые после запятой на следующую строку, в один заголовок.
Разбор, который смотрел только строку с `{`, не видел соседа над нашим хостом: блок
считался «только нашим», а чужая строка с запятой повисала над областью Скаута и
склеивалась с ней. Сеть и docker не нужны (инв. 7): хэш подменён.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "deploy_server.py"
TEMPLATE = REPO_ROOT / "docker" / "edge" / "scout-sites.caddy"

#: Не настоящий хэш: форма bcrypt, собранная по частям (инв. 3).
FAKE_HASH = "$2" + "a$14$" + "Z" * 53
MANUAL_HASH = "$2" + "a$14$" + "M" * 53
WORK_HOST = "scout.test.example"
SCAN_HOST = "scan.test.example"
BODY = "    reverse_proxy 127.0.0.1:9000\n}\n"
NEIGHBOR = "neighbor.example"


@pytest.fixture(scope="module")
def ds() -> ModuleType:
    """Скрипт как модуль: он лежит вне пакета, импортируется по пути."""
    spec = importlib.util.spec_from_file_location("deploy_server_blocks_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _region(ds: ModuleType) -> str:
    return ds.render_sites(TEMPLATE.read_text(encoding="utf-8"), WORK_HOST, SCAN_HOST, "scout", FAKE_HASH)


# Заголовки, в которых наш хост соседствует с чужим адресом: все формы записи.
MIXED = {
    "сосед первым, перенос после запятой": f"{NEIGHBOR},\n{WORK_HOST} {{\n{BODY}",
    "наш первым, перенос после запятой": f"{WORK_HOST},\n{NEIGHBOR} {{\n{BODY}",
    "между строками комментарий": f"{NEIGHBOR},\n# комментарий владельца\n{WORK_HOST} {{\n{BODY}",
    "между строками пустая строка и комментарий": f"{WORK_HOST},\n\n  # про соседа\n{NEIGHBOR} {{\n{BODY}",
    "три строки": f"{NEIGHBOR},\n{WORK_HOST},\nother.example {{\n{BODY}",
    "в одну строку": f"{NEIGHBOR}, {WORK_HOST} {{\n{BODY}",
    "однострочный блок": f"{NEIGHBOR}, {WORK_HOST} {{ respond ok }}\n",
}


@pytest.mark.parametrize("original", list(MIXED.values()), ids=list(MIXED))
def test_merge_наш_хост_рядом_с_чужим_адресом_в_любой_записи_отказ(ds, original: str) -> None:
    """Первый прогон (область без маркеров): сосед в заголовке — отказ, а не молчаливое поглощение."""
    with pytest.raises(ds.DeployError, match="чужим"):
        ds.merge_sites(original, _region(ds), [WORK_HOST, SCAN_HOST])


def test_merge_висячая_строка_с_запятой_над_областью_отказ(ds) -> None:
    """Область уже размечена, а над ней осталась строка `сосед,`: Caddy склеил бы её с первым блоком."""
    with_region = ds.merge_sites("", _region(ds), [WORK_HOST, SCAN_HOST])
    for dangling in (f"{NEIGHBOR},\n", f"{NEIGHBOR},\n# про область\n\n"):
        with pytest.raises(ds.DeployError, match="чужим"):
            ds.merge_sites(dangling + with_region, _region(ds), [WORK_HOST, SCAN_HOST])


def test_caddy_block_отказ_не_трогает_файл(ds, monkeypatch, tmp_path) -> None:
    """Отказ из-за соседа в заголовке: файл побайтно прежний, хэш не считался."""
    monkeypatch.setattr(ds, "hash_password", lambda password: FAKE_HASH)
    env = tmp_path / ".env"
    env.write_text("WEB_AUTH_PASSWORD=stub-password-3c91\n", encoding="utf-8")
    for original in MIXED.values():
        caddyfile = tmp_path / "Caddyfile"
        caddyfile.write_text(original, encoding="utf-8")
        inode = caddyfile.stat().st_ino
        argv = ["caddy-block", str(caddyfile), str(TEMPLATE), "--env-file", str(env), "--work-host", WORK_HOST]
        assert ds.main(argv) == 2
        assert caddyfile.read_text(encoding="utf-8") == original and caddyfile.stat().st_ino == inode


def test_top_level_blocks_заголовок_собирается_по_всем_строкам(ds) -> None:
    """Начало блока — первая строка заголовка, адреса — со всех строк."""
    lines = ["a.example {\n", "}\n", f"{NEIGHBOR},\n", "# про\n", f"{WORK_HOST} {{\n", "    respond ok\n", "}\n"]
    assert ds.top_level_blocks(lines) == [(0, 2, ["a.example"]), (2, 7, [NEIGHBOR, WORK_HOST])]
    one = ds.top_level_blocks([f"{WORK_HOST} {{ respond ok }}\n", "b.example {\n", "}\n"])
    assert one == [(0, 1, [WORK_HOST]), (1, 3, ["b.example"])]


def test_top_level_blocks_строка_без_скобки_обрывает_заголовок(ds) -> None:
    """Директива верхнего уровня без запятой на конце не склеивается со следующим блоком."""
    lines = ["import common\n", f"{WORK_HOST} {{\n", "}\n"]
    assert ds.top_level_blocks(lines) == [(1, 3, [WORK_HOST])]


def test_merge_чужие_многострочные_заголовки_остаются_байт_в_байт(ds) -> None:
    """Сосед с адресами в несколько строк, не пересекающийся с нашими, не меняется."""
    foreign = f"{NEIGHBOR},\n# второй адрес\nwww.{NEIGHBOR} {{\n{BODY}"
    original = foreign + "\n" + f"{WORK_HOST} {{\n    respond old\n}}\n"
    merged = ds.merge_sites(original, _region(ds), [WORK_HOST, SCAN_HOST])
    assert merged.startswith(foreign), "чужой блок изменён"
    assert merged.count(f"{WORK_HOST} {{") == 1 and "respond old" not in merged


def test_merge_наш_многострочный_заголовок_без_соседей_принимается_в_маркеры(ds) -> None:
    """Блок только из наших адресов, записанный в две строки или в одну, поглощается без дубля."""
    for ours in (
        f"{WORK_HOST},\n{SCAN_HOST} {{\n{BODY}",
        f"{SCAN_HOST},\n# про скан\n{WORK_HOST} {{\n{BODY}",
        f"{WORK_HOST} {{ respond ok }}\n",
    ):
        merged = ds.merge_sites(ours, _region(ds), [WORK_HOST, SCAN_HOST])
        assert merged.count(f"{WORK_HOST} {{") == 1, "рабочий хост описан дважды"
        assert "respond ok" not in merged and "127.0.0.1:9000" not in merged
        assert ds.merge_sites(merged, _region(ds), [WORK_HOST, SCAN_HOST]) == merged


# ── запасной путь хэша ──────────────────────────────────────────────────────


def _run(ds: ModuleType, tmp_path: Path, env_text: str) -> tuple[int, Path]:
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text("", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text(env_text, encoding="utf-8")
    argv = ["caddy-block", str(caddyfile), str(TEMPLATE), "--env-file", str(env), "--work-host", WORK_HOST]
    return ds.main(argv), caddyfile


def test_ручной_хэш_из_env_берётся_без_вызова_caddy(ds, monkeypatch, tmp_path) -> None:
    """`WEB_AUTH_HASH` в `.env` (в одинарных кавычках для compose) предпочтительнее вычисления; пароль не нужен."""

    def boom(password: str) -> str:
        raise AssertionError("хэш посчитан, хотя задан вручную")

    monkeypatch.setattr(ds, "hash_password", boom)
    code, caddyfile = _run(ds, tmp_path, f"WEB_AUTH_USER=owner\nWEB_AUTH_HASH='{MANUAL_HASH}'\n")
    assert code == 0
    assert f"owner {MANUAL_HASH}" in caddyfile.read_text(encoding="utf-8")


def test_ручной_хэш_не_bcrypt_отказ(ds, monkeypatch, tmp_path, capsys) -> None:
    """Мусор в `WEB_AUTH_HASH` в блок не попадает, значение в ошибке не печатается."""
    monkeypatch.setattr(ds, "hash_password", lambda password: FAKE_HASH)
    code, caddyfile = _run(ds, tmp_path, "WEB_AUTH_HASH=not-a-hash-value-1234\nWEB_AUTH_PASSWORD=stub-password-3c91\n")
    shown = capsys.readouterr()
    assert code == 2 and caddyfile.read_text(encoding="utf-8") == ""
    assert "not-a-hash-value-1234" not in shown.out + shown.err


class _Done:
    def __init__(self, returncode: int, stdout: bytes) -> None:
        self.returncode, self.stdout = returncode, stdout


@pytest.mark.parametrize("failure", ["код", "нет docker", "мусор"])
def test_отказ_хэширования_подсказывает_запасной_путь(ds, monkeypatch, failure: str) -> None:
    """Любой отказ `caddy hash-password` называет WEB_AUTH_HASH и раздел 13, пароль в тексте не светится."""

    def fake_run(args, **kwargs):
        if failure == "нет docker":
            raise FileNotFoundError("docker")
        return _Done(1, b"") if failure == "код" else _Done(0, b"oops\n")

    monkeypatch.setattr(ds.subprocess, "run", fake_run)
    with pytest.raises(ds.DeployError) as caught:
        ds.hash_password("stub-password-3c91")
    text = str(caught.value)
    assert "WEB_AUTH_HASH" in text and "раздел 13" in text and "stub-password-3c91" not in text
