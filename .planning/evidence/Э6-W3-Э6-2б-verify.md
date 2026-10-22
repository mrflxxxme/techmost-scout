# Верификация Э6-2б «Адаптеры Wikidata и каталога YC»
Вердикт: PASS (закрыто 3 из 3 проверяемых критериев; открыт DLG-1 по контейнеру — по указанию оркестратора переходит в ворота, не FAIL)

Рабочая копия: `/home/user/techmost-scout/.claude/worktrees/e6-2b`, ветка `claude/e6-2b`, HEAD до проверки `b19b402`, дерево чистое. Интерпретатор: `/home/user/techmost-scout/.venv/bin/`. Перед pytest сняты ключевые переменные окружения.

## Критерии приёмки

| № | Критерий из PLAN.md | Команда | Факт | Статус |
|---|---|---|---|---|
| 1 | Тесты Wikidata и YC проходят: разбор с полями контракта; 500/429/обрыв/не-JSON → SearchError; две страны P17 → None; узел не ycombinator.com отброшен; второй поиск YC без второго запроса; build_adapters включает оба без ключей | `pytest tests/test_wikidata.py tests/test_yc_catalog.py -v` | 60 passed | ок |
| 2 | `test_plan_types` и `test_search_failures` проходят | `pytest tests/test_plan_types.py tests/test_search_failures.py -v` | 31 passed | ок |
| 3а | `pytest -q`: 0 failed, passed ≥ база + новые | `pytest -q` | 1636 passed, 0 failed. База: `pytest --collect-only` на `b346e10^2` = 1572; +64 новых (60 в двух файлах и 4 параметризованных кейса по реестру) = 1636 | ок |
| 3б | `ruff check .` — 0 замечаний | `ruff check .` | All checks passed! | ок |
| 3в | `ruff format --check .` | `ruff format --check .` | 241 files already formatted | ок |
| 3г | Инв. 6: httpx в `backend/app` только в `llm/`, `search/`, оркестраторе | `rg -ln httpx backend/app \| rg -v "backend/app/(llm/\|search/\|cli_doctor\.py$\|pipeline/pipeline_context\.py$\|pipeline/pipeline_search\.py$)"` | пусто (без вывода) | ок |
| 3д | Прогон в контейнере `python:3.12-slim` (РЕШ-249) | `docker info`, `docker image ls python:3.12-slim` | демон недоступен: сокета `/var/run/docker.sock` нет | DLG-1 |

Итог по критериям: 1 — ок, 2 — ок, 3 — ок в части 3а–3г, 3д открыт как DLG-1.

## Мутации критериев (сделаны верификатором, по одной, с откатом `git checkout`; после всех `git status` пуст)

| Мутация | Файл | Результат |
|---|---|---|
| M1: страна берётся первой при двух кодах P17 | wikidata.py | убита: 1 failed (`test_две_страны_p17_дают_country_none`) |
| M2: 429 гасится в пустой успех | wikidata.py | убита: 2 failed (`[429]`, `test_повтор_при_отказе_тоже_searcherror`) |
| M3: проверка узла ycombinator.com отключена | yc_catalog.py | убита: 1 failed (`test_кандидат_не_на_ycombinator_com_отброшен`) |
| M4: кэш каталога YC отключён | yc_catalog.py | убита: 2 failed (`test_второй_поиск_не_делает_второго_запроса`, `test_параллельные_поиски_качают_каталог_один_раз`) |
| M5: обрыв/нечитаемый каталог → пустой успех | yc_catalog.py | убита: 3 failed (обрыв ×2, не-JSON) |
| M6: `yc_catalog` убран из реестра адаптеров | __init__.py | убита: 1 failed (`test_build_adapters_включает_yc_catalog_без_ключей`) |

## Инварианты (способы из INVARIANTS.md; перечень задачи: №1, №5, №6, №7, №8)

- №1 ссылки на первоисточник: url Wikidata — `/wiki/Q…` (тест разбора), url YC — только узел ycombinator.com (M3 убита).
- №5 отказ — `SearchError`: M2 и M5 убиты, `test_wikidata.py`/`test_yc_catalog.py` (500, 429, обрыв, не-JSON) зелёные.
- №6 `rg` из п. 3г — пусто.
- №7 без сети: `unshare -rn` (сеть проверена: `gaierror` на query.wikidata.org) — полный `pytest -q` 1636 passed.
- №8 `backend/app/search/` изменён вместе с `tests/`: `test_wikidata.py`, `test_yc_catalog.py` (новые), `test_plan_types.py` (правка).
- №2, №4, №9, №10 к задаче не относятся напрямую; автоотправки и новых промптов в diff нет (`git diff --stat` по путям).

Инвариант №3 (секреты) в перечень задачи не входит. Буквальный `grep -rE "sk-|gigachat|Api-Key" backend/ tests/` не пуст — см. DLG-2. Совпадения в добавленных строках этой задачи отсутствуют.

## Состав diff (`git diff --stat claude/sleepy-planck-11w8jv HEAD -- tests/`)

- `tests/test_wikidata.py` (новый, +260), `tests/test_yc_catalog.py` (новый, +253) — новые тесты;
- `tests/test_plan_types.py` — 9 строк +, 2 −; названное отступление исполнителя (правило 14 `planner.md`), критерий 2 без неё не проходит.

Файлы задачи: `backend/app/search/wikidata.py` (256 строк), `backend/app/search/yc_catalog.py` (319), `backend/app/search/__init__.py` (+10). Лишних файлов в diff нет (кроме evidence прежних шагов).

## Отложено

- **DLG-1: прогон в контейнере `python:3.12-slim`** (РЕШ-249 / РЕШ-276) не выполнен. Причина: docker-клиент 29.9.1 установлен, но демон недоступен (`/var/run/docker.sock` нет). Действие владельца: запустить docker-демон и прогнать `pytest -q` и `ruff check . && ruff format --check .` в `python:3.12-slim` на копии ветки `claude/e6-2b`, либо отметить проверку выполненной вне верификатора. Переходит в ворота.
- **DLG-2: буквальный инв. 3 не пуст по всему репозиторию.** `rg -l "sk-|gigachat|Api-Key" backend/ tests/` — 10 файлов, 17 совпадений. Все совпадения есть в базе `b346e10^2` с теми же счётчиками (`backend/app/config.py`, `backend/app/llm/yandex.py`, `backend/app/search/yandex_search.py`, `backend/app/search/patents_view.py`, `tests/conftest.py`, `tests/test_config.py`, `tests/test_llm_router.py`, `tests/test_dedup_judge_probe.py`, `tests/test_rescore_probe.py`, `tests/test_plan_types.py`: строки 387, 397, 403 — базовые). В строках, добавленных этой задачей, совпадений нет. Длины токенов после префиксов короткие: у большинства до 4 символов, у двух файлов 19 символов; формат ключа DeepSeek около 35 символов, совпадение с ним не выявлено. Значения не выводились. Ревью указало «совпадения только в старых файлах вне diff» — неточно: `test_plan_types.py` в diff, но его совпадения были в базе. Действие владельца: решить, сузить ли буквальный grep (исключить легаси-совпадения и заглушки) или очистить базу; решение записать в DECISIONS.md. Этой задаче не блокирует.

## Причина FAIL

Не применимо: проваленных проверяемых критериев нет.
