# Верификация Э6-4 · «Секция „Что делают в мире“ во всех выходах» (лёгкий цикл)
Вердикт: FAIL (не закрыт 1 критерий: общее правило 4, литеральная проверка против базы e6-w2)

Ветка claude/e6-4, HEAD 78da333. Окружение: Linux, Python 3.12.3, pytest 9.1.1, ruff 0.16.4 (общий .venv),
боевые переменные сняты. Импорт `app` проверен: из рабочей копии грузится `.claude/worktrees/e6-4/backend/app`.

| № | Критерий из PLAN.md / общих правил | Команда | Факт | Статус |
|---|---|---|---|---|
| 1 | Кр. 1: тесты задачи проходят, фикстура CN/US/TW/бессылочный, gov и business, пять выходов | `pytest tests/test_export_analogs.py -v` | 10 passed (фикстура и утверждения прочитаны: gov — CN в секции, US одна строка сноски, TW и бессылочный нигде; business — три со ссылками) | ок |
| 2 | Кр. 2: мутация наложения `briefs` в `build_export_view` — ≥1 failed | M1: убрать `_with_briefs_fields` из `view.py` → `pytest tests/test_export_analogs.py -q` | 4 failed, 6 passed | ок |
| 2 | Кр. 2: печать `analog_cards` вместо `analog_sections` — в каждом выходе, ≥1 failed | M2a JSON / M2b markdown / M2c печать / M2d DOCX / M2e страница (по одному выходу, копия из `git archive`) | 4 failed в каждом из пяти | ок |
| 2 | Кр. 2: всё в `main` — ≥1 failed | M3a: ветка сноски off / M3b: строго всё в `main` (сноска и исключение off) | 5 failed, 5 passed в обоих | ок |
| 2 | Контроль мутаций: без подстановок | M0 (копия без изменений) | 10 passed | ок |
| 3 | Кр. 3: `rg -n "country_status" backend/app/export` — только `export/analogs.py` | `rg -n "country_status" backend/app/export` | 3 строки: analogs.py:8, :24, :76 | ок |
| 4 | Кр. 4: `tests/test_backend_layout.py` проходит (`markdown.py` ≤ 500) | `pytest tests/test_backend_layout.py -q` | 3 passed; markdown.py 449, view.py 365, analogs.py 140 | ок |
| 5a | Кр. 5: JSON прежних отчётов несёт пустые `main`/`footnotes`, `schema_version` 1.0 | тест `test_старый_отчёт_без_аналогов_секции_не_имеет` (в файле Кр. 1) | passed; `{"main": [], "footnotes": []}`, `"1.0"` | ок |
| 5b | Общее правило 4: `git diff --stat claude/e6-wN -- tests/` — только новые файлы и названные | `git diff --stat origin/claude/e6-w2 HEAD -- tests/` | 7 файлов: `test_export_analogs.py` (+302, новый) и шесть файлов, которых в ветке нет из-за отставания: `test_analog_flow.py` −450, `test_analog_stage.py` −365, `test_plan_types.py` −34, `test_brief_ecosystem_fields.py`, `test_cli_run.py`, `test_prompts.py` | НЕ ЗАКРЫТ |
| 5b | Справочно: относительно собственной базы ветки 776b9c1 | `git diff --stat 776b9c1 HEAD -- tests/` | только `tests/test_export_analogs.py` (+302) | справочно |
| G3 | Общее правило 3: `pytest -q` — 0 failed; passed ≥ база + новые | `pytest -q` (полный, Linux) | HEAD: 1466 passed, 0 failed (78.72 с). Собственная база 776b9c1: 1456 passed → 1456 + 10 = 1466. Интеграция e6-w2 + diff задачи (патч применён чисто): 1517 passed = 1507 + 10 | ок |
| G3 | Общее правило 3: ruff из .venv | `ruff check --no-cache .` / `ruff format --no-cache --check .` | All checks passed! / 227 files already formatted | ок |
| G3 | Общее правило 3: прогон в `python:3.12-slim` | `which docker; docker info` | демона Docker нет | отступление — DLG-1 (по РЕШ-276 заменён CI) |
| G5 | Общее правило 5: 500 строк в `backend/app` | `pytest tests/test_backend_layout.py -q` | 3 passed | ок |
| G6 | Общее правило 6: httpx только в разрешённых местах | `rg -ln httpx backend/app \| rg -v "backend/app/(llm/\|search/\|cli_doctor\.py$\|pipeline/pipeline_context\.py$\|pipeline/pipeline_search\.py$)"` | пусто | ок |
| G9 | Общее правило 9: handoff и вывод проверок закоммичены до handoff | `git show --stat 5eaffdf` | `Э6-W2-Э6-4-handoff.yaml` + `Э6-W2-Э6-4-checks.txt` одним коммитом; имена по схеме волны e6-w2 | ок |

## Инварианты
№1 аналог без `evidence` не печатается ни в одном выходе (тест «разбор»; сноска берёт `evidence[0].url`, analogs.py:80) · №2 замок черновика
не изменён: `ReportNotValidatedError` в `export/json.py` 6 вхождений в базе и в HEAD, `tests/test_export_json.py` зелёный в полном прогоне ·
№3 `grep -rlE "sk-|gigachat|Api-Key" backend/ tests/` литерально не пуст (10 файлов), но набор файлов совпадает с базой 776b9c1 (diff пуст),
ключеподобных литералов `sk-[A-Za-z0-9_-]{20,}` в `backend/`, `tests/` — 0; новых вхождений задача не добавила · №4 `prompts/` не изменены,
`test_prompts.py` в полном прогоне зелёный · №5 адаптеры не затронуты · №6 см. G6 · №7 полный `pytest -q` при заблокированной сети
(`unshare -rn`, проверка сокета: OSError): 1466 passed · №8 `backend/` изменён вместе с `tests/test_export_analogs.py` · №9 diff против 776b9c1:
15 файлов, все в скоупе задачи и её evidence · №10 `tests/test_team_docs.py` в полном прогоне зелёный.

## Отложено
- DLG-1: прогон в контейнере `python:3.12-slim` не выполнен — демон Docker недоступен. Заменён CI GitHub по РЕШ-276.
  Действие владельца: подтвердить зелёный прогон CI на ветке claude/e6-4 до ворот этапа. Тесты при этом прогнаны локально (Linux, см. G3).

## Причина FAIL
Общее правило 4 не закрыто литеральной проверкой. Ветка claude/e6-4 создана от 776b9c1 и не содержит восьми коммитов Э6-2а,
которые уже в origin/claude/e6-w2 (fa3a5b4 … 3a92d8b). Поэтому `git diff --stat origin/claude/e6-w2 HEAD -- tests/` показывает
удаления шести тестовых файлов, которых эта задача не трогала. Задача изменила только `tests/test_export_analogs.py` (новый файл),
что подтверждается сравнением с собственной базой ветки.

Что закрывает пункт: слияние origin/claude/e6-w2 в claude/e6-4 (конфликтов нет: `git apply --check` патча задачи на e6-w2 прошёл;
интеграционный прогон даёт 1517 passed), затем повторная литеральная проверка `git diff --stat origin/claude/e6-w2 HEAD -- tests/`
(ожидается один новый файл). Слияние выполняет оркестратор (§11); верификатор его не выполнял, ветку не менял.

## Вне перечня проверок (вердикт не меняют)
- Общее правило 1: строка ADR в `.planning/DECISIONS.md` с меткой Э6-4 не найдена (`grep "Э6-4"` пусто).
- Общее правило 7 (тексты клиенту на утверждение владельцу) и советы ревью 1 не проверялись: вне перечня верификации.

## Логи
Полные выводы: `/tmp/claude-0/-home-user/0e99962e-75a4-55a9-9d23-923e69c2aaa2/scratchpad/full-pytest.txt` (HEAD),
`.../scratchpad/base-full.txt` (776b9c1), `.../scratchpad/integ-full.txt` (e6-w2 + патч задачи), `.../scratchpad/mut/` (копии мутаций).
