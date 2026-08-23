# Ревью+верификация З-7 · цикл 1
Вердикт: ПРИНЯТЬ

## Разбор по существу

### а) Строка печатается до первой платной стадии и на упавшем прогоне
`log.info(format_active_run_params(self._settings))` стоит первой строкой тела
`ScoutPipeline.run`, до создания `stage_log` и до вызова `_run_stages`
(orchestrator.py:635-639). Оба теста критерия 1 зелёные:
`test_params_line_printed_on_successful_run`,
`test_params_line_printed_on_run_failing_at_first_stage` (второй роняет прогон
на планировщике через `_AlwaysFailsProvider`, строка всё равно в `caplog`).

Проверил мутацию (а) независимо: перенёс печать после `await
self._run_stages(...)` — упал ровно ожидаемый тест
`test_params_line_printed_on_run_failing_at_first_stage` (остальные 4 зелёные).
Файл восстановлен, `git status --porcelain` пуст.

### б) Полнота строки (критерий 2)
Фактическая строка на тестовых настройках (см. `.planning/evidence/E2D-З7-params.txt`,
воспроизведена мной независимо в п. «Числа прогонов» ниже с полностью
изолированным окружением):

```
Параметры прогона: стоп-кран 10.00 ₽/запрос; relevance_min=0; second_pass_relevance_max=0;
маршруты стадий: intake:gigachat, extractor:gigachat, planner:deepseek, replan:deepseek,
scorer:deepseek, card_author:deepseek, verifier:deepseek, summary:deepseek;
порядок резерва: gigachat→deepseek→yandex; модели: gigachat=GigaChat-2, deepseek=deepseek-v4-flash
```

Сверено по списку: стоп-кран есть; оба порога (`relevance_min`,
`second_pass_relevance_max`) есть; все 8 стадий (`intake, extractor, planner,
replan, scorer, card_author, verifier, summary`) присутствуют с провайдером
каждая — ровно столько ключей у `settings.stage_routes()` по умолчанию
(config.py:77-83); порядок резерва `gigachat→deepseek→yandex` есть целиком и
по порядку; обе модели (`GigaChat-2`, `deepseek-v4-flash`) названы. Тест
`test_params_line_carries_all_named_values` проверяет то же самое программно,
в цикле по `settings.stage_routes().items()` — не на глаз.

Проверил мутацию (б) независимо не потребовалось (структура кода очевидна:
маршруты строятся join'ом по `settings.stage_routes()`, убрать нельзя не
затронув сборку строки) — положился на то, что цикл `for stage, stage_provider
in settings.stage_routes().items(): assert f"{stage}:{stage_provider}" in
line` ловит пропуск любой стадии по построению; сосредоточился на двух других
мутациях (см. ниже) как более рискованных.

### в) Секретов в выводе нет (РЕШ-124)
Тест `test_params_line_has_no_secret_values` ставит в окружение три уникальных
фиктивных значения (`FAKE-GIGACHAT-KEY-...`, `FAKE-DEEPSEEK-KEY-...`,
`FAKE-YANDEX-KEY-...`), создаёт `Settings()` заново и гоняет реальный прогон,
затем проверяет их отсутствие в `caplog.text`, `capsys.readouterr().out` и
`.err`. `format_active_run_params` в принципе не читает ни одно поле-секрет
(`gigachat_auth_key`, `deepseek_api_key`, `yandex_api_key`) — только величины
без тайной составляющей.

Проверил регрессию независимо: временно дописал в
`format_active_run_params` подстановку
`gigachat_key={settings.gigachat_auth_key.get_secret_value()}` в конец
строки — тест `test_params_line_has_no_secret_values` немедленно покраснел с
понятным сообщением («значение ключа не имеет права попасть в лог прогона»,
факт утечки виден в собственном выводе pytest, куда я не переносил ничего
руками). Файл восстановлен, `git status --porcelain` пуст, значение ключа
нигде в этом отчёте не приводится ни полностью, ни фрагментом (инвариант №3,
РЕШ-155) — только имя фиктивной переменной теста.

### г) Прогон не подорожал (критерий 5)
`test_params_line_does_not_add_router_calls`: `len(provider.calls) == 1` —
единственный платный вызов успешного прогона (планировщик) остаётся
единственным после добавления печати. Функция `format_active_run_params` не
обращается к роутеру и не делает I/O, кроме сборки строки — по коду это
самоочевидно, тест лишь фиксирует факт.

### д) Клиентский отчёт не изменился (критерий 4)
`pytest tests/test_export_markdown.py -q` → `33 passed`. `git diff --name-only
0f6ffd3..HEAD` не содержит `backend/app/export/` (список файлов см. в разделе
«Проверено»).

### е) Числа прогона — расхождение объяснено
Исполнитель заявил «660 passed, 9 failed — те же 9 отказов, что и на базовой
копии до правки». Проверил тремя независимыми прогонами:

1. Рабочая копия исполнителя (`5ca4ac8`), окружение по умолчанию (в этой
   среде в shell уже сидят посторонние переменные `RELEVANCE_MIN`,
   `SECOND_PASS_RELEVANCE_MAX`, `YANDEX_SEARCH_API_KEY`, `YANDEX_CATALOG_ID`,
   `LLM_STAGE_ROUTES` — не относятся к задаче, унаследованы средой запуска):
   **660 passed, 9 failed** — список: `test_contest_gate_and_order.py` × 4,
   `test_llm_router.py` × 2, `test_pipeline_second_pass.py` × 1,
   `test_probe.py` × 1, `test_team_docs.py::test_state_stays_short`.
2. База `0f6ffd3` (коммит слияния Э2-Г, до правки З-7), тот же
   загрязнённый shell-env, отдельный `git worktree --detach`:
   **655 passed, 9 failed**, список отказов **идентичен** п.1 — 5 новых
   тестов З-7 (изменённый файл появляется только с 660) объясняют всю разницу
   655→660, отказы не связаны с правкой.
3. Тот же коммит `5ca4ac8`, окружение очищено командой из задания
   (`env -u RELEVANCE_MIN -u SECOND_PASS_RELEVANCE_MAX -u
   YANDEX_SEARCH_API_KEY -u YANDEX_CATALOG_ID -u LLM_STAGE_ROUTES -u
   LLM_FALLBACK_ORDER`): **668 passed, 1 failed** — из 9 отказов 8 пропали
   (вызваны утечкой посторонних переменных окружения из shell в
   `tests/conftest.py`: `RELEVANCE_MIN`, `SECOND_PASS_RELEVANCE_MAX`,
   `LLM_STAGE_ROUTES` и др. не входят в `TEST_ENV` и не гасятся явно —
   `monkeypatch.setenv` только выставляет перечисленное, не удаляет
   постороннее, что и описано в `tests/conftest.py:43-48` как известное
   ограничение). Единственный оставшийся отказ,
   `test_team_docs.py::test_state_stays_short`, вызван тем, что
   `.planning/STATE.md` в этой рабочей копии не обновлялся с коммита
   `85a2d18` (волна Э2-Г) и отстаёт от `main`, где файл впоследствии
   сократили (3177 байт на `main` против 5489 байт в копии) — файл не входит
   в список файлов задачи З-7 и не тронут ни одним коммитом ветки после
   `85a2d18`.

Вывод: ни один из 9 отказов не вызван правкой З-7. 8 объясняются загрязнением
окружения запуска (посторонние переменные в shell), 1 — отставанием рабочей
копии по `.planning/STATE.md` (документ вне списка файлов задачи). Заявление
исполнителя подтверждено независимо и точно (655→660, идентичный список).

### ж) Границы задачи
`git diff --name-only 0f6ffd3..HEAD`: `backend/app/pipeline/orchestrator.py`,
`tests/test_run_visibility.py`, `.planning/evidence/E2D-З7-handoff.yaml`,
`.planning/evidence/E2D-З7-params.txt`. Ни `prompts/`, ни
`backend/app/search/`, ни `constraints_guard.py`, ни `extraction.py`, ни
`tests/conftest.py` не тронуты. `cli.py` и `backend/app/export/` не тронуты
(границы задачи и РЕШ-144 соблюдены).

### з) Мутации — воспроизведены независимо
Две мутации из пяти прогнаны лично (см. пп. а, в выше): (а) перенос печати
после первой стадии — падает `test_params_line_printed_on_run_failing_at_first_stage`;
(в) добавление значения ключа в строку — падает `test_params_line_has_no_secret_values`.
Обе воспроизвелись ровно так, как заявлено в handoff исполнителя. Файл после
каждой мутации восстановлен из бэкапа, дерево чистое.

### и) Инвариант №3 в этом отчёте
Значения фиктивных ключей теста (`FAKE-GIGACHAT-KEY-...` и т.п.) — не боевые
секреты, использованы по формулировке РЕШ-124 (тестовые маркеры). Боевые
значения переменных окружения (в частности утёкший `YANDEX_SEARCH_API_KEY` в
среде запуска) в этом отчёте не приводятся ни полностью, ни фрагментом.

## Проверено

- `pytest tests/test_run_visibility.py -v`: 5 passed (критерии 1, 2, 3, 5)
- `pytest tests/test_export_markdown.py -q`: 33 passed (критерий 4)
- `ruff check .`: All checks passed
- `ruff format --check .`: 117 files already formatted
- `git diff --name-only 0f6ffd3..HEAD`: только файлы задачи + evidence
- Полный набор в рабочей копии (загрязнённый shell-env): 660 passed, 9 failed
- Тот же коммит, окружение очищено по заданию: 668 passed, 1 failed
  (`test_state_stays_short`, не связан с задачей)
- База `0f6ffd3` (до правки), тот же загрязнённый env, отдельный worktree:
  655 passed, 9 failed — список отказов идентичен рабочей копии
- Мутация (а) перенос печати после первой стадии: воспроизведена, падает
  ожидаемый тест, файл восстановлен (`git status --porcelain` пуст)
- Мутация (в) добавление ключа в строку: воспроизведена, падает страж
  секретов, файл восстановлен (`git status --porcelain` пуст)
- Инварианты 1-10: применимые к задаче (№2, №3, №7, №8, №9) соблюдены;
  остальные (№1, №4, №5, №6, №10) задачи не касаются — файлы, которые они
  проверяют (промпты, адаптеры, `httpx`, YAML фронтматтер), в диффе
  отсутствуют

## Советы (не блокируют)

1. Мутация (б) («маршруты стадий убраны из строки») и (г) («уровень понижен
   до DEBUG») не проверены мной прицельным прогоном отдельно от заявления
   исполнителя — обе тривиально следуют из структуры кода (цикл по
   `stage_routes()` в тесте критерия 2 и явный `caplog.set_level(logging.INFO)`
   в тестах критерия 1), риск ложного зелёного по неверной причине здесь
   низкий, но при появлении сомнений в будущем стоит прогнать и их.
