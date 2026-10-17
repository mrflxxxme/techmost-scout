# Ревью Э6-1 · цикл 2 (последний, §4)
Вердикт: ВЕРНУТЬ (блокирующих 1, советов 2) — по §4 это BLOCKED: третьего цикла нет, нужен разбор с владельцем.
Проверялся HEAD 137505b (после `git fetch` + `reset --hard origin/claude/e6-1`, `git status` чистый).

## Блокирующие
1. [§5: поведение без теста; инв. 8] Четвёртая точка веб-проводки вида не покрыта: `routes/intake.py::_draft_brief_from_answers`,
   передача `solution_kind=draft.solution_kind` в `refine_brief_stage`. Мутация (М4: удалена эта строка) пережила
   `tests/test_solution_kind_web.py + tests/test_web_intake.py + tests/test_solution_kind.py` — 39 passed, 0 failed.
   Последствие реальное: человек выбрал «Цифровое», ответил на вопросы интейка, `refine_brief` получил умолчание `research`,
   выбор человека сильнее модели — итоговый бриф молча становится «Наукой». Это тот же класс дефекта, что находка 1 цикла 1
   (выбор вида теряется на веб-пути), и точка была в diff с первого цикла: **её пропустил я в цикле 1**, а не исполнитель
   в цикле 2 — три названные мной мутации исполнитель закрыл полностью. Существующие заменители `fake_refine_brief` в
   `test_web_intake.py` не принимают `solution_kind` (ради них и стоит `_kind_kwargs`, который для `research` аргумент
   не передаёт), поэтому тест нужен именно с видом, отличным от `research`.
   Что нужно: один тест на TestClient — `POST /requests` с `solution_kind=digital` и вопросом в ответе заглушки интейка →
   `POST /brief` без `confirm` с ответом → заглушка `app.pipeline.stages.refine_brief` (с `solution_kind="research"` по умолчанию,
   как у настоящей) получила `digital`, `load_brief` после шага даёт `digital`. Мутация М4 должна ронять этот тест.
   Код продукта править не нужно — он верен.
   Рекомендация для разбора: дефект в тесте, а не в коде; одна правка ~20 строк в `tests/test_solution_kind_web.py`
   (файл уже в задаче). Владельцу решить: принять узкую правку без третьего полного цикла (с проверкой М4 перед слиянием)
   либо вернуть Э6-1 в работу в обычном порядке.

## Советы (не блокируют)
1. Советы цикла 1 (убрать `_kind_kwargs` в Э6-2а; писать вид на insert в `persistence_write.py`; отдельный тест метки
   `SOLUTION_KIND` в guard при `solution_kind != research`) остаются в силе, исполнитель их не делал — верно, это Э6-2а.
   Убрав `_kind_kwargs`, получим и тест на М4 «бесплатно»: заменители перестанут скрывать потерю аргумента.
2. В `_request_id` тестов номер запроса вынимается цепочкой `split` по тексту фрагмента; хрупко при смене вёрстки заголовка.
   Не дефект (тест падает громко), на будущее — читать id из `hx-post="/requests/N/brief"`.

## Проверено
- Находка 1 цикла 1 (три мутации, по одной, откат `git checkout -- <файл>`; прогон `pytest tests/test_solution_kind_web.py -q`):
  - М1 (убрана строка `fields["solution_kind"] = saved.solution_kind …` в `_confirm_brief`): 1 failed, 5 passed —
    `test_confirm_brief_keeps_solution_kind_of_saved_draft`. УБИТА.
  - М2 (убрано `solution_kind=kind,` в `create_request` → `run_intake_stage`): 5 failed, 1 passed — три параметра
    `test_create_request_passes_…[digital|product|business_model]`, `test_confirm_brief_…`, `test_deidentification_warning_…`. УБИТА.
  - М3 (убрана скрытая `<input name="solution_kind">` в `_deidentification_warning.html`): 1 failed, 5 passed —
    `test_deidentification_warning_carries_hidden_solution_kind_and_resend_keeps_it`. УБИТА.
  - Базовый прогон файла до мутаций: 6 passed. После всех откатов `git status` чистый.
- Сверх карточки, М4 (см. находку 1): выжила, 39 passed. Откачена.
- Тесты по существу (не заглушка самой себя): заглушка `run_intake` принимает `solution_kind` с умолчанием `research`, как настоящая,
  и кладёт значение в `draft_brief`; утверждения сверяют аргумент, дошедший до интейка, и строку `briefs.solution_kind` в базе
  (через `session_scope`, не через ответ заглушки). Тест (в) собирает повторный POST из скрытых полей HTML-ответа — проверяет
  реальную разметку. Сеть не нужна (подменён `app.pipeline.stages.run_intake`) — инв. 7 соблюдён.
- Правило 4 / инв. 8: `git diff --stat origin/claude/e6-w1...HEAD -- tests/` — `test_prompts.py` (1 строка), `test_schemas_e6.py`,
  `test_solution_kind.py`, `test_solution_kind_web.py` (+162); существующие тесты не переписаны.
- `pytest -q --deselect tests/test_docker_compose_backup_runtime.py`: 1415 passed, 9 deselected, 1 warning, 0 failed (300 s).
- `ruff check .`: All checks passed!. `ruff format --check .`: 223 files already formatted.
- Границы задачи: изменённые файлы вне `.planning/` — db, models, constraints_guard, intake, intake_flow, schemas, brief_store,
  routes/intake, `_deidentification_warning.html` (заявлен и обоснован в цикле 1), index.html, prompts/intake.md, CHANGELOG,
  тесты. Цикл 2 тронул только `tests/` и `.planning/evidence/` — код продукта не менялся. Файлов вне списка нет.
- Инварианты: №3 `grep -rE "sk-|gigachat|Api-Key" backend/ tests/…` — только прежние комментарии про Api-Key в config/llm/patents_view, новых нет;
  №6 `httpx` в backend/app — llm/, search/, pipeline_context/pipeline_search, cli_doctor (прежний набор, дифф его не трогает);
  №7 сеть не нужна; №8 есть; №9 скоуп не расширен; №10 YAML не затронут; №1, 2, 4, 5 не затронуты циклом 2 (инв. 4 — по циклу 1 все вызовы `render_prompt("intake")` заполняют метку).
- Это цикл 2 — последний; третьего цикла не будет.
