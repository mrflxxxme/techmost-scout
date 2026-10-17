# Верификация Э6-1 · волна W1

Записано оркестратором по тексту отчёта верификатора: верификатор вернул
отчёт текстом и не закоммитил (отступление от §13а). Сокращено только в
оформлении, вердикт и факты — как у верификатора. Приёмка находки М4 ревью
цикла 2 — здесь, по РЕШ-270 (третьего цикла ревью нет; отступление от §4).

Вердикт: **PASS** (5 из 5 критериев, мутации М1–М4 убиты, общие критерии и инварианты в порядке; DLG нет).

HEAD `1f14404` (origin/claude/e6-1), `git status` чист до и после, мутации откатаны.
Ловушка среды: `import app` по умолчанию шёл в дерево `jolly-wilbur-e2b13b` (editable);
путь чужого `backend` убран из `sys.path`, `app.__file__` — копия верификатора.

| № | Критерий (PLAN.md, «Э6-1») | Факт | Статус |
|---|---|---|---|
| 1 | `pytest tests/test_solution_kind.py tests/test_schemas_e6.py -v` с покрытием утверждений | 37 passed. Нет `solution_kind` в `model_dump_json`; `resolve_solution_kind` на 5 входах; круг save→load на 4 видах; база без колонки → `research`; старый JSON → `analog_cards == []`; `country` отвергает "us", "USA", "" (и ещё 3) | ок |
| 2 | `pytest tests/test_prompts.py -v`; плейсхолдеры intake; версия = CHANGELOG | 54 passed; `test_prompts.py:26` — `{RAW_TASK_TEXT, INDUSTRY_HINT, SOLUTION_KIND}`; `intake.md` 1.4 = `CHANGELOG` 1.4 | ок |
| 3 | Мутация «resolve всегда отдаёт значение модели» → ≥1 failed | 8 failed, 11 passed; откат | ок |
| 4 | `rg solution_kind` в planner/card_author/verifier/summary.md — пусто | пусто | ок |
| 5 | Правило 4 | `M tests/test_prompts.py` (1 строка); новые `test_schemas_e6.py`, `test_solution_kind.py`, `test_solution_kind_web.py` | ок |

## Мутации находок ревью (`pytest tests/test_solution_kind_web.py -q`)

| Мутация | Что удалено | Факт |
|---|---|---|
| М1 | `fields["solution_kind"] = …` в `_confirm_brief` | 1 failed — убита |
| М2 | `solution_kind=kind` в `create_request` | 6 failed — убита |
| М3 | скрытое поле в `_deidentification_warning.html` | 1 failed — убита |
| М4 | `solution_kind=draft.solution_kind` в `_draft_brief_from_answers` (РЕШ-270) | 1 failed — убита |

## Общие критерии

| Проверка | Факт | Статус |
|---|---|---|
| `pytest -q --deselect tests/test_docker_compose_backup_runtime.py` | 1416 passed, 9 deselected, 0 failed | ок |
| Контейнер `python:3.12-slim`, `git archive HEAD`, `.[dev,export]` | 1416 passed, exit 0 | ок |
| Без сети (заглушка сокетов в процессе) | 1416 passed | ок |
| `ruff check .`, `ruff format --check .` | All checks passed; 223 files already formatted | ок |
| `tests/test_backend_layout.py` | зелёный | ок |

## Инварианты

№3 — литеральных ключей нет, новые тесты в совпадениях не участвуют; №4 — три вызова
`render_prompt("intake")` заполняют `SOLUTION_KIND` (`intake.py:71`, `:105`,
`constraints_guard.py:177–181`); №6 — httpx только в разрешённой карте; №7 — без сети
зелёно; №8 — `backend/` и `tests/` вместе; №9 — на форме нет «Гос», есть выбор вида.

**Замечание (не влияет):** `_deidentification_warning.html` вне списка файлов задачи в
PLAN.md — заявлен в handoff, принят ревью цикла 1, необходимость подтверждена М3.
