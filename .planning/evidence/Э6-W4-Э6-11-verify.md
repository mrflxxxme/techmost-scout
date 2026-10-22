# Верификация Э6-11 · белый список имён ПО в обезличивании (РЕШ-291)
Вердикт: PASS (закрыто 4 из 4 критериев РЕШ-291; открыт DLG-1 вне критериев)

Рабочая копия: /home/user/techmost-scout/.claude/worktrees/e6-11, ветка claude/e6-11, HEAD до отчёта 3384ac9 (ревью).
База 97d6219. Код задачи: 954ac1e. Окружение: /home/user/techmost-scout/.venv, `env -u` из регламента, без значений секретов в выводе.

## Критерии приёмки (РЕШ-291, пункт «Критерии (1)–(4)»)

| № | Критерий | Команда | Факт | Статус |
|---|---|---|---|---|
| 1 | Четыре имени ревью (TensorFlow, PyTorch, MatLab, SolidWorks) → рисков нет; AcmeCorp и ООО «Ромашка» → риск; TensorFlowLabs → риск | `pytest tests/test_anonymize.py -v` | 26 passed. Параметризованные `test_software_names_from_whitelist_are_not_risks[TensorFlow|PyTorch|MatLab|SolidWorks]` — passed; `test_acmecorp_and_legal_entity_remain_risks_next_to_software` — passed (AcmeCorp и ООО «Ромашка» в одном тексте); `test_software_name_inside_longer_word_is_still_a_risk` (TensorFlowLabs) — passed | ок |
| 2a | Мутация «пропускать любой CamelCase» → ≥1 failed | `if pattern is _CAMEL_CASE and match.group(0).lower() in software:` заменено на `if pattern is _CAMEL_CASE:`; `pytest tests/test_anonymize.py -q` | 5 failed, 21 passed (AcmeCorp ×2, test_multiple_distinct_entities…, test_acmecorp_and_legal_entity…, test_software_name_inside_longer_word…, test_whitelist_is_read_from_yaml). Откат `git checkout`, затем 26 passed | ок |
| 2b | Мутация «белый список не читается» → ≥1 failed | `software = load_software_names()` заменено на `software = frozenset()`; `pytest tests/test_anonymize.py -q` | 6 failed, 20 passed (4 имени ревью, test_acmecorp_and_legal_entity…, test_whitelist_is_read_from_yaml). Откат, затем 26 passed; `git status` чист | ок |
| 3a | Полный набор 0 failed | `pytest -q` (env -u) | 1986 passed, 1 warning in 119.74s (warning starlette, к задаче не относится) | ок |
| 3b | Ruff чист | `ruff check .`; `ruff format --check .` | All checks passed!; 257 files already formatted | ок |
| 3c | `anonymize.py` ≤ 500 строк | `wc -l backend/app/anonymize.py` | 89 | ок |
| 4 | Прежние тесты без правок | `git diff 97d6219 HEAD -- tests/` | Удалённых строк 0 (`grep -c '^-[^-]'` → 0); добавлено 45 строк в конец tests/test_anonymize.py | ок |

Дополнительно: `pytest tests/test_anonymize.py tests/test_team_docs.py` под `unshare -rn` (сеть отключена): 61 passed.

## Инварианты

- №3 секретов: `grep -rEl "sk-|gigachat|Api-Key" backend/ tests/` не пуст, но совпадения идентичны базе 97d6219 (те же 10 файлов, те же счётчики строк; проверено сравнением `git grep -c`). В diff задачи совпадений нет (`git diff 97d6219 HEAD -- backend/ tests/ | grep -E "^\+.*(sk-|gigachat|Api-Key)"` пусто). Пункт за пределами Э6-11, отмечен оркестратору.
- №6 httpx: в diff задачи httpx не добавлен. Вхождения `httpx` вне `llm/`, `search/`, оркестратора (cli_doctor.py, pipeline/pipeline_search.py, pipeline/pipeline_context.py) есть в базе и задачей не затронуты.
- №7 pytest без сети: подмножество (anonymize + team_docs) под `unshare -rn` — 61 passed; полный набор без сети не прогонялся.
- №8 backend/ и tests/ изменены вместе: backend/app/anonymize.py, backend/app/reference/software_names.yaml и tests/test_anonymize.py — да.
- №10 YAML: `yaml.safe_load` в backend/app/anonymize.py:55; в значениях software_names.yaml двоеточий 0; `safe_load` разбирает файл: 49 имён, 49 уникальных без учёта регистра.
- Остальные пункты инвариантов отдельно не проверялись; в diff задачи три продуктовых файла (anonymize.py, software_names.yaml, tests/test_anonymize.py) и отчёты evidence.
- Артефакты handoff исполнителя существуют (anonymize.py, software_names.yaml, test_anonymize.py, checks.txt). Лишних тронутых файлов в рабочей копии нет (`git status` пуст до коммита отчёта).

## Отложено

- DLG-1: контейнерный прогон `python:3.12-slim` (РЕШ-276, п. по Э6-6в) не выполнен: демона Docker в среде нет (`docker info` → код возврата 1). Пункт не входит в критерии 1–4 РЕШ-291, на вердикт не влияет. Действие владельца: закрыть прогоном CI GitHub на ветке интеграции после слияния (Ubuntu, Python 3.12); ссылка на прогон — в воротах.
- Состав списка (49 имён) передан владельцу на утверждение (handoff исполнителя, notes); это не критерий приёмки.

## Причина FAIL

Не применимо: критерии 1–4 закрыты.
