"""Страж workflow деплоя `.github/workflows/deploy.yml` (Э6-9, РЕШ-272, РЕШ-287).

Живого запуска у workflow нет до слияния стопки в `main` и секретов владельца
(DLG-Э6-3), поэтому его безопасность держат эти проверки по тексту файла:
запуск только руками, права только на чтение, секреты и входы только через
`env:`, ни одной команды, печатающей секрет, строгая проверка хоста ssh,
edge-caddy — только копия, validate и reload через stdin (Э-58).

Файл читается `yaml.safe_load` (инв. №10). Ловушка YAML 1.1: ключ `on` без
кавычек читается как `True`, поэтому триггер берётся по обоим ключам. Сети и
ssh нет (инв. №7): `bash -n` только разбирает синтаксис, ничего не выполняя.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
SERVER_SCRIPT = REPO_ROOT / "scripts" / "deploy_server.py"
DEPLOY_MD = REPO_ROOT / "docs" / "deploy.md"

#: Команда из контракта п. 5 (Э-58): конфиг только через stdin, никогда не restart.
RELOAD = "docker exec -i edge-caddy caddy reload --config /dev/stdin --adapter caddyfile < /opt/edge-caddy/Caddyfile"
VALIDATE = (
    "docker exec -i edge-caddy caddy validate --config /dev/stdin --adapter caddyfile < /opt/edge-caddy/Caddyfile"
)

SECRETS = {
    "SCOUT_DEPLOY_SSH_KEY",
    "SCOUT_DEPLOY_KNOWN_HOSTS",
    "SCOUT_DEPLOY_HOST",
    "SCOUT_DEPLOY_USER",
    "DEEPSEEK_API_KEY",
    "YANDEX_API_KEY",
    "YANDEX_CATALOG_ID",
    "YANDEX_SEARCH_API_KEY",
}
VARIABLES = {
    "SCOUT_WORK_HOST",
    "SCOUT_SCAN_HOST",
    "EDGE_NEIGHBOR_URLS",
    "SCAN_LIMIT_PER_IP_PER_DAY",
    "SCAN_DAILY_BUDGET_RUB",
    "SCAN_COST_LIMIT_RUB",
    "YANDEX_SEARCH_RUB_PER_QUERY",
    "SCAN_ORDER_TELEGRAM_URL",
    "SCAN_ORDER_EMAIL",
}
ACTIONS = ["deploy", "doctor", "caddy-apply", "env-keys"]

_HEREDOC = re.compile(r"(?<!<)<<-?\s*(['\"]?)(\w+)\1")


def _doc() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps() -> list[dict]:
    return _doc()["jobs"]["run"]["steps"]


def _runs() -> list[str]:
    return [s["run"] for s in _steps() if "run" in s]


def _walk(node, path=()):
    """Все строки YAML-дерева с путём из ключей."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk(value, (*path, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, (*path, index))
    elif isinstance(node, str):
        yield path, node


def _env_values() -> dict[str, str]:
    """Имя → значение по всем блокам `env:` workflow (задание и шаги)."""
    found: dict[str, str] = {}
    doc = _doc()
    for env in [doc["jobs"]["run"].get("env", {}), *[s.get("env", {}) for s in _steps()]]:
        found.update({k: str(v) for k, v in env.items()})
    return found


def _secret_names() -> set[str]:
    return {name for name, value in _env_values().items() if "secrets." in value}


def _split_heredocs(run: str) -> list[tuple[str, str | None]]:
    """Строки `run`; у строки внутри here-document вторым элементом — строка-открывашка."""
    lines = run.splitlines()
    out: list[tuple[str, str | None]] = []
    i = 0
    while i < len(lines):
        opener = lines[i]
        out.append((opener, None))
        match = _HEREDOC.search(opener)
        if match:
            i += 1
            while i < len(lines) and lines[i].strip() != match.group(2):
                out.append((lines[i], opener))
                i += 1
            if i < len(lines):
                out.append((lines[i], opener))
        i += 1
    return out


def _step_actions(step: dict) -> set[str]:
    """Для каких действий выполняется шаг: имена из `if: inputs.action == '…'`, иначе для всех."""
    condition = str(step.get("if", ""))
    named = set(re.findall(r"inputs\.action == '([a-z-]+)'", condition))
    return named or set(ACTIONS)


def _step_index(fragment: str) -> int:
    for index, step in enumerate(_steps()):
        if fragment in step.get("run", ""):
            return index
    raise AssertionError(f"ни в одном шаге нет {fragment!r}")


# ── триггер, права, очередь ─────────────────────────────────────────────────


def test_файл_разбирается_и_триггер_только_workflow_dispatch() -> None:
    """`on` читается как `True` (YAML 1.1) — берём оба ключа; других триггеров нет."""
    doc = _doc()
    assert "on" in doc or True in doc, "в файле нет блока триггеров"
    trigger = doc.get("on", doc.get(True))
    assert set(trigger) == {"workflow_dispatch"}, f"лишние триггеры: {sorted(set(trigger) - {'workflow_dispatch'})}"


def test_входы_action_и_ref() -> None:
    """Ровно четыре действия; `ref` — строка с умолчанием `main`."""
    doc = _doc()
    inputs = doc.get("on", doc.get(True))["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"action", "ref"}
    assert inputs["action"]["type"] == "choice"
    assert sorted(inputs["action"]["options"]) == sorted(ACTIONS)
    assert len(inputs["action"]["options"]) == 4
    assert inputs["ref"]["type"] == "string" and inputs["ref"]["default"] == "main"


def test_права_только_чтение_очередь_без_отмены_и_таймаут() -> None:
    """`contents: read` на уровне файла; второй деплой ждёт, а не отменяет начатый."""
    doc = _doc()
    assert doc["permissions"] == {"contents": "read"}
    assert "permissions" not in doc["jobs"]["run"] or doc["jobs"]["run"]["permissions"] == {"contents": "read"}
    assert doc["concurrency"]["cancel-in-progress"] is False
    assert doc["concurrency"]["group"]
    assert 0 < doc["jobs"]["run"]["timeout-minutes"] <= 60


def test_нет_environment_и_действия_закреплены_по_версии() -> None:
    """Правила защиты окружений включает владелец (§10); сторонних действий без версии нет."""
    job = _doc()["jobs"]["run"]
    assert "environment" not in job
    for step in _steps():
        if "uses" in step:
            assert re.search(r"@v\d+$", step["uses"]), f"действие без версии: {step['uses']}"
            assert step.get("with", {}).get("persist-credentials") is False


# ── секреты и входы ─────────────────────────────────────────────────────────


def test_подстановки_секретов_и_входов_только_в_env() -> None:
    """`${{ … }}` встречается лишь в значениях `env:`; в `run`, `with`, `name` его нет."""
    for path, text in _walk(_doc()):
        if "${{" in text:
            assert "env" in path, f"подстановка вне env: {'/'.join(map(str, path))}"
    for run in _runs():
        assert "${{" not in run
        assert "secrets." not in run and "inputs." not in run


def test_секреты_и_переменные_ровно_из_списка_контракта() -> None:
    """Используются все секреты и переменные контракта и никаких лишних."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert set(re.findall(r"secrets\.([A-Z_]+)", text)) == SECRETS
    assert set(re.findall(r"vars\.([A-Z_]+)", text)) == VARIABLES
    assert "WEB_AUTH_PASSWORD" not in text, "пароль входа через workflow не ходит"


def test_секрет_не_печатается_ни_одной_командой() -> None:
    """Переменная секрета встречается в `run` только в теле here-document, пишущего в файл или в ssh."""
    names = _secret_names()
    assert len(names) == len(SECRETS), "в env заведены не все секреты контракта"
    allowed_opener = re.compile(r"^\s*(cat\s*>\s*(?!/dev/)\S+|ssh\s+scout-deploy\b.*)\s*<<")
    for run in _runs():
        for line, opener in _split_heredocs(run):
            used = [n for n in names if re.search(rf"\$\{{?{n}\b", line)]
            if not used:
                continue
            assert opener is not None and allowed_opener.match(opener), f"секрет {used} вне here-document: {line!r}"
            assert not re.search(r"\b(echo|printf)\b", line), f"секрет {used} печатается: {line!r}"


def test_нет_трассировки_и_дампов_окружения() -> None:
    """`set -x`, `printenv`, `env`, `declare -p`, `export -p`, `cat` файла `.env` в `run` запрещены."""
    banned = [
        r"set\s+-[a-z]*x",
        r"xtrace",
        r"\bprintenv\b",
        r"^\s*env\s*($|\|)",
        r"declare\s+-p",
        r"export\s+-p",
        r"\b(cat|less|more|tail|head|tac|nl)\b[^\n]*\.env\b",
        r"bash\s+-x",
    ]
    for run in _runs():
        for line in run.splitlines():
            for pattern in banned:
                assert not re.search(pattern, line), f"запрещённая команда {pattern!r}: {line!r}"


def test_ref_проверяется_до_первого_обращения_к_серверу() -> None:
    """Шаблон ссылки из контракта стоит в первом шаге, до любого ssh."""
    first = _steps()[0]["run"]
    assert "^[A-Za-z0-9._/-]+$" in first and '"$REF"' in first
    assert "ssh" not in first
    assert _env_values()["REF"].strip() == "${{ inputs.ref }}"


# ── ssh ─────────────────────────────────────────────────────────────────────


def test_ssh_строгая_проверка_хоста_и_ключ_удаляется() -> None:
    """Отпечаток хоста — из секрета; `ssh-keyscan` и отключения проверки нет; ключ стирается в конце."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "ssh-keyscan" not in text
    assert not re.search(r"StrictHostKeyChecking[= ]+(no|off|accept-new)", text, re.IGNORECASE)
    assert re.search(r"^\s*StrictHostKeyChecking yes$", text, re.MULTILINE)
    assert "UserKnownHostsFile ~/.ssh/known_hosts" in text
    assert not re.search(r"UserKnownHostsFile[= ]+/dev/null", text)
    assert "umask 077" in text and "chmod 600" in text
    last = _steps()[-1]
    assert last.get("if") == "always()"
    assert "rm -f" in last["run"] and "scout_deploy" in last["run"] and "known_hosts" in last["run"]
    for run in _runs():
        for line in run.splitlines():
            if re.match(r"^\s*(\w+=\$\()?ssh ", line):
                assert "ssh scout-deploy" in line, f"ssh мимо проверенного псевдонима: {line!r}"
                assert " -o " not in line


def test_первый_шаг_на_сервере_проверяет_версию_python_до_правок() -> None:
    """Версия python3 хоста проверяется раньше любой записи и доставки скрипта."""
    index = _step_index("sys.version_info >= (3, 8)")
    for earlier in _steps()[:index]:
        assert "ssh scout-deploy" not in earlier.get("run", ""), "обращение к серверу раньше проверки версии"
    run = _steps()[index]["run"]
    assert run.index("sys.version_info") < run.index("mktemp") < run.index("cat > $REMOTE_PY")


# ── порядок: копия раньше правки ────────────────────────────────────────────


def test_бэкап_раньше_git_fetch() -> None:
    """База (`db-backup`), `.env` (`file-backup`) копируются до получения новой ссылки."""
    backup = _step_index("db-backup")
    assert backup < _step_index("git fetch")
    assert "file-backup .env" in _steps()[backup]["run"]
    assert _step_index("git fetch") <= _step_index("git checkout") < _step_index("build scout")
    assert _step_index("build scout") < _step_index("up -d")


def test_копия_caddyfile_раньше_записи_блоков() -> None:
    """Копия `Caddyfile.bak-…` снимается раньше `caddy-block`."""
    text = "\n".join(_runs())
    assert re.search(r"file-backup /opt/edge-caddy/Caddyfile\)\s*$", text, re.MULTILINE), "копия не того файла"
    assert re.search(r"caddy-block /opt/edge-caddy/Caddyfile \"\$TPL\" ", text), "блоки пишутся не в тот файл"
    assert re.search(r"file-restore \"\$1\" /opt/edge-caddy/Caddyfile$", text, re.MULTILINE), "откат не в тот файл"
    assert _step_index("file-backup /opt/edge-caddy/Caddyfile") < _step_index("caddy-block")
    assert _step_index("caddy-block") < _step_index("file-restore")


def test_reload_только_через_stdin_validate_раньше_reload() -> None:
    """Каждый reload и validate — точная команда контракта; в шаге применения validate стоит раньше reload."""
    reloads = validates = 0
    for run in _runs():
        for line in run.splitlines():
            if "caddy reload" in line:
                reloads += 1
                assert line.strip() == RELOAD, f"reload не по контракту: {line!r}"
            if "caddy validate" in line:
                validates += 1
                assert line.strip() == VALIDATE, f"validate не по контракту: {line!r}"
    assert reloads >= 2 and validates >= 1, "ожидаются reload в применении и в откате, validate в применении"
    for run in _runs():
        if "caddy validate" in run and "caddy reload" in run:
            assert run.index("caddy validate") < run.index("caddy reload")
    assert "caddy validate" in _steps()[_step_index("caddy-block")]["run"]
    text = WORKFLOW.read_text(encoding="utf-8")
    assert set(re.findall(r"--config\s+(\S+)", text)) == {"/dev/stdin"}
    assert set(re.findall(r"edge-caddy caddy (\S+)", "\n".join(_runs()))) == {"reload", "validate"}


def test_слов_restart_и_опасных_команд_нет() -> None:
    """Edge обслуживает чужие сайты: ни перезапуска, ни остановки, ни чистки."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert not re.search(r"restart", text, re.IGNORECASE)
    for banned in (
        "compose down",
        "docker stop",
        "docker rm",
        "docker kill",
        "docker system prune",
        "docker image prune",
        "docker volume prune",
        "rm -rf",
        "volume rm",
        "sudo",
    ):
        assert banned not in text, f"в workflow {banned!r}"
    assert "194.87" not in text, "адрес сервера только в секрете и переменных"


def test_оба_compose_файла_названы_во_всех_вызовах() -> None:
    """Каждый `docker compose` идёт с основным файлом и надстройкой edge, иначе поднимется свой caddy."""
    calls = 0
    for run in _runs():
        for line in run.splitlines():
            for match in re.finditer(r"docker compose(.*)", line):
                calls += 1
                assert "-f docker-compose.yml -f docker-compose.edge.yml" in match.group(0), line
            if "$DC" in line:
                assert 'DC="docker compose -f docker-compose.yml -f docker-compose.edge.yml"' in run
    assert calls >= 3


# ── какое действие что делает ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("fragment", "expected"),
    [
        ("env-upsert", {"env-keys"}),
        ("caddy-block", {"caddy-apply"}),
        ("file-restore", {"caddy-apply"}),
        ("git fetch", {"deploy"}),
        ("db-backup", {"deploy"}),
        ("build scout", {"deploy"}),
        ("scout doctor", {"deploy", "env-keys", "doctor"}),
        ("up -d", {"deploy", "env-keys"}),
    ],
)
def test_шаг_выполняется_только_для_своих_действий(fragment: str, expected: set[str]) -> None:
    """`doctor` ничего не пишет; запись в `.env`, Caddyfile и git — только своим действием."""
    step = _steps()[_step_index(fragment)]
    assert _step_actions(step) == expected


def test_откат_только_при_сбое_и_только_после_копии() -> None:
    """Откат Caddyfile запускается по `failure()`, когда копия уже снята."""
    step = _steps()[_step_index("file-restore")]
    assert "failure()" in step["if"] and "steps.bak.outcome == 'success'" in step["if"]
    assert _steps()[_step_index("file-backup /opt/edge-caddy/Caddyfile")]["id"] == "bak"


def test_проверка_кодов_соседей_и_своих_хостов() -> None:
    """Рабочий `/` — 401, JSON-API не закрыт паролем прокси, скан — 200, соседи — не 5xx и не 000."""
    run = _steps()[_step_index("EDGE_NEIGHBOR_URLS//")]["run"]
    assert '/" 401' in run and "/api/v1/" in run and "www-authenticate" in run.lower()
    assert '/scan" 200' in run
    assert "[1-4][0-9][0-9]" in run, "соседний сайт с 5xx или 000 обязан давать красный шаг"
    assert run.rstrip().endswith('exit "$fail"')


def test_env_keys_пишет_ровно_закреплённый_список_ключей() -> None:
    """Имена в here-document env-upsert совпадают с `ALLOWED_ENV_KEYS` серверного скрипта."""
    spec = importlib.util.spec_from_file_location("deploy_server_for_workflow", SERVER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    run = _steps()[_step_index("env-upsert")]["run"]
    names = {line.split("=", 1)[0] for line, opener in _split_heredocs(run) if opener and "=" in line}
    assert names == set(module.ALLOWED_ENV_KEYS)
    assert "PUBLIC_SCAN_HOST=${SCOUT_SCAN_HOST}" in run
    assert "env-upsert /opt/techmost-scout/docker/.env" in run


def test_синтаксис_bash_шагов_и_удалённых_скриптов() -> None:
    """`bash -n` разбирает каждый `run` и каждый удалённый скрипт; ничего не выполняется."""
    scripts: list[str] = list(_runs())
    for run in _runs():
        lines = _split_heredocs(run)
        for opener in {o for _, o in lines if o and "'" in o}:
            body = [text for text, o in lines if o == opener and not re.fullmatch(r"\s*REMOTE", text)]
            scripts.append("\n".join(body))
    for script in scripts:
        result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True, check=False)  # noqa: S607
        assert result.returncode == 0, result.stderr


# ── документация ────────────────────────────────────────────────────────────


def _docs_section() -> str:
    text = DEPLOY_MD.read_text(encoding="utf-8")
    match = re.search(r"^## 13\. Деплой через GitHub Actions.*?(?=^## \d+\.|\Z)", text, re.MULTILINE | re.DOTALL)
    assert match is not None, "в docs/deploy.md нет раздела «13. Деплой через GitHub Actions»"
    return match.group(0)


def test_раздел_деплоя_называет_секреты_переменные_и_порядок() -> None:
    """Имена секретов и переменных, порядок действий, откат и действия владельца — без значений."""
    section = _docs_section()
    for name in SECRETS | VARIABLES:
        assert name in section, f"в разделе нет {name}"
    order = [section.find(f"`{a}`") for a in ("env-keys", "deploy", "caddy-apply", "doctor")]
    assert all(i >= 0 for i in order)
    assert "env-keys → deploy → caddy-apply → doctor" in section
    for word in ("authorized_keys", "отпечаток", "ssh-keygen", "ветк", "откат", "validate", "stdin"):
        assert word in section, f"в разделе нет упоминания {word!r}"
    assert "194.87" not in section and "BEGIN" not in section


def test_адрес_сервера_не_попал_в_файлы_деплоя() -> None:
    """Критерий 5: адрес только в секрете и переменных."""
    for path in (
        WORKFLOW,
        SERVER_SCRIPT,
        DEPLOY_MD,
        REPO_ROOT / "docker" / "edge" / "scout-sites.caddy",
        REPO_ROOT / "docker" / "Caddyfile",
    ):
        assert "194.87" not in path.read_text(encoding="utf-8"), path.name
