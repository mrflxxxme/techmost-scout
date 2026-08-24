# Ревью Д-4 волны Э2-З · цикл 1
Вердикт: ПРИНЯТЬ

## Проверено (чистый перенос против правки логики)
Побайтовое сравнение тел функций из базы (f41beab) с новыми файлами
(3b4a60f), исключая докстроки/импорты:

- `SECRET_CHECKS` + `_schema_drift` + `_doctor` → `cli_doctor.py`: единственные
  добавления — докстрока `_doctor` и строка `from app.cli import LLMRouter,
  console, get_settings` внутри функции (`diff /tmp/old_doctor_block.py
  /tmp/new_doctor_block.py` — только эти два хунка).
- `_run` (318 строк) → `cli_run.py`: тот же результат — добавлена докстрока и
  `from app.cli import LLMRouter, console, get_settings` внутри функции, тело
  байт в байт (`diff /tmp/old_run_block.py /tmp/new_run_block.py`).
- `_intake_stage_record`, `_persist_intake_stop`, `_normalize_owner_answer` →
  `cli_run.py`: `diff /tmp/old_helpers.py /tmp/new_helpers.py` — 0 отличий
  (кроме пустой строки).
- `_setup_logging`, `_import_showcase`, команды `doctor/run/serve/showcase` в
  `cli.py` — тело идентично; единственное изменение — `import csv as
  csv_module` поднят из тела `_import_showcase` на уровень модуля (не логика).

Единственные содержательные изменения во всех трёх файлах — импорты и приём
отложенного импорта `from app.cli import ...` внутри `_doctor`/`_run`.
Поведение не менялось.

## Отложенный импорт и цикл
`cli.py` импортирует `cli_doctor`/`cli_run` на уровне модуля (обычный,
безусловный `from app.cli_doctor import ...` / `from app.cli_run import
...`). `cli_doctor.py`/`cli_run.py`, в свою очередь, читают `console`,
`get_settings`, `LLMRouter` из `app.cli` ТОЛЬКО отложенным импортом внутри
`_doctor()`/`_run()` — на уровне модуля такого импорта нет. Цикла не
возникает: к моменту первого вызова `_doctor()`/`_run()` модуль `app.cli` уже
полностью загружен и присутствует в `sys.modules`, отложенный импорт просто
читает готовые атрибуты. Проверено запуском в самой копии:
`python -c "import app.cli as cli; cli._doctor; cli._run; cli.SECRET_CHECKS"` —
загрузка и обращения прошли без ImportError. Приём идентичен образцу
`pipeline/orchestrator.py` (упомянут в задаче и докстроках).

Приём нужен ради существующих тестов, которые патчат `monkeypatch.setattr(cli,
"LLMRouter"/"get_settings"/"console", ...)` — подтверждено грепом:
`cli._doctor()`/`cli._run()` вызываются напрямую в
`tests/test_cli_run.py`, `tests/test_cli_doctor_schema_guard.py`,
`tests/test_openalex.py`, `tests/test_pipeline_resilience.py`,
`tests/test_run_accounting.py`, `tests/test_secret_leak_guard.py`, а
`cli.INTAKE_FALLBACK_CAVEAT`/`cli.REFINE_FALLBACK_CAVEAT` читаются напрямую в
`test_cli_run.py`/`test_pipeline_resilience.py`. `STAGE_INTAKE*`,
`UNKNOWN_ANSWER`, `UNSPECIFIED_REQUIREMENT`, `SECRET_CHECKS` в тестах через
`cli.` не читаются — их присутствие в `__all__` избыточно, но не вредит и не
блокирует (совет, не находка).

## Entrypoint — прогнан в самой копии
```
$ PYTHONPATH=.../agent-a7c66d160ef00fd18/backend python -m app.cli --help
Usage: python -m app.cli [OPTIONS] COMMAND [ARGS]...
...
doctor    Проверить ключи, модели и источники. Ничего не меняет.
run       Прогнать запрос через пайплайн и сохранить отчёт в markdown.
serve     Запустить веб-интерфейс.
showcase  Загрузить витрину разработок партнёров из CSV.
```
`import app.cli as cli` в python -c из копии — `cli._doctor`, `cli._run`,
`cli.SECRET_CHECKS` (8 записей), `cli.STAGE_INTAKE` доступны без ошибок.
Именно код копии, не основного дерева (PYTHONPATH указывает на
`backend/` внутри worktree агента).

## Страж и границы задачи
- `tests/test_backend_layout.py`: запись `"cli.py": 726` убрана из
  `_EXCEPTIONS`; строки `"search/openalex.py": 593` и
  `"search/yandex_search.py": 558` НЕ тронуты (значения совпадают с базой) —
  конфликта с параллельной Д-5 не будет. Комментарий у обеих переписан на
  «дробление — предмет отдельной задачи Д-5 той же волны, после которой
  список опустеет» — корректно отражает план.
- `git show 3b4a60f --name-status`: только `backend/app/cli.py` (M),
  `backend/app/cli_doctor.py` (A), `backend/app/cli_run.py` (A),
  `tests/test_backend_layout.py` (M). Ровно то, что описано в задаче
  Д-4 PLAN.md, ничего лишнего.

## Полный прогон и прицельные тесты (в копии, не на слово)
```
$ python -m pytest -q                                          → 787 passed, 1 warning (deprecation FastAPI/httpx, не наше)
$ python -m pytest -q tests/test_backend_layout.py tests/test_cli_run.py \
    tests/test_cli_doctor_schema_guard.py tests/test_prompts.py → 74 passed
$ python -m pytest -q tests/test_cli_run.py                     → 22 passed
$ ruff check .                                                  → All checks passed!
$ ruff format --check .                                         → 142 files already formatted
$ wc -l backend/app/cli.py backend/app/cli_doctor.py backend/app/cli_run.py
  176 / 184 / 487 — все ≤ 500
```
Числа исполнителя (787, 74, 176/184/487, ruff чисто) воспроизведены лично,
совпадают.

## Мутации
Карточкой не заданы (по инструкции цикла). Замена — полный прогон плюс
прицельный набор тестов, патчащих `cli.*` (см. выше); оба зелёные по факту
переноса без изменения тела функций (см. раздел «чистый перенос» выше) —
основания подозревать «зелено по неверной причине» нет: переносимый код
буквально идентичен байт в байт, кроме экспортов.

## Инварианты (все десять)
1. Факт без ссылки — не применимо: задача не трогает карточки/evidence.
2. Автоотправка клиенту — не применимо: CLI-рефакторинг, ветки валидации не
   тронуты.
3. Секреты: `grep -rE "sk-|gigachat|Api-Key" backend/ tests/` — только
   легитимные упоминания (заголовки авторизации, `sk-test`/`sk-fake-*` в
   тестах и conftest, существовавшие до задачи); значения секретов нигде не
   печатаются; `.env`/`.env.*` в `.gitignore` (проверено).
4. `render_prompt`/`{{...}}` — не применимо: промпты не тронуты.
   `pytest tests/test_prompts.py` зелёный (входит в прицельные 74).
5. Отказ источника — не применимо: адаптеры не тронуты, `_doctor` и раньше, и
   сейчас гасит отказы health-проверки в статус строки таблицы, поведение не
   изменилось (перенесено байт в байт).
6. `grep -rln "httpx" backend/app --include='*.py' | grep -vE
   "backend/app/(llm/|search/|cli\.py$|pipeline/pipeline_context\.py$|
   pipeline/pipeline_search\.py$)"` теперь выдаёт `backend/app/cli_doctor.py`
   — httpx-клиент доктора переехал туда вместе с `_doctor`. Это ожидаемо и не
   нарушение по смыслу (тот же клиент, то же назначение — проверка
   источников), но пример-паттерн в `docs/team/INVARIANTS.md` (пункт 6)
   формально устарел: он перечисляет `cli.py`, а не `cli_doctor.py`. Правка
   `INVARIANTS.md` не входит в файлы задачи Д-4 — исполнитель корректно не
   тронул её и написал об этом в notes handoff (см. ниже, раздел «Замечание
   исполнителя»).
7. Сеть в тестах: `pytest -q` зелёный без сети (respx/заглушки, не менялись).
8. Backend без теста: изменения в `backend/` сопровождены изменением
   `tests/test_backend_layout.py` (страж структуры) — это именно тот тест,
   который обязан отреагировать на дробление файла.
9. Выход за границы MVP — не применимо, чисто структурный рефакторинг.
10. YAML с двоеточием: `pytest tests/test_team_docs.py` — 35 passed.

## Замечание исполнителя (не блокирует, передаю дальше)
Исполнитель обнаружил и честно не стал чинить: пример grep-паттерна в пункте 6
`docs/team/INVARIANTS.md` перечисляет легитимные места httpx (`llm/`,
`search/`, `cli.py`, `pipeline_context.py`, `pipeline_search.py`) — после
переноса httpx-клиента доктора в `cli_doctor.py` перечисление устарело.
Подтверждаю фактически: буквальный прогон команды из инварианта №6 теперь
называет `cli_doctor.py`. Это не нарушение (клиент легитимен, назначение
прежнее — проверка источников), а расхождение текста документа с кодом.
Правка `docs/team/INVARIANTS.md` не входит в список файлов Д-4 (только
`cli.py`/`cli_doctor.py`/`cli_run.py`/`tests/test_backend_layout.py`) —
адресуется задаче Д-7 той же волны («документы под слитый код», волна 2),
куда и передаю. Блокирующей находкой не считаю: сам инвариант по смыслу
соблюдён, устарела только иллюстрация в тексте.

## Советы (не блокируют)
1. `__all__` в `cli.py` перечисляет `STAGE_INTAKE`, `STAGE_INTAKE_REFINE`,
   `UNKNOWN_ANSWER`, `UNSPECIFIED_REQUIREMENT`, `SECRET_CHECKS` — ни одно имя
   тесты через `cli.` не читают (грепом подтверждено отсутствие
   `cli.SECRET_CHECKS`/`cli.STAGE_INTAKE`/`cli.UNKNOWN_ANSWER`/
   `cli.UNSPECIFIED_REQUIREMENT` в `tests/`). Оставлено с запасом ради
   единообразия реэкспорта — не вредит, ruff не ругается (`__all__`
   засчитывает использование).
2. Комментарий над `__all__` в `cli.py» немного длиннее, чем нужно для сути;
   не критично.

## Handoff исполнителя — проверен
Артефакты (`backend/app/cli.py`, `cli_doctor.py`, `cli_run.py`,
`tests/test_backend_layout.py`) существуют и соответствуют diff. `checks`
содержат фактический вывод команд, все числа воспроизведены самостоятельно и
совпали. `self_audit: pass` + `escalate: none` — непротиворечиво. `notes`
содержит честную запись про исходное расхождение копии с базой волны
(git reset --hard на чистом дереве, РЕШ-158) и про находку в INVARIANTS.md —
обе годны к передаче дальше без переработки.

## Проверено (сводка команд)
- `pytest -q` (полный): 787 passed, 1 warning
- `pytest -q tests/test_backend_layout.py tests/test_cli_run.py tests/test_cli_doctor_schema_guard.py tests/test_prompts.py`: 74 passed
- `pytest -q tests/test_cli_run.py`: 22 passed
- `pytest -q tests/test_team_docs.py`: 35 passed
- `ruff check .`: All checks passed!
- `ruff format --check .`: 142 files already formatted
- `wc -l backend/app/cli.py backend/app/cli_doctor.py backend/app/cli_run.py`: 176 / 184 / 487
- `python -m app.cli --help` (в копии, PYTHONPATH на её `backend/`): команды доступны
- `git show 3b4a60f --name-status`: 4 файла, все из списка задачи Д-4
- `grep -rE "sk-|gigachat|Api-Key" backend/ tests/`: только легитимные существовавшие совпадения
- Инварианты 1-10: соблюдены; №6 — расхождение примера-иллюстрации в
  `INVARIANTS.md` с кодом, не нарушение сути, адресовано Д-7
