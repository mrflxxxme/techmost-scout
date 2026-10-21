"""Серверная часть деплоя через GitHub Actions (Э6-9, РЕШ-272).

Workflow `.github/workflows/deploy.yml` кладёт этот файл во временный путь на
сервере и вызывает подкомандами. Ограничения, которые держат тесты:

* только стандартная библиотека и синтаксис Python 3.8 — на хосте стоит
  системный `python3`, а не интерпретатор проекта (`ast.parse` с
  `feature_version=(3, 8)` в `tests/test_deploy_server.py`);
* значения секретов приходят только через stdin и наружу не выводятся —
  команды печатают имена ключей и пути, но не значения (инвариант №3);
* файлы правятся **на месте** (тот же inode): `/opt/edge-caddy/Caddyfile`
  смонтирован в контейнер по inode, после подмены файла `reload` рапортует
  успех и ничего не меняет (`docs/deploy.md`, раздел 11).

Подкоманды:

    env-upsert <.env>                       пары ИМЯ=значение из stdin
    caddy-block <Caddyfile> <шаблон> ...    блоки Скаута между маркерами
    file-backup <файл>                      копия <файл>.bak-<время>, права 600
    file-restore <копия> <файл>             откат файла из копии, на месте
    db-backup <база> <копия>                копия SQLite через Connection.backup()
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
import time
from urllib.parse import quote

#: Ключи, которые `env-upsert` вообще вправе трогать. Остальное в `.env`
#: (пароль входа, ключи приложений, режим) владелец правит на сервере сам.
ALLOWED_ENV_KEYS = (
    "DEEPSEEK_API_KEY",
    "YANDEX_API_KEY",
    "YANDEX_CATALOG_ID",
    "YANDEX_SEARCH_API_KEY",
    "PUBLIC_SCAN_HOST",
    "SCAN_LIMIT_PER_IP_PER_DAY",
    "SCAN_DAILY_BUDGET_RUB",
    "SCAN_COST_LIMIT_RUB",
    "YANDEX_SEARCH_RUB_PER_QUERY",
    "SCAN_ORDER_TELEGRAM_URL",
    "SCAN_ORDER_EMAIL",
)

MARK_BEGIN = "# >>> techmost-scout"
MARK_END = "# <<< techmost-scout"
SCAN_BEGIN = "# [[scan]]"
SCAN_END = "# [[/scan]]"

#: Умолчание приложения для пароля входа: хэш от него считать нельзя.
DEFAULT_WEB_PASSWORD = "change-me"  # noqa: S105

#: Хэш считает бинарник самого edge-Caddy: тот же, что проверит блок. Пароль
#: идёт в stdin, а не аргументом, чтобы не попасть в список процессов сервера.
HASH_COMMAND = ["docker", "exec", "-i", "edge-caddy", "caddy", "hash-password"]

HASH_FALLBACK = "хэш вручную по docs/deploy.md (раздел 13) и ключ WEB_AUTH_HASH в docker/.env"

_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")
_USER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_BCRYPT_RE = re.compile(r"^\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}$")
_ENV_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_BAD_VALUE_RE = re.compile(r"[\s\"'#$\\`]")


class DeployError(Exception):
    """Ожидаемый отказ подкоманды: текст идёт в stderr, код выхода 2."""


def timestamp() -> str:
    """Метка времени UTC для имён копий: `20261021-093000`."""
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


# ── .env ────────────────────────────────────────────────────────────────────


def parse_pairs(text: str) -> list[tuple[str, str]]:
    """Разбирает пары `ИМЯ=значение` из stdin; пустые строки пропускает.

    Ошибка называет номер строки, а не её содержимое: строка без `=` может
    оказаться самим секретом.
    """
    pairs: list[tuple[str, str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        name, sep, value = line.partition("=")
        if not sep or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            raise DeployError(f"строка {number}: ожидается ИМЯ=значение")
        pairs.append((name, value.strip()))
    return pairs


def upsert_env(text: str, pairs: list[tuple[str, str]]) -> tuple[str, dict[str, list[str]]]:
    """Возвращает новый текст `.env` и отчёт по именам: что добавлено, заменено.

    Чистая функция. Имя вне `ALLOWED_ENV_KEYS` и значение с символами, которые
    compose или dotenv истолкуют иначе (пробел, кавычки, `#`, `$`), отвергаются.
    Пустое значение ключ не стирает. Чужие строки и комментарии не трогаются.
    """
    report: dict[str, list[str]] = {"added": [], "replaced": [], "same": [], "skipped_empty": []}
    lines = text.splitlines(keepends=True)
    for name, value in pairs:
        if name not in ALLOWED_ENV_KEYS:
            raise DeployError(f"имя {name} не входит в список разрешённых ключей")
        if not value:
            report["skipped_empty"].append(name)
            continue
        if _BAD_VALUE_RE.search(value):
            raise DeployError(
                f"значение {name} содержит пробел, кавычку, #, $ или обратную косую: задайте его на сервере вручную"
            )
        hits = [i for i, line in enumerate(lines) if _env_name(line) == name]
        new_line = f"{name}={value}\n"
        if not hits:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(new_line)
            report["added"].append(name)
            continue
        if all(lines[i].rstrip("\r\n") == new_line.rstrip("\n") for i in hits):
            report["same"].append(name)
            continue
        for i in hits:
            lines[i] = new_line
        report["replaced"].append(name)
    return "".join(lines), report


def _env_name(line: str) -> str:
    """Имя переменной в строке `.env` или пустая строка для комментариев и мусора."""
    match = _ENV_LINE_RE.match(line.rstrip("\r\n"))
    return match.group(1) if match else ""


def env_value(text: str, name: str, default: str = "") -> str:
    """Значение переменной из текста `.env` (последняя строка побеждает), без кавычек по краям."""
    found = default
    for line in text.splitlines():
        match = _ENV_LINE_RE.match(line)
        if match and match.group(1) == name:
            found = match.group(2).strip()
    if len(found) >= 2 and found[0] == found[-1] and found[0] in "\"'":
        found = found[1:-1]
    return found


def write_in_place(path: str, data: bytes, mode: int = 0o600) -> None:
    """Записывает файл на месте: тот же inode, права существующего файла сохраняются."""
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return
    with open(path, "r+b") as handle:
        handle.seek(0)
        handle.write(data)
        handle.truncate()


def make_backup(path: str) -> str:
    """Копия `<path>.bak-<время>` с правами 600; возвращает путь копии."""
    with open(path, "rb") as handle:
        data = handle.read()
    stamp = timestamp()
    for attempt in range(100):
        suffix = "" if attempt == 0 else f"-{attempt}"
        target = f"{path}.bak-{stamp}{suffix}"
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return target
    raise DeployError(f"не удалось подобрать имя копии для {path}")


def run_env_upsert(path: str, stdin_text: str) -> str:
    """Применяет пары из stdin к `.env`; возвращает отчёт — только имена ключей."""
    pairs = parse_pairs(stdin_text)
    old = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8", newline="") as handle:
            old = handle.read()
    new, report = upsert_env(old, pairs)
    out: list[str] = []
    if new != old:
        if os.path.exists(path):
            out.append(f"копия: {make_backup(path)}")
        write_in_place(path, new.encode("utf-8"))
    for key, title in (
        ("added", "добавлено"),
        ("replaced", "заменено"),
        ("same", "без изменений"),
        ("skipped_empty", "пропущено, значение пустое"),
    ):
        if report[key]:
            out.append("{}: {}".format(title, ", ".join(report[key])))
    return "\n".join(out) if out else "пар нет, файл не тронут"


# ── Caddyfile ───────────────────────────────────────────────────────────────


def _strip_noise(line: str) -> str:
    """Строка без комментария и без содержимого кавычек — для счёта скобок."""
    out: list[str] = []
    quote_char = ""
    escaped = False
    for char in line:
        if quote_char:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote_char:
                quote_char = ""
            continue
        if char in "\"'`":
            quote_char = char
        elif char == "#" and (not out or out[-1].isspace()):
            break
        else:
            out.append(char)
    return "".join(out)


#: Конец заголовка блока в однострочной записи `адрес { директива }`.
_HEAD_END_RE = re.compile(r"\s\{(\s|$)")


def top_level_blocks(lines: list[str]) -> list[tuple[int, int, list[str]]]:
    """Верхнеуровневые блоки `адреса { … }`: (первая строка заголовка, строка после последней, адреса).

    Заголовок собирается по всем строкам: строка на запятую продолжается следующей (пустые
    строки и комментарии между ними Caddy пропускает). Однострочный блок тоже распознаётся.
    """
    blocks: list[tuple[int, int, list[str]]] = []
    depth, start, head = 0, -1, ""
    addresses: list[str] | None = None
    for index, raw in enumerate(lines):
        clean = _strip_noise(raw.rstrip("\r\n")).strip()
        if depth == 0 and clean:
            start = start if head else index
            joined = (head + " " + clean).strip()
            one_line = _HEAD_END_RE.search(joined) if clean.endswith("}") else None
            head = joined if clean.endswith(",") else ""
            if clean.endswith("{"):
                addresses = [a for a in re.split(r"[,\s]+", joined[:-1]) if a]
            elif one_line:
                addresses = [a for a in re.split(r"[,\s]+", joined[: one_line.start()]) if a]
        depth += clean.count("{") - clean.count("}")
        if depth <= 0:
            if addresses is not None:
                blocks.append((start, index + 1, addresses))
                addresses = None
            start = start if head else -1
            depth = 0
    return blocks


def _normalize_address(address: str) -> str:
    """Адрес сайта без схемы и порта, в нижнем регистре."""
    value = re.sub(r"^https?://", "", address.lower())
    return re.sub(r":\d+$", "", value)


def validate_params(work_host: str, scan_host: str, user: str, hashed: str) -> None:
    """Подстановки идут в Caddyfile: допускается только то, что не меняет его разбор."""
    if not _HOST_RE.match(work_host):
        raise DeployError("рабочий хост задан неверно")
    if scan_host and not _HOST_RE.match(scan_host):
        raise DeployError("хост скана задан неверно")
    if scan_host and scan_host.lower() == work_host.lower():
        raise DeployError("хост скана совпадает с рабочим")
    if not _USER_RE.match(user):
        raise DeployError("логин WEB_AUTH_USER содержит недопустимые символы")
    if not _BCRYPT_RE.match(hashed):
        raise DeployError("хэш пароля не похож на bcrypt")


def render_sites(template: str, work_host: str, scan_host: str, user: str, hashed: str) -> str:
    """Подставляет значения в шаблон блоков; без хоста скана его секция вырезается."""
    validate_params(work_host, scan_host, user, hashed)
    kept: list[str] = []
    in_scan = False
    for line in template.splitlines(keepends=True):
        marker = line.strip()
        if marker == SCAN_BEGIN:
            in_scan = True
        elif marker == SCAN_END:
            in_scan = False
        elif not in_scan or scan_host:
            kept.append(line)
    body = "".join(kept)
    for key, value in (
        ("%%WORK_HOST%%", work_host),
        ("%%SCAN_HOST%%", scan_host),
        ("%%AUTH_USER%%", user),
        ("%%AUTH_HASH%%", hashed),
    ):
        body = body.replace(key, value)
    if not body.endswith("\n"):
        body += "\n"
    return f"{MARK_BEGIN}\n{body}{MARK_END}\n"


def merge_sites(caddyfile: str, region: str, hosts: list[str]) -> str:
    """Вписывает размеченный блок Скаута в текст Caddyfile, не трогая чужое.

    Прежняя область между маркерами заменяется. Верхнеуровневый блок без
    маркеров, чьи адреса — только наши хосты, принимается в маркеры (первый
    прогон после ручной правки). Блок, где наш хост соседствует с чужим
    адресом, — отказ: разделять его автоматически нельзя. Остальные блоки
    остаются байт в байт. Нет ни области, ни такого блока — область в конец.
    """
    lines = caddyfile.splitlines(keepends=True)
    begins = [i for i, line in enumerate(lines) if line.strip() == MARK_BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == MARK_END]
    if len(begins) > 1 or len(ends) > 1 or len(begins) != len(ends):
        raise DeployError("маркеры techmost-scout в Caddyfile парные не по одному — разберите вручную")
    marked = (begins[0], ends[0] + 1) if begins else None
    if marked and begins[0] > ends[0]:
        raise DeployError("маркер конца techmost-scout стоит раньше начала")
    ours = {_normalize_address(host) for host in hosts if host}
    absorbed: list[tuple[int, int]] = []
    for start, stop, addresses in top_level_blocks(lines):
        if marked and start >= marked[0] and stop <= marked[1]:
            continue
        normalized = {_normalize_address(a) for a in addresses}
        if not normalized & ours:
            continue
        if not normalized <= ours:
            raise DeployError("в одном блоке наш хост соседствует с чужим адресом — разделите блок вручную")
        absorbed.append((start, stop))
    region_lines = region.splitlines(keepends=True)
    if marked:
        spans = [marked, *absorbed]
        anchor = marked[0]
    elif absorbed:
        spans = list(absorbed)
        anchor = absorbed[0][0]
    else:
        return _append_region(caddyfile, region)
    result: list[str] = []
    index = 0
    for start, stop in sorted(spans):
        result.extend(lines[index:start])
        if start == anchor:
            result.extend(region_lines)
        elif result and not result[-1].strip() and stop < len(lines) and not lines[stop].strip():
            stop += 1  # убрали блок — убираем и лишнюю пустую строку после него
        index = stop
    result.extend(lines[index:])
    return "".join(result)


def _append_region(caddyfile: str, region: str) -> str:
    """Дописывает область в конец файла, отделяя пустой строкой."""
    if not caddyfile:
        return region
    out = caddyfile if caddyfile.endswith("\n") else caddyfile + "\n"
    if out.splitlines()[-1].strip():
        out += "\n"
    return out + region


def hash_password(password: str) -> str:
    """bcrypt-хэш пароля от `caddy hash-password` edge-контейнера; пароль идёт через stdin."""
    code, hashed = "не выполнен", ""
    try:
        result = subprocess.run(  # noqa: S603
            HASH_COMMAND, input=(password + "\n").encode("utf-8"), capture_output=True, timeout=120, check=False
        )
        code = f"код {result.returncode}"
        hashed = result.stdout.decode("utf-8", "replace").strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        code = type(exc).__name__
    if not _BCRYPT_RE.match(hashed):
        raise DeployError(f"caddy hash-password не дал bcrypt-хэш ({code}); запасной путь — {HASH_FALLBACK}")
    return hashed


def run_caddy_block(caddyfile: str, template: str, env_file: str, work_host: str, scan_host: str) -> str:
    """Собирает блоки по шаблону и вписывает их в Caddyfile на месте; отчёт — без хэша."""
    with open(env_file, encoding="utf-8") as handle:
        env_text = handle.read()
    user = env_value(env_text, "WEB_AUTH_USER", "scout")
    hashed = env_value(env_text, "WEB_AUTH_HASH")  # запасной путь: хэш, посчитанный вручную
    password = env_value(env_text, "WEB_AUTH_PASSWORD")
    if not hashed and (not password or password == DEFAULT_WEB_PASSWORD):
        raise DeployError("WEB_AUTH_PASSWORD в .env не задан или остался умолчанием — хэш считать не от чего")
    with open(template, encoding="utf-8") as handle:
        template_text = handle.read()
    region = render_sites(template_text, work_host, scan_host, user, hashed or hash_password(password))
    with open(caddyfile, encoding="utf-8", errors="surrogateescape", newline="") as handle:
        old = handle.read()
    new = merge_sites(old, region, [work_host, scan_host])
    if new == old:
        return "Caddyfile не изменился"
    write_in_place(caddyfile, new.encode("utf-8", "surrogateescape"))
    return "Caddyfile обновлён: блоки Скаута между маркерами (хосты: {})".format(
        ", ".join(h for h in (work_host, scan_host) if h)
    )


# ── копии ───────────────────────────────────────────────────────────────────


def run_file_restore(backup: str, path: str) -> str:
    """Возвращает содержимое файла из копии `.bak-…` на месте (тот же inode)."""
    if ".bak-" not in os.path.basename(backup):
        raise DeployError("в качестве копии принимается только файл вида <имя>.bak-<время>")
    with open(backup, "rb") as handle:
        data = handle.read()
    write_in_place(path, data)
    return f"восстановлено из копии {backup}"


def run_db_backup(source: str, target: str) -> str:
    """Копия базы SQLite через `Connection.backup()`: во временное имя, затем переименование."""
    if not os.path.exists(source):
        raise DeployError(f"базы {source} нет")
    part = target + ".part"
    src = sqlite3.connect(f"file:{quote(source)}?mode=ro", uri=True, timeout=5)
    dst = sqlite3.connect(part, timeout=5)
    copied = False
    try:
        with dst:
            src.backup(dst)
        copied = True
    finally:
        dst.close()
        src.close()
        if not copied and os.path.exists(part):
            os.remove(part)
    os.rename(part, target)
    return f"копия базы: {target}"


# ── разбор команд ───────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Разбор аргументов подкоманд; значения секретов в аргументы не входят."""
    parser = argparse.ArgumentParser(description="Серверная часть деплоя Скаута")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("env-upsert", help="обновить ключи в .env (пары ИМЯ=значение из stdin)")
    p.add_argument("env_path")
    p = sub.add_parser("caddy-block", help="вписать блоки Скаута в Caddyfile между маркерами")
    p.add_argument("caddyfile")
    p.add_argument("template")
    p.add_argument("--env-file", required=True, help="docker/.env: логин и пароль для хэша")
    p.add_argument("--work-host", required=True)
    p.add_argument("--scan-host", default="")
    p = sub.add_parser("file-backup", help="копия файла рядом, права 600")
    p.add_argument("path")
    p = sub.add_parser("file-restore", help="вернуть файл из копии на месте")
    p.add_argument("backup")
    p.add_argument("path")
    p = sub.add_parser("db-backup", help="копия базы SQLite")
    p.add_argument("source")
    p.add_argument("target")
    return parser


def main(argv: list[str] | None = None, stdin_text: str | None = None) -> int:
    """Точка входа: 0 — выполнено, 2 — ожидаемый отказ (текст в stderr)."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "env-upsert":
            text = sys.stdin.read() if stdin_text is None else stdin_text
            message = run_env_upsert(args.env_path, text)
        elif args.command == "caddy-block":
            message = run_caddy_block(args.caddyfile, args.template, args.env_file, args.work_host, args.scan_host)
        elif args.command == "file-backup":
            message = make_backup(args.path)
        elif args.command == "file-restore":
            message = run_file_restore(args.backup, args.path)
        else:
            message = run_db_backup(args.source, args.target)
    except DeployError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
