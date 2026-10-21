# Верификация Э6-6а (полный цикл, ревью цикла 2)
Вердикт: FAIL (закрыто 3 из 4 критериев; критерий 1 НЕ ЗАКРЫТ)

Ветка claude/e6-6a, рабочая копия `.claude/worktrees/e6-6a`, база — origin/claude/e6-w2 (влита, предок HEAD подтверждён).
Окружение: общий `.venv`, `env -u` боевых переменных, секреты не печатались.

## Критерии приёмки (PLAN.md, «Э6-6а»)

| № | Критерий из PLAN.md | Команда | Факт | Статус |
|---|---|---|---|---|
| 1 | Роутинг по Host: публичный хост закрывает рабочие пути 404 без Authorization (не 401); `/` → 302 на /scan; /health 200; /api/v1/requests/1 без ключа 401; хост с портом и в верхнем регистре; рабочий хост /scan 404; пустая настройка | `pytest tests/test_host_routing.py -v` | 30 passed, 0 failed. Покрыто: 8 путей 404 без пароля (/requests, /requests/1/report, /requests/1/review, /requests/1/export.pdf, /demo, /showcase, /api/docs, /api/openapi.json), POST /requests; `/` → 302; /health 200 и /api/v1/requests/1 401 (тест на строках 106–107); хост с портом и в верхнем регистре (3 случая); рабочий /scan 404; пустая PUBLIC_SCAN_HOST. **Не покрыт литеральный путь `/request/1`** из критерия: в тестах его нет (и `/requests/1` без суффикса тоже нет). Пробный прогон вне pytest: `/request/1` → 404 без WWW-Authenticate, `/requests/1` → 404, `/history` → 404. Отступление оркестратора (`/history` → `/requests`) принято как объявленное, не переоценивалось. | НЕ ЗАКРЫТ |
| 2 | Лимиты: 3-й скан с IP за сутки → ip_limit, в следующие сутки UTC+3 → ok; 291 ₽ отказ, 285 ₽ допуск; 2 незавершённых резервируют 20 ₽; поддельный X-Forwarded-For не меняет IP; caplog без IP; умолчания по РЕШ-258 | `pytest tests/test_scan_limits.py -v` | 20 passed, 0 failed. Соответствие: test_третий_скан_с_адреса_за_сутки_отказ_а_в_следующие_сутки_допуск; test_потрачено_291_отказ_285_допуск; test_два_незавершённых_скана_резервируют_20_рублей; test_поддельный_forwarded_for_с_публичного_адреса_ip_не_меняет; test_адрес_не_попадает_в_лог (caplog). Умолчания — test_умолчания_настроек_скана_по_реш_258 (в test_host_routing.py), passed: 2 / 300 / 10 / "" / "" / "" / 0.48 | ок |
| 3 | Мутации, каждая ≥1 failed: (а) убрать резерв незавершённых; (б) доверять X-Forwarded-For всегда; (в) пропускать рабочие пути на публичном хосте | по одной замене, прогон `pytest tests/test_scan_limits.py tests/test_host_routing.py -q`, затем `git checkout --` и `git status` | (а) `return models + search + unfinished * …` → `return models + search`: 1 failed (test_два_незавершённых_скана_резервируют_20_рублей). (б) `if not _is_private(peer):` → `if False:`: 2 failed. (в) `if not is_public_path(path):` → `if False:`: 12 failed. Откат после каждой: 0 изменённых файлов | ок |
| 4 | `rg -ni "captcha" backend/ tests/` — пусто (РЕШ-265) | `rg -ni "captcha" backend/ tests/` | вывода нет, код возврата 1 | ок |

## Общие критерии этапа (PLAN.md, «Общие правила этапа»)

| № | Правило | Команда | Факт | Статус |
|---|---|---|---|---|
| G1 | pytest -q: 0 failed, passed ≥ база + новые | `pytest -q` (env -u) | 1557 passed, 0 failed. База 1507 (указана оркестратором) + 50 новых (30 + 20) = 1557 | ок |
| G2 | ruff check и ruff format --check | `.venv/bin/ruff check .`; `.venv/bin/ruff format --check .` | All checks passed!; 234 files already formatted | ок |
| G3 | Прогон в python:3.12-slim (РЕШ-249) | Docker в среде недоступен | не выполнен, см. DLG-1 | DLG-1 (не FAIL) |
| G4 | git diff --stat по tests/: только новые и названные файлы | `git diff --stat origin/claude/e6-w2 HEAD -- tests/` | test_host_routing.py (+218), test_scan_limits.py (+279) — новые; test_config.py (18 строк) — только 7 имён в `_AWAITING_OWNER_IN_EXAMPLE` (РЕШ-274, названное исключение) | ок |
| G5 | 500 строк в backend/app | `tests/test_backend_layout.py` и `tests/test_tests_layout.py` в полном прогоне | входят в 1557 passed; host_routing.py 84, scan_limits.py 161; тесты 218 и 279 строк | ок |
| G6 | Инв. 6: httpx только в разрешённых местах | `rg -ln httpx backend/app` с исключениями | пусто; в web/ httpx нет | ок |
| G7 | Тексты клиенту — на утверждение владельцем | просмотр diff | новых клиентских текстов нет; единственная строка ответа прослойки — `{"detail": "Not Found"}` (ответ Starlette на отсутствующий маршрут) | ок (N/A) |
| G8 | Новых зависимостей нет; сутки UTC+3 без zoneinfo | `git diff --stat … -- pyproject.toml prompts/`; grep по добавленным строкам | pyproject.toml и prompts/ не менялись; zoneinfo — только в комментарии scan_limits.py | ок |
| G9 | Коммит до handoff | `git commit` до ответа | см. хэш в финальном ответе | ок |

## Прогон без сети (инв. 7)
`unshare -rn` (сетевое пространство без loopback): `pytest -q` → 1557 passed, 0 failed.

## Инварианты
- №1, №2, №5 — к задаче не относятся напрямую (нет карточек, отчётов и LLM-отказа); нарушений в diff нет.
- №3 — `grep -rE "sk-|gigachat|Api-Key" backend/ tests/` по всему набору не пуст, но совпадения (10 файлов) те же, что в базе origin/claude/e6-w2 (10 файлов): фиктивные ключи-заглушки в тестах и заголовки `Api-Key` в адаптерах. По добавленным строкам диффа `grep` пуст. Значения ключей в отчёт не выводятся.
- №4 — `pytest tests/test_prompts.py`: 59 passed.
- №6 — см. G6.
- №7 — см. «Прогон без сети»: 1557 passed.
- №8 — backend/ (8 файлов) и tests/ (3 файла) изменены вместе.
- №9 — diff в границах списка файлов из PLAN.md; `build_app(..., public_scan_host)` — ровно заявленный контракт.
- №10 — `pytest tests/test_team_docs.py`: 35 passed.

## Отложено
- DLG-1: контейнерный прогон `python:3.12-slim` (РЕШ-249) не выполнен — Docker недоступен в среде верификации. По РЕШ-276 прогон заменён CI. Действие владельца: приложить вывод CI к evidence этапа. Тест на подменах — не заменяет контейнер.
- Замечание к основаниям: РЕШ-274 и РЕШ-276 в `.planning/DECISIONS.md` рабочей копии не найдены (grep пуст); их содержание принято по постановке оркестратора.

## Причина FAIL
Критерий 1 не закрыт. Литеральный путь `/request/1` из PLAN.md отсутствует и в параметризации `test_публичный_хост_закрывает_рабочие_пути_404_без_пароля`, и в других тестах файла `tests/test_host_routing.py`. Объявленное отступление касается только `/history` → `/requests`. Пробный запрос показывает корректное поведение (404 без WWW-Authenticate), но критерий требует прогона тестом, а не подмены соседним случаем. Закрыть: случай `/request/1` в тестах либо решение оркестратора о признании отступления для `/request/1` (тогда повторная верификация критерия 1).
