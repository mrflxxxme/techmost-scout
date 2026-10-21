# Верификация Э6-2в · цикл 1

Вердикт: PASS (критериев задачи закрыто 3 из 3; общих критериев закрыто 6 из 6)

Ветка `claude/e6-2v`, HEAD до коммита верификации `e2efe58`, база `claude/sleepy-planck-11w8jv`.
Прогоны из рабочей копии `/home/user/techmost-scout/.claude/worktrees/e6-2v`, окружение без ключей (`env -u …`).

## Критерии приёмки из PLAN.md (Э6-2в)

| № | Критерий | Команда / способ | Факт | Статус |
|---|---|---|---|---|
| 1а | `pytest tests/test_yandex_analog.py -v` проходит | pytest -v | 37 passed | ок |
| 1б | `analog` несёт `site:` доменов списка | тесты `test_analog_уходит_пачками…`, `test_with_site_filter_сужает_analog…`; независимо: тела запросов и тексты пачек | 37 passed; в каждой пачке `site:` каждого её домена (sites_ok=True) | ок |
| 1в | длина каждой формулировки ≤ предела (400) | тесты `test_каждая_пачка_не_длиннее…`, `test_analog_уходит_пачками…`; независимо: текст 52 символа → 2 пачки, длины [396, 225]; текст 47 → [391, 220]; текст 318 → 7 пачек, max 399 | all ≤ 400 = True во всех трёх случаях | ок |
| 1г | не влезшие домены названы в `limit_problems` | тесты `test_формулировка_без_места_под_site_называет_все_домены_не_влезшими`, `test_пачки_сверх_предела_называются_в_limit_problems_поимённо`, `test_исчерпанный_предел_до_первой_пачки_называет_все_домены` | 3 passed (поимённо) | ок |
| 1д | кандидаты — `ANALOG` | тест `test_analog_уходит_пачками…` (assert `candidate.source_type is SourceType.ANALOG`) | passed | ок |
| 1е | домен со схемой, путём или дублем — ошибка загрузки | тесты `test_домен_со_схемой_путём_или_пробелом_это_ошибка_загрузки[8 вариантов]`, `test_дубль_домена_внутри…`, `…между_секциями…`; независимо через `_validate`: `https://a.com`, `a.com/reports`, `a.com/`, `A.com`, `a .com`, `a.com:8080`, `localhost`, `-a.com`, дубль внутри секции, дубль между секциями, нестрока → ValueError; контроль (корректный файл) → без ошибки | все ValueError, контроль проходит | ок |
| 1ж | есть `ycombinator.com` и `producthunt.com` | тест `test_каталоги_ycombinator_и_producthunt_в_списке`; независимо: загруженный список | True / True | ок |
| 1з | доменов 20–30 | тест `test_справочник_читается_и_укладывается_в_20_30_доменов`; независимо: 27 доменов (25 `reports` + 2 `catalogs`), повторов 0 | 27 в [20; 30] | ок |
| 2 | `pytest tests/test_search_yandex.py tests/test_yandex_limit_visibility.py -v` зелёные без правок | pytest -v; `git diff --name-status claude/sleepy-planck-11w8jv HEAD -- tests/` | 39 passed; в diff по `tests/` только `A tests/test_yandex_analog.py` | ок |
| 3 | мутация «снять сужение для `analog`» → ≥1 failed | мутация `with_site_filter`: `texts = plan_analog_queries(query.text).texts` → `texts = (query.text,)`, прогон `tests/test_yandex_analog.py`, откат | 3 failed, 34 passed; после отката sha256 `yandex_parse.py` совпал с исходным | ок |

## Общие критерии этапа (PLAN.md, «Общие правила этапа», п. 3–6, 8)

| № | Критерий | Команда | Факт | Статус |
|---|---|---|---|---|
| G1 | `pytest -q`: 0 failed, passed ≥ база + новые | `pytest -q` (без ключей) | 1609 passed, 0 failed; база: `pytest --collect-only` архива `claude/sleepy-planck-11w8jv` = 1572, + 37 новых = 1609 | ок |
| G2 | ruff: 0 замечаний | `ruff check .`; `ruff format --check .` | All checks passed!; 239 files already formatted | ок |
| G3 | прогон без сети | `unshare -rn … pytest -q` | 1609 passed; в namespace `/proc/net/dev` содержит только `lo`; исходящее соединение → OSError | ок |
| G4 | инв. 6: httpx только в разрешённых местах | `rg -ln httpx backend/app \| rg -v "backend/app/(llm/\|search/\|cli_doctor\.py$\|pipeline/pipeline_context\.py$\|pipeline/pipeline_search\.py$)"` | пусто | ок |
| G5 | инв. 10: `yaml.safe_load`, без `unsafe_load` | `rg -c "unsafe_load\|yaml\.load\("` по `backend tests scripts` | пусто; `analog_sources.py` использует `yaml.safe_load`; `checked_at` в кавычках (тип str) | ок |
| G6 | инв. 8: backend/ и tests/ меняются вместе | `git diff --name-only claude/sleepy-planck-11w8jv HEAD` | backend: 4 файла; tests: `tests/test_yandex_analog.py` | ок |
| G7 | размеры файлов ≤ 500 | `wc -l` | `yandex_search.py` 373, `yandex_parse.py` 300, `analog_sources.py` 152, `tests/test_yandex_analog.py` 370 | ок |
| G8 | `git status`: лишних тронутых файлов нет | `git status --short` | пусто (до коммита верификации) | ок |

## Инварианты

№3 секретов нет: в добавленных строках ветки (`git diff … -- backend tests`) совпадений с `sk-|gigachat|Api-Key` нет; совпадения в прежних файлах (имена: `backend/app/search/yandex_search.py`, `backend/app/search/patents_view.py`, `backend/app/config.py`, `backend/app/llm/yandex.py`, прежние тесты с фиктивными значениями) задачей не вносятся · №5 пропуск виден (тесты `limit_problems`, отказ — `SearchError`) · №6 см. G4 · №7 см. G3 · №8 см. G6 · №10 см. G5 · №1, №2, №4 к задаче не относятся: промптов и карточек в diff нет · №9 границы: файлы из перечня задачи плюс принятая оркестратором правка `backend/app/search/yandex_search.py` (`_search_analog`), других файлов вне перечня в diff нет.

## Вне критериев (на вердикт не влияет)

- Живой платный вызов Яндекс Search не выполнялся: критерии задачи его не требуют, проверка идёт на подменах (respx).
- Утверждение состава белого списка и текстов владельцем в PR (правило 7 этапа, РЕШ-257) — шаг ворот, не критерий верификации.
- Файл `.planning/evidence/Э6-W3-Э6-2в-checks.txt` исполнителя — вывод прежнего цикла (1549 passed, другой rootdir); в доказательства не берётся, данные выше получены заново.
- Временный вывод pytest был по ошибке записан в `.planning/evidence/` рабочей копии и удалён до коммита; `git status` чистый.

## Причина FAIL

Нет: критерии закрыты, FAIL не выставлен.
