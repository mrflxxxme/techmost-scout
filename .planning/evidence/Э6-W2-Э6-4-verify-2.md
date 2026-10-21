# Верификация Э6-4 (повторная, свежий верификатор) · «Секция „Что делают в мире“ во всех выходах»

Вердикт: PASS (закрыто все проверяемые критерии задачи и общие правила 3–6; правило 4 закрыто слиянием dd93e59; DLG-1 и DLG-2 открыты и переходят в ворота, на вердикт не влияют)

Предыдущая верификация `Э6-W2-Э6-4-verify.md` (FAIL, правило 4) — закрыта: литеральная проверка против origin/claude/e6-w2 даёт один новый файл.

Окружение: ветка claude/e6-4, HEAD dd93e59, рабочая копия чистая. Python 3.12.3, pytest и ruff из общего `/home/user/techmost-scout/.venv`. Боевые переменные сняты (список из задания). Импорт `app` при pytest: `pyproject.toml` задаёт `pythonpath = ["backend"]` относительно rootdir рабочей копии; проверено: импортируется `.claude/worktrees/e6-4/backend/app/__init__.py`.

| № | Критерий | Команда | Факт | Статус |
|---|---|---|---|---|
| R4 | Правило 4: только новые файлы в tests/ относительно e6-w2 | `git diff --stat origin/claude/e6-w2 HEAD -- tests/` и `--name-status` | 1 файл: `tests/test_export_analogs.py | 302 +++` (статус A). Остальные шесть файлов прежней проверки не различаются | ок (закрыто) |
| R3a | Общее правило 3: `pytest -q` полный, 0 failed, passed ≥ 1507 + 10 | `env -u DEEPSEEK_API_KEY ... -u YANDEX_MODEL -u DEEPSEEK_BASE_URL -u YANDEX_CATALOG_ID .venv/bin/pytest -q` | 1517 passed, 1 warning, 0 failed (77.86 с). Лог: `scratchpad/e6-4-verify-2-pytest-full.txt` | ок |
| R3b | Общее правило 3: ruff check | `.venv/bin/ruff check --no-cache .` | All checks passed! | ок |
| R3c | Общее правило 3: ruff format | `.venv/bin/ruff format --no-cache --check .` | 232 files already formatted | ок |
| R3d | Общее правило 3: прогон в `python:3.12-slim` | `which docker`; `docker info` | CLI есть, демон не запущен (`docker info` не отвечает) | DLG-1 |
| 1 | Кр. 1: `pytest tests/test_export_analogs.py -v` | `.venv/bin/pytest tests/test_export_analogs.py -v` | 10 passed (gov: CN в секции, US сноской, TW и бессылочный нигде; business: три со ссылками; старый отчёт без секции → `{"main": [], "footnotes": []}`, `"1.0"`). Лог: `scratchpad/e6-4-verify-2-crit1.txt` | ок |
| 2 | Кр. 2: мутация M1 — убрать наложение полей `briefs` в `build_export_view` → ≥1 failed | Копия `git archive dd93e59` в `scratchpad/mut-m1`, строка 164 `view.py` заменена на `pass`; `pytest tests/test_export_analogs.py -q` в копии (импорт из копии подтверждён) | 4 failed, 6 passed: `test_гос_cn_в_секции_us_сноской_tw_и_бессылочный_нигде`, `test_гос_сноска_одной_строкой_название_страна_ссылка`, `test_гос_полная_карточка_только_у_cn`, `test_режим_берётся_из_briefs_а_не_из_умолчания_report_json`. Лог: `scratchpad/e6-4-verify-2-mutM1.txt` | ок (мутация убита) |
| 3 | Кр. 3: `rg -n "country_status" backend/app/export` — только `export/analogs.py` | `rg -n "country_status" backend/app/export` | 3 строки, все в `backend/app/export/analogs.py` (:8 докстринг, :24 импорт, :76 вызов) | ок |
| 4 | Кр. 4 / общее правило 5: `pytest tests/test_backend_layout.py -q` | `.venv/bin/pytest tests/test_backend_layout.py -q` | 3 passed | ок |
| 6 | Общее правило 6 (инв. 6): rg-проверка httpx из PLAN.md | `rg -ln httpx backend/app \| rg -v "backend/app/(llm/\|search/\|cli_doctor\.py$\|pipeline/pipeline_context\.py$\|pipeline/pipeline_search\.py$)"` | пусто (код выхода rg 1, совпадений нет) | ок |
| 7 | Инв. 7: полный pytest при выключенной сети | `unshare -rn env -u ... pytest -q` | 1517 passed, 1 warning. Лог: `scratchpad/e6-4-verify-2-pytest-nonet.txt` | ок |

Контроль мутации: копия `git archive dd93e59` без правок проходит тот же файл (10 passed по прогону рабочей копии, идентичной HEAD).

## Инварианты

№3: `git grep -nE "sk-[A-Za-z0-9_-]{20,}" -- backend tests` в отслеживаемых файлах — 0 совпадений. Широкая проверка `sk-|gigachat|Api-Key` по отслеживаемым файлам — 10 файлов, столько же, сколько в базе 776b9c1; новых файлов нет. Два зонда с литералом короче порога регулярки — см. DLG-2. Значения секретов в отчёт не выводились.
№6 — см. R6 · №7 — см. строку 7 (сеть отключена, 1517 passed) · №8 — `git diff --stat origin/claude/e6-w2 HEAD -- backend tests`: 11 файлов, изменения `backend/` сопровождаются `tests/test_export_analogs.py` (+302) · №1, №2 — прошлая верификация; полный прогон (включая `test_export_json.py`) зелёный, в diff задачи `export/json.py` замок черновика не трогается.

## Отложено

- DLG-1: прогон в контейнере `python:3.12-slim` невозможен: CLI Docker есть, демона нет. По РЕШ-276 (`.planning/DECISIONS.md:4869`) и правилу 3 плана прогон заменён CI GitHub. Локально: Linux, Python 3.12.3, 1517 passed, 0 failed. Действие владельца: подтвердить зелёный CI на ветке задачи или сессионной ветке до ворот Э6. Прецедент волны: Э6-2а (`Э6-W2-Э6-2а-verify.md`) с тем же DLG-1 получил PASS.
- DLG-2: в файлах `tests/test_dedup_judge_probe.py` (строки 230, 242) и `tests/test_rescore_probe.py` (строки 383, 397) стоит литерал вида `sk-` + 19 символов в кавычках. Регулярка инварианта №3 (`{20,}`) его не ловит. Файлы отслеживаются и введены в базе 776b9c1 (коммиты 0a9609b, 9ced099); в diff Э6-4 и волны не входят (`git diff --stat 776b9c1 HEAD` по ним пусто). Значение не выводилось: по классификации 9 различных символов, одна цифра. Кэш `tests/__pycache__/*.pyc` (игнорируется git, не отслеживается) содержит ту же строку. Действие владельца: подтвердить, что литерал — не секрет (фикстура); если секрет — ротация ключа и удаление из истории. Инвариант №3 блокирует нарушение, поэтому оркестратор решает, нужна ли правка до ворот; вердикт задачи Э6-4 не меняется, так как нарушение не введено задачей.

## Вне перечня проверок (вердикт не меняют)

- Строка ADR с меткой Э6-4 в `.planning/DECISIONS.md` не найдена: `grep -c "Э6-4" .planning/DECISIONS.md` → 0. Это процессное требование лёгкого цикла («Пределы циклов» плана), не критерий приёмки 1–5 задачи и не общее правило 3–6. Оркестратор решает, нужна ли запись до ворот.
- Общее правило 7 (тексты клиенту на утверждение владельцу) и советы ревью не проверялись: вне перечня верификации.

## Логи (не в репозитории)

Каталог: `/tmp/claude-0/-home-user/0e99962e-75a4-55a9-9d23-923e69c2aaa2/scratchpad/`
- `e6-4-verify-2-pytest-full.txt` (полный прогон), `e6-4-verify-2-pytest-nonet.txt` (без сети),
- `e6-4-verify-2-crit1.txt` (критерий 1), `e6-4-verify-2-mutM1.txt` (мутация M1), `mut-m1/` (копия для мутации).

## Причина FAIL

Нет. Вердикт PASS.
