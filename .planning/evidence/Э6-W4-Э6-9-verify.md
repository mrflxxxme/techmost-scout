# Верификация Э6-9 · Деплой через GitHub Actions (полный цикл)
Вердикт: FAIL (закрыто 5 из 5 критериев задачи и 7 из 8 общих; открыт общий критерий 3 — контейнер python:3.12-slim, DLG-1)

Ветка claude/e6-9, HEAD ae7ad2b. База claude/sleepy-planck-11w8jv = 97d6219: опережает HEAD только записью РЕШ-291 в `.planning/DECISIONS.md` (отсюда −29 строк в `git diff base HEAD`, это не правка задачи). Ревью: цикл 1 ДОРАБОТАТЬ, цикл 2 ПРИНЯТЬ.

## Критерии задачи (PLAN.md, «### Э6-9», «Критерии приёмки»)

| № | Критерий из PLAN.md | Команда | Факт | Статус |
|---|---|---|---|---|
| 1 | `tests/test_deploy_workflow.py` проходит | `pytest tests/test_deploy_workflow.py -v` | 30 passed | ок |
| 2 | `tests/test_deploy_server.py` проходит | `pytest tests/test_deploy_server.py -v` | 42 passed; ast.parse с feature_version=(3,8) — test_синтаксис_python_3_8…; вывод без значений — test_env_upsert_вывод_без_значений… (capsys); inode — test_caddy_block_пишет_на_месте_inode_прежний (st_ino) | ок |
| 3 | Мутации (7 штук), каждая ≥1 failed | по одной на копии `git archive HEAD`, откат проверен сравнением с HEAD (см. ниже) | базовый прогон копии 72 passed; М1 reload /etc/caddy: 1 failed; М2 validate после reload: 1 failed; М3 echo секрета в run: 1 failed; М4 триггер push: 1 failed; М5 без шага бэкапа: 2 failed; М6 временный файл и os.replace: 4 failed; М7 env-upsert печатает значение: 1 failed | ок |
| 4 | Прежние стражи зелёные без правок | `pytest tests/test_deploy_auth_parity.py tests/test_docker_compose_edge.py tests/test_caddyfile.py tests/test_docker_compose.py tests/test_scripts_layout.py tests/test_tests_layout.py -q` | 56 passed; diff по этим файлам против fa65e23 и base пуст | ок |
| 5 | `rg -n "194\.87" .github docker docs/deploy.md scripts` пусто | rg (указанные пути) | пусто, rc=1 | ок |

Мутации М1–М7 прогонялись на копии HEAD в каталоге scratchpad (`git archive HEAD | tar -x`), рабочая копия e6-9 не менялась. Каждая мутация: правка одного файла, `pytest tests/test_deploy_workflow.py tests/test_deploy_server.py -q`, восстановление файла из HEAD и сравнение с HEAD.

## Общие критерии (PLAN.md, «Общие правила»; задание верификатора)

| № | Критерий | Команда | Факт | Статус |
|---|---|---|---|---|
| G1 | `pytest -q` — 0 failed, ожидание около 2156 | `env -u … pytest -q` | 2156 passed, 0 failed, exit 0 | ок |
| G1б | то же без сети (инв. 7) | `unshare -rn env -u … pytest -q` | 2156 passed, 0 failed, exit 0 | ок |
| G2 | ruff чистый | `ruff check . && ruff format --check .` | All checks passed!; 262 files already formatted | ок |
| G3 | прогон в контейнере python:3.12-slim (РЕШ-249) | `docker info` | docker CLI есть, демон недоступен; контейнер не прогнан | НЕ ЗАКРЫТ — DLG-1 |
| G4 | прежние тесты не правятся | `git diff --name-only fa65e23 HEAD`; `git diff --stat base HEAD -- tests/` | в tests/ только новые файлы test_deploy_{workflow,server,steps,caddy_blocks}.py | ок |
| G5 | размеры: scripts/*.py ≤500, backend/app ≤500 | `wc -l`; test_scripts_layout, test_backend_layout | deploy_server.py 499 строк; в backend/app файлов >500 нет | ок |
| G6 | инв. 6: httpx только в разрешённых местах | `rg -ln httpx backend/app` минус разрешённые | пусто | ок |
| G7 | инв. 3 по РЕШ-287: добавленные строки диффа | `git diff base HEAD \| grep "^+" \| grep -E "sk-\|gigachat\|Api-Key"` | 2 совпадения, обе в тексте evidence-отчётов (checks.txt, review-2.md), в коде и тестах 0; по дереву `sk-` + 20 символов — 0; в файлах задачи (.github, docker, scripts, docs/deploy.md, tests/test_deploy_*) совпадений 0 | ок |
| G8 | новых зависимостей нет | `git diff --name-only base HEAD` | pyproject.toml не тронут | ок |

## Инварианты

№3 добавленные строки чисты (см. G7), секретов в файлах задачи нет, `.env` в .gitignore · №6 пусто (G6) · №7 pytest без сети: 2156 passed (G1б) · №8 backend/ в диффе не тронут, новое поведение покрыто 7 новыми тест-файлами (test_deploy_*) · №10 test_deploy_workflow.py разбирает файл через yaml.safe_load, ключ `on` берётся по обоим вариантам · №4 test_prompts входит в полный прогон (2156 passed); промпты не менялись · №1, №2, №5, №9 к задаче не относятся напрямую, нарушений в diff нет.

## Проверено по содержимому workflow (факты команд)

* Триггер: только `workflow_dispatch`; `action` — choice из четырёх вариантов (deploy, doctor, caddy-apply, env-keys); `ref` — string, default main.
* `permissions: contents: read`; `concurrency.cancel-in-progress: false`; `timeout-minutes: 45`.
* `${{ secrets.* }}` и `${{ inputs.* }}` встречаются только в блоках `env:` (строки 55–56, 103–106, 201–204).
* Строки `echo` в run печатают только литералы и служебные переменные (`$label`, `$BAK`, `$api`, `$REMOTE_PY` в env-файле GITHUB_ENV); секретные переменные в run попадают только в heredoc, пишущий в файл или в ssh.
* `caddy validate` (строка 313) стоит раньше `caddy reload` (строка 314) в шаге применения; reload в откате — строка 374; все три — с `--config /dev/stdin … < /opt/edge-caddy/Caddyfile`.
* Шаг «Бэкап перед деплоем» (строка 143) идёт раньше «Получение ссылки» (git fetch, строка 164).
* Слов `restart`, `ssh-keyscan`, `StrictHostKeyChecking no|off|accept-new` в файле нет; есть `StrictHostKeyChecking yes` (строка 122).
* Шаги workflow: 16 шагов, последний — «Удаление ключа и временных файлов» (`if: always()`).

## Отложено

* **DLG-1 (общий критерий 3, контейнер python:3.12-slim).** Причина: в среде нет демона Docker (`docker info` не отвечает). Действие владельца: по РЕШ-276 пункт закрывает CI GitHub на ветке интеграции после слияния (Ubuntu, Python 3.12) — ссылка на прогон идёт в ворота; либо прогон на машине с Docker. До ссылки или решения владельца в воротах пункт не закрыт.
* **DLG-Э6-3 (живой деплой через Actions).** Не критерий задачи (в «Отложенностях» плана). Нужны ключи ssh, секреты и переменные владельца, запуск только из ветки по умолчанию после слияния стопки в main и «да» владельца. Живой запуск не выполнялся, ssh не делался, секреты не читались и не печатались.

## Причина FAIL

Один незакрытый пункт: общий критерий 3 (контейнер python:3.12-slim) не прогнан, демона Docker в среде нет. Все критерии задачи закрыты, кодовых дефектов в прогонах не найдено. Правило верификатора: открытый DLG не закрывает критерий, PASS при открытом критерии невозможен. Перевод пункта в «не FAIL» по РЕШ-276 — решение для ворот (владелец или оркестратор), верификатор его сам не принимает.
