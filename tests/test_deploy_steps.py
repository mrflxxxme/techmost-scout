"""Поведение шагов workflow деплоя на заглушках (Э6-9, цикл 2 ревью, блокирующая 3).

Текстовый страж `test_deploy_workflow.py` видит подстроки, а не поведение: мутация
`fail=1` → `fail=0` в проверке соседей или убранный `exit 1` в ожидании готовности он
пропускал. Здесь настоящий `run` каждого шага достаётся из YAML и выполняется `bash` с
заглушками в `PATH`: `curl`, `sleep`, `docker`, `ssh` (запускает «удалённый» скрипт локально,
подменяя `/opt/…` каталогом теста) и `python3`. Серверный `scripts/deploy_server.py` — настоящий.
Сети, ssh и docker нет (инв. 7); значения — заглушки, не секреты.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
SERVER_SCRIPT = REPO_ROOT / "scripts" / "deploy_server.py"
TEMPLATE = REPO_ROOT / "docker" / "edge" / "scout-sites.caddy"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash")

WORK, SCAN = "scout.test.example", "scan.test.example"
NEIGHBORS = "https://a.test.example/ https://b.test.example/"
FAKE_HASH = "$2" + "a$14$" + "Z" * 53
PASSWORD = "stub-web-password-5be1d7"
BAK_OK = "/opt/edge-caddy/Caddyfile.bak-20261021-093000"

CURL = r"""#!/bin/bash
url="${*: -1}"
echo "curl $url" >> "$STUB_LOG"
code=$(awk -v u="$url" '$1 == u { print $2; exit }' "$CURL_MAP")
extra=$(awk -v u="$url" '$1 == u { print $3; exit }' "$CURL_MAP")
code=${code:-000}
for a in "$@"; do
  if [ "$a" = -D ]; then
    if [ "$code" = 000 ]; then exit 6; fi
    printf 'HTTP/2 %s\r\n' "$code"
    if [ "$extra" = basic ]; then printf 'www-authenticate: Basic realm="x"\r\n'; fi
    printf '\r\n'
    exit 0
  fi
done
printf '%s' "$code"
if [ "$code" = 000 ]; then exit 6; fi
"""

SLEEP = '#!/bin/bash\necho "sleep $*" >> "$STUB_LOG"\n'

DOCKER = r"""#!/bin/bash
echo "docker $*" >> "$STUB_LOG"
case "$*" in
  *"caddy hash-password"*) cat > /dev/null; echo "$FAKE_HASH" ;;
  *"caddy validate"*) cat > "$STUB_DIR/validated.txt"; exit "${VALIDATE_RC:-0}" ;;
  *"caddy reload"*) cat > "$STUB_DIR/reloaded.txt"; exit "${RELOAD_RC:-0}" ;;
  "inspect "*)
    head -n 1 "$STATES_FILE"
    if [ "$(wc -l < "$STATES_FILE")" -gt 1 ]; then
      tail -n +2 "$STATES_FILE" > "$STATES_FILE.next"; mv "$STATES_FILE.next" "$STATES_FILE"
    fi ;;
  *" ps "*) echo cid-1 ;;
  *" logs "*) echo "журнал scout" ;;
  *"scout doctor"*) cat "$DOCTOR_OUT" ;;
esac
"""

SSH = """#!{python}
import os, subprocess, sys

root = os.environ["FAKE_ROOT"]
args = sys.argv[2:]
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
    log.write("ssh " + " ".join(args)[:200] + "\\n")


def rewrite(text):
    for path in ("/opt/techmost-scout", "/opt/edge-caddy"):
        text = text.replace(path, root + path)
    return text


if "SSH_REPLY" in os.environ:
    sys.stdout.write(os.environ["SSH_REPLY"])
    sys.exit(0)
if args[:2] == ["bash", "-s"]:
    script = rewrite(sys.stdin.read())
    cmd = ["bash", "-s", *[rewrite(a) for a in args[2:]]]
    done = subprocess.run(cmd, input=script, capture_output=True, text=True)
else:
    done = subprocess.run(["bash", "-c", rewrite(" ".join(args))], stdin=sys.stdin, capture_output=True, text=True)
sys.stdout.write(done.stdout.replace(root, ""))
sys.stderr.write(done.stderr.replace(root, ""))
sys.exit(done.returncode)
"""


class Box:
    """«Сервер» в каталоге теста, заглушки команд и запуск настоящего `run` шага workflow."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.root = tmp / "root"
        self.bin = tmp / "bin"
        self.stub_dir = tmp / "stub"
        for path in (self.bin, self.stub_dir, self.root / "opt/techmost-scout/docker", self.root / "opt/edge-caddy"):
            path.mkdir(parents=True)
        self.log = self.stub_dir / "calls.log"
        self.log.write_text("", encoding="utf-8")
        for name, body in (("curl", CURL), ("sleep", SLEEP), ("docker", DOCKER)):
            self._tool(name, body)
        self._tool("ssh", SSH.format(python=sys.executable))
        self._tool("python3", f'#!/bin/bash\nexec "{sys.executable}" "$@"\n')
        self.curl_map = self.stub_dir / "curl_map"
        self.states = self.stub_dir / "states"
        self.doctor = self.stub_dir / "doctor.txt"
        for f in (self.curl_map, self.states, self.doctor):
            f.write_text("", encoding="utf-8")
        self.caddyfile = self.root / "opt/edge-caddy/Caddyfile"
        self.env_file = self.root / "opt/techmost-scout/docker/.env"
        self.output = tmp / "github_output"
        self.output.write_text("", encoding="utf-8")

    def _tool(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def run(self, step_name: str, **env: str) -> subprocess.CompletedProcess:
        """Выполняет `run` шага так, как это делает раннер: `bash -eo pipefail`."""
        steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["run"]["steps"]
        script = next(s["run"] for s in steps if s.get("name") == step_name)
        full = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.tmp),
            "LC_ALL": "C.UTF-8",
            "STUB_LOG": str(self.log),
            "STUB_DIR": str(self.stub_dir),
            "FAKE_ROOT": str(self.root),
            "FAKE_HASH": FAKE_HASH,
            "CURL_MAP": str(self.curl_map),
            "STATES_FILE": str(self.states),
            "DOCTOR_OUT": str(self.doctor),
            "RUNNER_TEMP": str(self.tmp),
            "GITHUB_OUTPUT": str(self.output),
            "REMOTE_PY": str(SERVER_SCRIPT),
            "REMOTE_TPL": str(TEMPLATE),
        }
        full.update(env)
        return subprocess.run(  # noqa: S603
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],  # noqa: S607
            capture_output=True,
            text=True,
            env=full,
            timeout=120,
            check=False,
        )


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def _map(box: Box, **codes: str) -> None:
    """Таблица ответов `curl`: ключ — URL, значение — «код» или «код basic»."""
    box.curl_map.write_text("".join(f"{url} {code}\n" for url, code in codes.items()), encoding="utf-8")


def _good_codes() -> dict[str, str]:
    return {
        f"https://{WORK}/": "401",
        f"https://{WORK}/api/v1/requests/0": "404",
        f"https://{SCAN}/scan": "200",
        "https://a.test.example/": "200",
        "https://b.test.example/": "301",
    }


def _shown(done: subprocess.CompletedProcess) -> str:
    return done.stdout + done.stderr


# ── проверка входов и ветки ─────────────────────────────────────────────────

INPUTS = "Проверка входов и ветки запуска"
GOOD_INPUTS = {
    "ACTION": "deploy",
    "REF": "main",
    "RUN_REF": "refs/heads/main",
    "DEFAULT_BRANCH": "main",
    "SCOUT_WORK_HOST": WORK,
    "SCOUT_SCAN_HOST": SCAN,
    "EDGE_NEIGHBOR_URLS": NEIGHBORS,
}


def _inputs(box: Box, **change: str) -> subprocess.CompletedProcess:
    return box.run(INPUTS, **{**GOOD_INPUTS, **change})


@pytest.mark.parametrize("ref", ["main", "v1.2.3", "feature/x_y-1", "origin/main", "0a1b2c3d4e5f"])
def test_допустимые_ссылки_проходят(box: Box, ref: str) -> None:
    assert _inputs(box, REF=ref).returncode == 0


@pytest.mark.parametrize(
    "ref",
    ["-x", "--upload-pack=x", "a..b", "..", "a b", "a;b", "$(id)", "a`id`", "a\nb", "", "ветка", "a*b"],
)
def test_недопустимые_ссылки_отвергаются_до_сервера(box: Box, ref: str) -> None:
    """Пробел, `;`, подстановка, ведущий `-` (стал бы опцией git) и `..` — красный шаг."""
    done = _inputs(box, REF=ref)
    assert done.returncode == 1 and "ref не прошёл проверку" in _shown(done)
    assert box.calls() == []


@pytest.mark.parametrize("run_ref", ["refs/heads/feature", "refs/tags/v1", "refs/heads/main2", "", "refs/pull/1/merge"])
def test_запуск_не_из_ветки_по_умолчанию_отвергается(box: Box, run_ref: str) -> None:
    done = _inputs(box, RUN_REF=run_ref)
    assert done.returncode == 1 and "ветки по умолчанию" in _shown(done)


def test_ветка_по_умолчанию_может_называться_иначе(box: Box) -> None:
    assert _inputs(box, RUN_REF="refs/heads/master", DEFAULT_BRANCH="master").returncode == 0
    assert _inputs(box, RUN_REF="refs/heads/main", DEFAULT_BRANCH="").returncode == 1


@pytest.mark.parametrize("host", ["https://x.example", "a b", "a/b", "a:443", "x.example/"])
def test_хосты_только_без_схемы_порта_и_пути(box: Box, host: str) -> None:
    assert _inputs(box, SCOUT_WORK_HOST=host).returncode == 1
    assert _inputs(box, SCOUT_SCAN_HOST=host).returncode == 1


def test_caddy_apply_требует_рабочий_хост_и_соседей(box: Box) -> None:
    assert _inputs(box, ACTION="caddy-apply").returncode == 0
    assert _inputs(box, ACTION="caddy-apply", SCOUT_WORK_HOST="").returncode == 1
    assert _inputs(box, ACTION="caddy-apply", EDGE_NEIGHBOR_URLS="").returncode == 1
    assert _inputs(box, ACTION="deploy", EDGE_NEIGHBOR_URLS="", SCOUT_WORK_HOST="").returncode == 0


@pytest.mark.parametrize("urls", ["-o/etc/x", "ftp://a.example/", "https://a.example/ -K", "a.example", "https://"])
def test_адреса_соседей_только_со_схемой(box: Box, urls: str) -> None:
    """Значение, начинающееся с `-`, стало бы опцией curl."""
    done = _inputs(box, EDGE_NEIGHBOR_URLS=urls)
    assert done.returncode == 1 and "EDGE_NEIGHBOR_URLS" in _shown(done)


def test_адреса_соседей_через_запятую_и_пробел(box: Box) -> None:
    assert _inputs(box, EDGE_NEIGHBOR_URLS="https://a.example/,http://b.example:8080/x").returncode == 0


# ── ожидание готовности ─────────────────────────────────────────────────────

WAIT = "Ожидание готовности scout"


@pytest.mark.parametrize("states", [["healthy"], ["starting", "starting", "healthy"]])
def test_ожидание_готовности_зелёное_когда_healthy(box: Box, states: list[str]) -> None:
    box.states.write_text("\n".join(states) + "\n", encoding="utf-8")
    done = box.run(WAIT)
    assert done.returncode == 0 and "Состояние scout: healthy" in done.stdout
    assert "журнал scout" not in done.stdout


@pytest.mark.parametrize("states", [["unhealthy"], ["starting"], ["starting", "unhealthy"], ["none"]])
def test_ожидание_готовности_красное_без_healthy_и_с_журналом(box: Box, states: list[str]) -> None:
    """`unhealthy` и «так и не стал healthy» краснят шаг и печатают хвост журнала контейнера."""
    box.states.write_text("\n".join(states) + "\n", encoding="utf-8")
    done = box.run(WAIT)
    assert done.returncode == 1 and "журнал scout" in done.stdout
    assert any(" logs --tail 40 scout" in call for call in box.calls())


# ── scout doctor ────────────────────────────────────────────────────────────

DOCTOR = "Проверка scout doctor"
READY = "Схема базы совпадает с моделями.\n\nГотово к работе. Рабочие модели: deepseek\n"


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        (READY, True),
        ("Схема базы разошлась с моделями: нет таблицы\n" + READY, False),
        ("Схема базы совпадает с моделями.\n\nНи одна модель не отвечает. Начните с DEEPSEEK_API_KEY\n", False),
        ("Ни одна модель не отвечает.\n" + READY, False),
        ("Схема базы совпадает с моделями.\n", False),
        ("", False),
    ],
    ids=["готово", "расхождение схемы", "нет моделей", "нет моделей при готово", "нет строки готовности", "пусто"],
)
def test_doctor_краснеет_по_тексту_вывода_при_коде_ноль(box: Box, text: str, ok: bool) -> None:
    """Сам `scout doctor` завершается кодом 0 всегда: шаг читает его вывод."""
    box.doctor.write_text(text, encoding="utf-8")
    done = box.run(DOCTOR)
    assert (done.returncode == 0) is ok
    assert "Схема базы" in done.stdout or not text, "вывод doctor должен попасть в журнал шага"


# ── пересоздание scout ──────────────────────────────────────────────────────


def test_env_keys_пересоздаёт_контейнер_принудительно_а_deploy_нет(box: Box) -> None:
    """Без `--force-recreate` ключи записаны в `.env`, а контейнер их не перечитал бы."""
    assert box.run("Пересоздание scout", ACTION="env-keys").returncode == 0
    assert any(c.endswith("up -d --force-recreate scout") for c in box.calls())
    box.log.write_text("", encoding="utf-8")
    assert box.run("Пересоздание scout", ACTION="deploy").returncode == 0
    ups = [c for c in box.calls() if " up " in c]
    assert len(ups) == 1 and ups[0].endswith("up -d") and "force-recreate" not in ups[0]


def test_env_keys_пишет_ключи_и_хост_скана_без_вывода_значений(box: Box) -> None:
    """Значения секретов уходят в `.env` через stdin и не попадают ни в вывод, ни в аргументы ssh."""
    box.env_file.write_text("APP_ENV=prod\nWEB_AUTH_PASSWORD=keep-me-1\n", encoding="utf-8")
    secrets = {
        "DEEPSEEK_API_KEY": "stub-deepseek-9a1c",
        "YANDEX_API_KEY": "stub-yandex-77b2",
        "YANDEX_CATALOG_ID": "stubcatalog01",
        "YANDEX_SEARCH_API_KEY": "stub-search-5d0e",
    }
    done = box.run("Обновление ключей в .env", SCOUT_SCAN_HOST=SCAN, SCAN_ORDER_EMAIL="", **secrets)
    assert done.returncode == 0, done.stderr
    text = box.env_file.read_text(encoding="utf-8")
    assert "APP_ENV=prod" in text and "WEB_AUTH_PASSWORD=keep-me-1" in text
    for name, value in secrets.items():
        assert f"{name}={value}" in text
        assert value not in _shown(done) and not any(value in call for call in box.calls())
    assert f"PUBLIC_SCAN_HOST={SCAN}" in text


# ── копия Caddyfile, применение блоков, откат ───────────────────────────────

BACKUP = "Копия Caddyfile"
APPLY = "Применение блоков Скаута в edge-Caddyfile"
ROLLBACK = "Откат Caddyfile из копии"
FOREIGN = 'neighbor.test.example {\n    respond "сосед"\n}\n'


def test_копия_caddyfile_отдаёт_путь_только_нужного_вида(box: Box) -> None:
    box.caddyfile.write_text(FOREIGN, encoding="utf-8")
    done = box.run(BACKUP)
    assert done.returncode == 0, done.stderr
    path = box.output.read_text(encoding="utf-8").strip().removeprefix("path=")
    assert path.startswith("/opt/edge-caddy/Caddyfile.bak-")
    assert (box.root / path.lstrip("/")).read_text(encoding="utf-8") == FOREIGN


@pytest.mark.parametrize(
    "reply", ["/etc/passwd\n", "/opt/edge-caddy/Caddyfile.bak-1; rm -f x\n", "\n", "ошибка: нет\n"]
)
def test_копия_caddyfile_неожиданный_путь_красный_шаг(box: Box, reply: str) -> None:
    done = box.run(BACKUP, SSH_REPLY=reply)
    assert done.returncode == 1 and "неожиданный путь" in _shown(done)
    assert box.output.read_text(encoding="utf-8") == ""


def _apply_setup(box: Box) -> None:
    box.caddyfile.write_text(FOREIGN, encoding="utf-8")
    box.env_file.write_text(f"APP_ENV=prod\nWEB_AUTH_USER=owner\nWEB_AUTH_PASSWORD={PASSWORD}\n", encoding="utf-8")


def test_применение_блоков_хост_скана_доходит_до_скрипта(box: Box) -> None:
    """С хостом скана в файле есть блок скана; validate стоит раньше reload и видит новый файл."""
    _apply_setup(box)
    done = box.run(APPLY, SCOUT_WORK_HOST=WORK, SCOUT_SCAN_HOST=SCAN)
    assert done.returncode == 0, done.stderr
    text = box.caddyfile.read_text(encoding="utf-8")
    assert FOREIGN in text and f"{WORK} {{" in text and f"{SCAN} {{" in text, "блок скана не дошёл до файла"
    assert f"owner {FAKE_HASH}" in text
    kinds = [m.group(1) for c in box.calls() if (m := re.search(r"edge-caddy caddy (\S+)", c))]
    assert kinds == ["hash-password", "validate", "reload"]
    assert (box.stub_dir / "validated.txt").read_text(encoding="utf-8") == text
    assert PASSWORD not in _shown(done) and FAKE_HASH not in _shown(done)


def test_применение_блоков_без_хоста_скана_блока_скана_нет(box: Box) -> None:
    _apply_setup(box)
    done = box.run(APPLY, SCOUT_WORK_HOST=WORK, SCOUT_SCAN_HOST="")
    text = box.caddyfile.read_text(encoding="utf-8")
    assert done.returncode == 0 and f"{WORK} {{" in text and "[[" not in text and SCAN not in text


def test_применение_блоков_validate_красный_reload_не_запускается(box: Box) -> None:
    _apply_setup(box)
    done = box.run(APPLY, SCOUT_WORK_HOST=WORK, SCOUT_SCAN_HOST=SCAN, VALIDATE_RC="1")
    assert done.returncode != 0
    assert not any("caddy reload" in c for c in box.calls())


def test_откат_неопознанный_путь_копии_до_ssh_не_доходит(box: Box) -> None:
    """Путь из вывода предыдущего шага проверяется ещё раз: чужой путь серверу не передаётся."""
    for bak in (
        "",
        "/etc/shadow",
        "/opt/edge-caddy/Caddyfile.bak-1;id",
        "/opt/edge-caddy/../etc/x.bak-20261021-093000",
    ):
        done = box.run(ROLLBACK, BAK=bak)
        assert done.returncode == 1 and "откат не выполняется" in _shown(done), bak
    assert box.calls() == [], "в ssh ушёл неопознанный путь"


def test_откат_возвращает_файл_из_копии_и_перечитывает_конфиг(box: Box) -> None:
    box.caddyfile.write_text("BROKEN\n", encoding="utf-8")
    (box.root / BAK_OK.lstrip("/")).write_text(FOREIGN, encoding="utf-8")
    inode = box.caddyfile.stat().st_ino
    done = box.run(ROLLBACK, BAK=BAK_OK)
    assert done.returncode == 0, done.stderr
    assert box.caddyfile.read_text(encoding="utf-8") == FOREIGN and box.caddyfile.stat().st_ino == inode
    assert (box.stub_dir / "reloaded.txt").read_text(encoding="utf-8") == FOREIGN


# ── коды своих и соседних сайтов ────────────────────────────────────────────

CODES = "Коды своих и соседних сайтов"
CODES_ENV = {"SCOUT_WORK_HOST": WORK, "SCOUT_SCAN_HOST": SCAN, "EDGE_NEIGHBOR_URLS": NEIGHBORS}


def _codes(box: Box, change: dict[str, str] | None = None, **env: str) -> subprocess.CompletedProcess:
    _map(box, **{**_good_codes(), **(change or {})})
    return box.run(CODES, **{**CODES_ENV, **env})


def test_коды_все_в_порядке_зелёный_шаг(box: Box) -> None:
    done = _codes(box)
    assert done.returncode == 0, done.stdout
    assert "Рабочий хост без пароля: 401" in done.stdout and "Публичный скан: 200" in done.stdout


@pytest.mark.parametrize("code", ["200", "301", "403", "404", "418"])
def test_сосед_с_ответом_ниже_5xx_не_краснит_шаг(box: Box, code: str) -> None:
    assert _codes(box, {"https://a.test.example/": code}).returncode == 0


@pytest.mark.parametrize("code", ["500", "502", "503", "504", "000"])
def test_сосед_с_5xx_или_без_ответа_краснит_шаг(box: Box, code: str) -> None:
    """Сосед, упавший после reload, — повод для отката."""
    for neighbor in ("https://a.test.example/", "https://b.test.example/"):
        done = _codes(box, {neighbor: code})
        assert done.returncode == 1 and f"Соседний сайт {neighbor}: {code}" in done.stdout


@pytest.mark.parametrize("code", ["000", "502", "504"])
def test_json_api_без_ответа_или_с_ошибкой_шлюза_краснит_шаг(box: Box, code: str) -> None:
    done = _codes(box, {f"https://{WORK}/api/v1/requests/0": code})
    assert done.returncode == 1


@pytest.mark.parametrize("code", ["200", "404", "422", "401"])
def test_json_api_с_ответом_приложения_зелёный(box: Box, code: str) -> None:
    done = _codes(box, {f"https://{WORK}/api/v1/requests/0": code})
    assert done.returncode == 0, done.stdout


def test_json_api_закрытый_паролем_прокси_краснит_шаг(box: Box) -> None:
    done = _codes(box, {f"https://{WORK}/api/v1/requests/0": "401 basic"})
    assert done.returncode == 1 and "закрыт паролем прокси" in done.stdout


@pytest.mark.parametrize("code", ["200", "302", "403", "000"])
def test_рабочий_хост_без_пароля_обязан_давать_401(box: Box, code: str) -> None:
    assert _codes(box, {f"https://{WORK}/": code}).returncode == 1


@pytest.mark.parametrize("code", ["401", "404", "502", "000"])
def test_публичный_скан_обязан_давать_200_а_без_хоста_не_проверяется(box: Box, code: str) -> None:
    assert _codes(box, {f"https://{SCAN}/scan": code}).returncode == 1
    assert _codes(box, {f"https://{SCAN}/scan": code}, SCOUT_SCAN_HOST="").returncode == 0


def test_соседи_через_запятую_проверяются_все(box: Box) -> None:
    done = _codes(
        box, {"https://b.test.example/": "502"}, EDGE_NEIGHBOR_URLS="https://a.test.example/,https://b.test.example/"
    )
    assert done.returncode == 1 and "Соседний сайт https://a.test.example/: 200" in done.stdout
