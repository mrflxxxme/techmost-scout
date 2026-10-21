# Верификация Э6-8 · реестры РФ как источники аналогов
Вердикт: PASS (закрыто 6 из 6 критериев задачи; общих проверок 5 из 5; мутаций 3 из 3 убиты, четвёртая не применима — адаптера нет)
Рабочая копия: claude/e6-8, HEAD 15e4ed4 перед коммитом отчёта. База: 1274ad7 (`1a48bbc^2`, РЕШ-284 — Э6-7 влита). Окружение: Python 3.12.3, общий `.venv`, сняты переменные ключей и маршрутов по постановке. Сырой вывод — в `/tmp/claude-0/-home-user/0e99962e-75a4-55a9-9d23-923e69c2aaa2/scratchpad/` (`v-crit2.txt`, `full-pytest.txt`, `nonet-pytest.txt`, `crit6.txt`, `inv10.txt`, `mut-a.txt`, `mut-b.txt`, `mut-c.txt`).

## Критерии приёмки из PLAN.md (раздел «### Э6-8»)

| № | Критерий из PLAN.md | Команда | Факт | Статус |
|---|---|---|---|---|
| 1 | Проба есть; ≥ 3 упоминания доменов реестров | `rg -c "gisp\.gov\.ru\|reestr\.digital\.gov\.ru\|productradar\.ru" .planning/evidence/Э6-W4-Э6-8-probe.txt` | 9 (порог ≥ 3); файл 5636 байт; раздел «Решение по пробе» — строка 54 | ок |
| 2 | `tests/test_ru_registries.py` проходит, покрыты все подпункты критерия | `pytest tests/test_ru_registries.py -v` | 31 passed, 0 failed (см. соответствие ниже) | ок |
| 3 | Адаптер Product Radar не написан: `rg` пусто, причина в пробе | `rg -ln "productradar" backend/app --glob "*.py"`; `ls backend/app/search/product_radar.py tests/test_product_radar.py` | `rg` пусто (rc=1); оба файла отсутствуют; причина — probe.txt, раздел «Решение по пробе» | ок (ветка «не написан») |
| 4 | Домены реестров не появляются в коде | `rg -ln "gisp\.gov\.ru\|reestr\.digital\.gov\.ru" backend/app --glob "*.py"` | пусто (rc=1); домены только в `analog_sources.yaml` | ок |
| 5 | Мутации: каждая ≥ 1 failed в `pytest tests/test_ru_registries.py -q` | см. таблицу мутаций ниже | 6 failed; 5 failed; 2 failed; мутация адаптера не применима | ок |
| 6 | Прицельный набор зелёный; `git diff --stat 1274ad7 -- tests/` — только новые файлы и названные правки; инв. 6 — пусто | `pytest tests/test_yandex_analog.py tests/test_search_yandex.py tests/test_yandex_limit_visibility.py tests/test_analog_stage.py tests/test_analog_flow.py tests/test_backend_layout.py -q`; `git diff --stat 1274ad7 -- tests/` | 123 passed; в tests/ 4 файла: `test_ru_registries.py` (новый, +403), `test_analog_flow.py` (5 строк), `test_analog_stage.py` (9), `test_yandex_analog.py` (9) — все три правки названы в плане | ок |

### Соответствие подпунктов критерия 2 тестам (`-v`)
- `ru_registries` — ровно три домена РЕШ-261, каждый покрыт: `test_ru_registries_ровно_три_домена_решения_261`, `test_каждый_реестр_решения_261_покрыт_списком_и_ни_один_не_в_мировом`.
- Домен со схемой, путём, пробелом, прописными, числом — ошибка загрузки: `test_домен_реестра_со_схемой_путём_или_пробелом_это_ошибка_загрузки` (5 вариантов); дубль — `test_дубль_домена_реестра_это_ошибка_загрузки`; домен мирового списка — `test_реестр_с_доменом_мирового_списка_это_ошибка_загрузки`.
- Русская `analog` несёт `site:` только реестров: `test_русская_analog_несёт_site_только_реестров`; английская — ни одного домена реестра: `test_английская_analog_не_несёт_ни_одного_домена_реестра`.
- `registry_analog_query`: «Продукт» (product / digital / business_model) — одна русская `analog` с текстом русской `web`: `test_рынок_даёт_одну_русскую_analog_с_текстом_русской_web` (3 варианта); без русской `web` — из `keywords_ru`: `test_без_русской_web_текст_из_первых_трёх_keywords_ru`; без обоих — `None` и строка в журнале (`assert ... is None`, `caplog`): `test_без_русской_web_и_без_keywords_ru_формулировки_нет_и_есть_строка_в_журнале`; `research` — `None`: `test_research_русской_формулировки_не_строит`.
- План 10 + 2 + 1 после нормализации ≤ 12: `test_план_10_плюс_2_плюс_1_после_нормализации_не_длиннее_потолка` (`assert len(result.queries) <= 12`); второй проход без `analog`: `test_второй_проход_русской_analog_не_несёт`.
- Яндекс получает русскую `analog` с `site:` реестров: `test_яндекс_получает_русскую_analog_с_site_реестров_и_английскую_без_них` (respx).
- Исчерпанный предел — домены реестров в `limit_problems`: `test_исчерпанный_предел_называет_домены_реестров_в_limit_problems`.
- Сквозной: бриф «Продукт» → план → Яндекс (respx) с `site:reestr.digital.gov.ru` → кандидат `ANALOG` с адресом записи → `split_analog_candidates`: `test_продукт_запись_реестра_доходит_до_аналоговой_части` (проверяет текст запроса, `source_type is ANALOG`, попадание url в `analogs`).
- Отказ Яндекса на реестровой формулировке — `SearchError`: `test_отказ_яндекса_на_реестровой_формулировке_поднимается_как_search_error`.

## Мутации (критерий 5), каждая отдельно, откат `git checkout`, дерево после каждой — чистое

| № | Мутация | Файл:строка | Результат `pytest tests/test_ru_registries.py -q` | Статус |
|---|---|---|---|---|
| М-a | Русская `analog` сужается мировым списком: `if lang == "ru":` → `if lang == "zz":` | `backend/app/search/analog_sources.py:158` | 6 failed, 25 passed | убита |
| М-b | `normalize_plan_queries` не зовёт `registry_analog_query`: `registry = registry_analog_query(plan, brief)` → `registry = None` | `backend/app/pipeline/planning.py:167` | 5 failed, 26 passed | убита |
| М-c | `registry_analog_query` не проверяет `research`: `if brief.solution_kind == "research":` → `if False:` | `backend/app/pipeline/planning.py:147` | 2 failed, 29 passed | убита |
| М-d | «HTTP-ошибка → []» в `pytest tests/test_product_radar.py -q` | — | адаптера Product Radar нет; условие «при адаптере» критерия 5 не наступило | не применима |

## Общие проверки
| Проверка | Команда | Факт | Статус |
|---|---|---|---|
| Полный набор | `pytest -q` (сняты переменные ключей и маршрутов) | 1728 passed, 1 warning, 0 failed (87.9 с). Предупреждение starlette/httpx, не из кода задачи | ок (ожидание 1728) |
| Без сети | `unshare -rn` + `pytest -q` (в пространстве имён только `lo`, проверено по `/proc/net/dev`) | 1728 passed, 1 warning, 0 failed | ок |
| Линтер | `ruff check .` | All checks passed! | ок |
| Формат | `ruff format --check .` | 245 files already formatted | ок |
| Размер файлов | `tests/test_backend_layout.py` в прицельном наборе | зелёный; `analog_sources.py` 188, `planning.py` 354, `yandex_parse.py` 302, `yandex_search.py` 373, `test_ru_registries.py` 403 строки | ок |
| Изменения вне задачи | `git diff --name-only 1274ad7 HEAD` | backend: 5 файлов из списка задачи; tests: 4 файла; `prompts/`, `schemas.py`, `search/__init__.py`, `config.py` не тронуты; `git status` пуст | ок |
| Порядок «проба → код» | `git merge-base --is-ancestor 581acc7 52c9843` | проба 581acc7 (21:35:10) раньше кода 52c9843 (21:43:33) | ок |

## Инварианты
- №1 карточки и формат не менялись (в diff нет `export/`, `schemas.py`); запись реестра доходит ссылкой на её страницу — сквозной тест. Полный набор включает `test_export_markdown.py` — зелёный.
- №2 путей автоотправки в diff нет.
- №3 литеральный `grep -rl -e "sk-" -e "gigachat" -e "Api-Key" backend/ tests/` не пуст: тот же набор из 10 файлов, что и на базе 1274ad7 (`diff` списков — пусто). В добавленных строках diff совпадений 0. Слово-граничных `\bsk-` — 6 файлов тестов; длины токенов `sk-` — 3, 7 и 22 символа; идентификаторы рядом с `gigachat`/`Api-Key` — имена заголовков и переменных. Значения не выводились. Нарушения, внесённого задачей, нет.
- №4 `prompts/` не менялись (`git diff --stat 1274ad7 HEAD -- prompts/` пуст); `test_prompts.py` в полном прогоне зелёный.
- №5 отказ Яндекса даёт `SearchError` (тест выше), предел — строка в `limit_problems` с доменами реестров; тихих путей нет.
- №6 `httpx` в `backend/app`: `rg -ln httpx backend/app | rg -v "backend/app/(llm/|search/|cli_doctor\.py$|pipeline/pipeline_context\.py$|pipeline/pipeline_search\.py$)"` — пусто (rc=1); в `planning.py` и `analog_sources.py` `httpx` нет (`rg -n` пусто).
- №7 полный набор проходит без сети (см. выше).
- №8 в diff изменены и `backend/` (5 файлов), и `tests/` (4 файла).
- №9 вне скоупа: в diff нет адаптера, правок промптов, схемы и потребителей `country_status`.
- №10 `pytest tests/test_team_docs.py -v`: 35 passed; реальный `analog_sources.yaml` читается `yaml.safe_load`: секция `ru_registries` — 3 записи, домены и `name_ru` — строки.

## Эскалация Э-4
Вариант А (русская `analog` добавляется кодом) реализован: `registry_analog_query` в `planning.py`, промпты не менялись. Решение принято оркестратором по умолчанию (РЕШ-285 не меняет); верификатор его не переоценивает.

## Отложено (DLG, не закрывают и не меняют критерии)
- **DLG-1: живая проверка реестров с сервера в РФ.** Причина: проба 21.10.2026 из облачной среды (зарубежный адрес): `gisp.gov.ru` — 403, `reestr.digital.gov.ru` — таймаут соединения; живая выдача Яндекса по `site:` реестров из этой среды не проверяется. Не входит в критерии приёмки. Действие владельца: после деплоя Э6-9 на сервер в РФ запустить проверку и приложить результат к Э6-8 (исполнитель: `DLG-Э6-1` в handoff, пункт `deferred`).
- **DLG-2: контейнерный прогон `python:3.12-slim`** (общее правило 3, РЕШ-249). Причина: по постановке — DLG (РЕШ-276); в этой верификации не запускался (docker в среде есть, `/usr/bin/docker`). Действие владельца: решить, нужен ли контейнерный прогон до слияния Э6-8, или принять прогон на общем `.venv` (Python 3.12.3).

## Не проверялось верификатором
- Внешние адреса повторно не запрашивались: проба принята как evidence по критерию 1 (ключей и платных вызовов в ней не было). Ревью цикла 1 воспроизвело два маршрута Product Radar (401 и 200 со статьями блога), совпадение с probe.txt зафиксировано.
- Совет 1 ревью (порядок `analog` в пределе платных запросов, РЕШ-283) не является критерием приёмки и в вердикт не входит.

## Причина FAIL
Нет: все критерии задачи закрыты.
