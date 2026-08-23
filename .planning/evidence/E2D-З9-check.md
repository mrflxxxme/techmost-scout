# Ревью+верификация З-9 · цикл 1

Вердикт: ПРИНЯТЬ (блокирующих 0, советов 1)

## Порядок §12 — подтверждён историей git

- `git show 77189c4 --stat` — только `E2D-З9-diagnosis.md`, `E2D-З9-red.txt`,
  `tests/test_constraints_guard.py` (+265 строк). `constraints_guard.py` в
  этом коммите не тронут — фикса нет.
- `git show af0be76 --stat` — `constraints_guard.py` (+61/-остальное),
  правка одного теста, `E2D-З9-green.txt`. Фикс отдельным коммитом после
  диагноза.
- Независимая проверка: временно вернул `constraints_guard.py` на состояние
  коммита 77189c4 при тесте из того же коммита — `pytest
  tests/test_constraints_guard.py` дал **2 failed, 22 passed**, совпадает
  дословно с `evidence/E2D-З9-red.txt`. Дерево восстановлено до HEAD,
  `git status --porcelain` пуст. На HEAD тот же файл тестов — **24 passed**.
  Порядок «падающий тест до фикса» — факт, не слова.

## Диагноз — числа проверены самостоятельно

Читал `data/scout.db` в режиме `?mode=ro` независимо от исполнителя:
- `requests.id=1`: `status='drafted'`, `cost_rub=9.182` — совпадает с
  diagnosis.md.
- `raw_text` запроса №1 содержит дословно «Требуемая точность — 95–98%» и
  «Уровень технологической готовности — от TRL 6 до TRL 9» — совпадает.
- `SELECT count(*) FROM briefs` → `0`, `solution_cards` для request_id=1 → `5`
  — совпадает с находкой «таблица briefs никогда не заполняется».
- `pipeline_logs` для request_id=1: только `planner, search, enrich, scorer,
  cards, summary, contest_cards` — ни `intake`, ни `intake_constraints`,
  подтверждает разрыв персистентности, о котором пишет диагноз.
- Грепом сверил цитаты отчётов: `E2G-gate-фарма.md:11` — только точность, TRL
  нет; `E2V-gate-фарма.md:11` — оба ограничения. Дословно совпадает с
  diagnosis.md.

Диагноз опирается на реальные данные, а не додуман.

## Фикс — минимальный, границы сохранены

- Старое поведение «список пуст → повторный вызов» сохранено буквально
  (`if brief.constraints: ... elif not has_requirement_signs(...): return`).
- Объединение подтверждено тестом
  `test_retry_constraints_fires_when_constraints_non_empty_but_marker_uncovered`:
  результат содержит и старую точность, и добавленный TRL, не только TRL.
- Здоровый прогон (все маркеры покрыты) не дорожает — тест
  `test_retry_constraints_skipped_when_marker_already_covered` явно проверяет
  `router.cost.calls == 0`. Есть и обратный случай (маркеров в тексте нет
  вовсе) — `test_retry_constraints_skipped_without_requirement_signs`.
- Вызов остаётся однократным при двух непокрытых маркерах —
  `test_retry_constraints_two_uncovered_markers_trigger_single_call`
  (`calls == 1`).

## Цена (пункт г брифа) — подтверждено

Фикс не добавляет вызова модели в обычном случае: когда все маркеры покрыты
собранными требованиями либо признаков требований в тексте нет вообще,
`router.cost.calls == 0` — проверено тестом и воспроизведено мной отдельным
прогоном тех же тестов. Лишний платный вызов на каждом прогоне не появляется.

## Мутации — воспроизведены независимо (3 из 5, обе обязательные + одна доп.)

1. **(а) условие расширено до «любой прогон»** (страховка сработала
   безусловно) — сломал `if`, заменил на `pass`. Результат: **4 failed**
   (`skipped_without_requirement_signs`, `skipped_when_constraints_already_present`,
   `skipped_when_marker_already_covered`, `marker_coverage_ignores_case`) —
   критерий 4 падает, как и предсказано.
2. **(б) объединение заменено затиранием** — вернул `draft_brief.constraints`
   как есть, без слияния со старым списком. Результат: **2 failed**
   (оба новых теста регрессии Э2-Г) — критерий 3 падает.
3. **(г) вызовов стало два** — продублировал вызов `router.complete_json`.
   Результат: **3 failed**, включая
   `test_retry_constraints_two_uncovered_markers_trigger_single_call`
   (`calls == 2` вместо 1) — критерий 5 падает.

Каждая мутация отменена, дерево восстановлено, `git status --porcelain`
пуст после каждого прогона и в конце проверки.

## Границы

`git diff --name-only 0f6ffd3..HEAD` (0f6ffd3 — база волны, влитые ворота
Э2-Г): только `backend/app/pipeline/constraints_guard.py`,
`tests/test_constraints_guard.py` и файлы `.planning/evidence/E2D-З9-*`.
`prompts/`, `backend/app/export/`, `backend/app/cli.py`,
`backend/app/pipeline/orchestrator.py` не тронуты.

## Полный набор и ruff

- `ruff check .`: All checks passed.
- `ruff format --check .`: 116 files already formatted.
- Голая команда `pytest -q --deselect tests/test_team_docs.py::test_state_stays_short`
  в копии исполнителя: **659 passed, 8 failed, 1 deselected**. Разобрал
  расхождение: все 8 падений не связаны с `constraints_guard.py` и не
  введены этой задачей — `git diff --name-only` между базой копии (0f6ffd3) и
  файлами, от которых зависят упавшие тесты, пуст (ни один из четырёх
  файлов тестов не менялся в этом диапазоне). Причины две, обе — известный
  долг волны, не этой задачи:
  - 3 падения (`test_llm_router.py` × 2, `test_probe.py` × 1) вызваны
    загрязнением тестов переменными окружения процесса
    (`YANDEX_*`, `LLM_STAGE_ROUTES`, `LLM_FALLBACK_ORDER` и т.д.) — фикс
    изоляции сделан в З-1 (`9e65cd8`), но копия исполнителя ветвилась от
    `0f6ffd3`, **до** влития З-1 в основную ветку (задача формально зависит
    от слияния З-1, но старт копии этого не дождался — известный факт волны,
    зафиксирован в `3f023e5`: «664 passed с очисткой окружения, 8 failed без
    неё»). Проверил сам: с ручной очисткой этих переменных окружения падения
    пропадают.
  - 5 падений (`test_contest_gate_and_order.py` × 4,
    `test_pipeline_second_pass.py` × 1) тянутся к правкам
    `backend/app/pipeline/orchestrator.py`, сделанным задачами З-5/З-6 и
    влитыми в основную ветку **после** `0f6ffd3` (коммиты `b08b3b8`,
    `14e2d73`, `2fdb5d3`) — в копии исполнителя `orchestrator.py` их не
    содержит. Убедился, что причина именно в этом: один из упавших тестов
    падает на другом дефекте оркестратора (порядок сборки сводки/конкурсных
    карточек), не имеющем отношения к constraints_guard.
  - С теми переменными окружения, которые исполнитель сам перечислил в
    checks handoff (`RELEVANCE_MIN`, `SECOND_PASS_RELEVANCE_MAX`, `YANDEX_*`,
    `LLM_*`), воспроизвёл его число **дословно: 667 passed, 1 deselected, 0
    failed**. Заявление исполнителя подтверждено, а не принято на слово.
  - На сессионной ветке (main, где обе задачи-зависимости уже слиты) та же
    голая команда даёт 670 passed, 1 deselected, 0 failed — расхождение с
    667/671 объясняется исключительно тем, что копия исполнителя стартовала
    раньше слияния параллельных задач волны, а не дефектом этой задачи.
- Секреты (инвариант №3): `grep -rE "sk-|gigachat|Api-Key" backend/ tests/`
  даёт только имена переменных/классов (`GIGACHAT_AUTH_KEY`,
  `GigaChatProvider`, `Api-Key` в заголовке авторизации YandexGPT) — значений
  ключей нет. `.env` в `.gitignore`. Ни один секрет в отчёт не попал ни
  полностью, ни частично.
- `grep -rn "httpx" backend/app` (без учёта `.pyc`): только `llm/`,
  `search/`, `cli.py` (не касается этой задачи, доиспользуется существующим
  кодом `doctor`), `orchestrator.py` не в этом диапазоне вообще не менялся
  этой задачей.
- `tests/test_prompts.py`, `tests/test_team_docs.py` (без
  `test_state_stays_short`): 78 passed — метки в промптах не задеты, YAML
  handoff корректен.

## Инварианты 1-10

№1 факт без ссылки — не применимо (задача про TRL-маркер, не про новые
факты в карточке); код только детектирует маркер, не извлекает TRL в
клиентский текст (граница-эскалация Р-5 верно не сработала — подтверждаю
диагнозом и кодом). №2 не затронут. №3 — секретов нет (см. выше). №4 —
`render_prompt`/`EXPECTED_PLACEHOLDERS` не менялись, `test_prompts.py`
зелёный. №5 — отказ повторного вызова не роняет прогон
(`except Exception` → `log.warning` + `return brief, True`), только
`CostLimitExceededError` пробрasывается намеренно (РЕШ-45, документировано).
№6 — `httpx` вне слоёв не добавлен. №7 — набор офлайн, внешние вызовы через
`fake_provider_factory`. №8 — правки `backend/` сопровождены правками
`tests/`. №9 — diff строго в границах задачи. №10 — YAML handoff проверен
тестом `test_team_docs.py`.

## Советы (не блокируют)

1. Диагноз честно называет находку вне границ задачи (пустая таблица
   `briefs`) и не трогает её — решение верное, перенесено отдельным решением
   РЕШ-156 в план этапа 3.
