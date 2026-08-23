# Ревью+верификация З-5 · цикл 1
Вердикт: ПРИНЯТЬ

## Поправка к постановке (зафиксирована по указанию оркестратора)
Постановка ревью содержала ошибочный пункт (б): «задвоение строки — блокирующее».
Карточка З-5 в `.planning/PLAN.md` (строки 501–504, «Что делать», п.2) прямо
предписывает обратное: печать в `ScoutPipeline.run` (`run_flow.py:88`) сохраняется,
задвоение строки в логе `scout run` названо «сознательной ценой» и требует лишь
комментария на месте правки. Оркестратор отменил свой пункт (б) до сдачи отчёта;
далее задвоение проверялось не как дефект, а как ожидаемое, документированное
поведение.

## Сверка diff с задачей
Изолированный коммит задачи (`47e27e8^..47e27e8`) трогает ровно два файла:
`backend/app/cli.py` (+13) и `tests/test_cli_run.py` (+91) — совпадает с
заявленным списком файлов карточки. Второй коммит `e30d01d` добавляет только
`.planning/evidence/E2E-З5-handoff.yaml`. Выход за границы задачи не найден:
`prompts/`, `backend/app/pipeline/history.py`, `backend/app/export/`,
`backend/app/pipeline/run_flow.py`, `backend/app/pipeline/run_journal.py` не
тронуты (`git diff 47e27e8^..e30d01d --stat -- prompts/ backend/app/pipeline/history.py backend/app/export/`
— пусто).

## а) Главный тест — смерть на интейке
`test_params_line_printed_when_intake_stops_on_cost_limit` роняет
`run_intake` исключением `CostLimitExceededError` (стоп-кран, РЕШ-45) и
проверяет, что строка «Параметры прогона:» всё равно попала в `caplog` —
именно путь, названный долгом ворот Э2-Д (смерть ДО `ScoutPipeline.run`).

Мутация «печать перенесена после `run_intake`» (поставлена самостоятельно,
редактированием `backend/app/cli.py`: убрана печать из начала `_run`, вставлена
сразу после строки с вызовом `run_intake`) — прогон
`pytest tests/test_cli_run.py -k params_line -v`:
```
FAILED tests/test_cli_run.py::test_params_line_printed_when_intake_stops_on_cost_limit
FAILED tests/test_cli_run.py::test_params_line_printed_before_run_intake_is_called
2 failed, 1 passed, 19 deselected
```
Оба целевых теста краснеют, третий (без задвоения) не задет — ожидаемо. Дерево
возвращено, `git status --porcelain` пуст.

## б) Задвоение — проверено как ожидаемое поведение (не блокирует)
1. `run_flow.py:88` не тронут: `log.info(format_active_run_params(self._settings))`
   стоит там же, с собственным комментарием, поясняющим причину (веб-прогон,
   прямые вызовы пайплайна и тесты пользуются этим путём).
2. Причина задвоения написана комментарием на месте правки в `cli.py` (14
   строк перед вызовом), а не подразумевается — соответствует требованию плана.
3. Прямой вызов `ScoutPipeline.run` (без CLI) печатает строку ровно один раз —
   `cli._run` в этом пути не участвует вовсе, второй точки печати физически
   нет; `tests/test_run_visibility.py` (5 тестов, не правился) зелёный.
4. На полном прогоне `scout run` строка действительно печатается дважды — это
   заявлено в самой задаче как «сознательная цена» и подтверждено новым тестом
   `test_params_line_not_duplicated_by_pipeline_on_successful_run`, который
   проверяет, что именно `cli._run` не печатает дубль сам по себе (пайплайн в
   тесте — заглушка `_RecordingPipeline`, не печатающая строку вовсе; лишняя
   запись в `caplog` доказывала бы задвоение источником в самом `cli.py`).
   Числа: в этом тесте `len(matches) == 1` — печать `cli._run` не задваивается
   сама по себе, что и требовалось проверить в её собственной зоне
   ответственности.

## в) Страж `tests/test_run_visibility.py` не сломан
`git -C <копия> diff 9a63365..e30d01d -- tests/test_run_visibility.py` — 0
строк, файл не правился. Прогон:
```
pytest tests/test_run_visibility.py -v
5 passed
```

## г) Секреты
`format_active_run_params` не изменялась (`run_journal.py` вне диффа задачи) и
собирает только величины без секретной составляющей (лимит, `relevance_min`,
маршруты стадий, порядок резерва, имена моделей) — те же поля, что уже
проверены существующим тестом `test_params_line_has_no_secret_values` в
`tests/test_run_visibility.py` для пути `ScoutPipeline.run`.

Регрессия проверена вручную: временно дописано в вызов `cli.py`
`format_active_run_params(settings) + " sk-FAKESECRETVALUE123"` — прогон
`pytest tests/test_cli_run.py tests/test_run_visibility.py`: **27 passed**, ни
один тест не покраснел. Это значит, что специализированного стража секретов
именно на пути `cli._run` в тестах нет — новая печать реиспользует уже
проверенную функцию без изменений и не добавляет иных `print`/`log`, поэтому
структурного риска утечки не вижу (секретные поля `Settings` — `SecretStr`,
`format_active_run_params` их не трогает), но фиксирую это отдельным советом
ниже, не блокирующим приёмку. Дерево возвращено, `git status --porcelain`
пуст.

## д) Цена
Тест `test_params_line_printed_before_run_intake_is_called` явно проверяет
`router.cost.calls == 0` после печати и заглушки интейка — печать не тратит
обращений к модели. Внешних источников печать также не касается (функция
работает только с `Settings`, без сети).

## е) Границы
`prompts/`, `backend/app/pipeline/history.py`, `backend/app/export/` не
тронуты (см. выше). `backend/app/cli.py` вырос с 656 до 669 строк (+13):
14-строчный комментарий + отложенный импорт + вызов — ровно то, что требует
плановый пункт «причина пишется комментарием, а не подразумевается»,
избыточного кода нет.

## Мутации карточки (независимое воспроизведение)
- (а) печать перенесена после `run_intake` — воспроизведена лично, красит
  `test_params_line_printed_when_intake_stops_on_cost_limit` и
  `test_params_line_printed_before_run_intake_is_called` (см. раздел «а»).
- (б) печать только в успешной ветке (перенесена в конец `_run`, после блока
  вывода стоимости) — воспроизведена лично:
  ```
  pytest tests/test_cli_run.py -k params_line -v
  FAILED test_params_line_printed_when_intake_stops_on_cost_limit
  FAILED test_params_line_printed_before_run_intake_is_called
  2 failed, 1 passed, 19 deselected
  ```
  Дерево возвращено, `git status --porcelain` пуст.
- (в) и (г) из карточки не переставлялись повторно: заявленные исполнителем
  результаты (падение `test_run_visibility.py` и падение всех трёх новых
  тестов соответственно) логически прямые следствия кода функции
  `_params_line_index`, проверены чтением, независимого повтора не потребовалось.

## Полный набор и статика
```
/home/user/techmost-scout/.venv/bin/python -m pytest -q
719 passed, 1 warning in 38.18s
```
```
ruff check .
All checks passed!
ruff format --check .
132 files already formatted
```

## Инварианты (все десять)
1. Факт без ссылки — задача печати не касается карточек/evidence, не применимо.
2. Автоотправка клиенту / снятие «ЧЕРНОВИК» — не затронуто.
3. Секреты: `grep -rE "sk-|gigachat|Api-Key" backend/ tests/` — совпадения
   только в существующем безопасном коде (заголовки авторизации, тестовые
   фикстуры с фиктивными ключами, комментарии); значений секретов в отчёте не
   приводилось. `.env`/`.env.*` в `.gitignore` — да. См. раздел «г» про ручную
   регрессию.
4. `render_prompt` и подстановки — задача промптов не касается;
   `pytest tests/test_prompts.py` (часть полного набора, 719 passed) зелёный.
5. Отказ источника — не применимо, задача не про адаптеры.
6. `grep -rn "httpx" backend/app` — совпадения только в `llm/`, `search/`,
   `pipeline/pipeline_context.py`, `pipeline/pipeline_search.py` и **уже
   существовавшая** (до этой задачи) строка в `cli.py:243` — вне диффа задачи
   (`scout doctor`/проверка соединения), новых нарушений задача не добавила.
7. `pytest` без сети — да, полный набор зелёный, новые тесты используют
   `monkeypatch`/заглушки, respx не требовался (LLM/сеть не вызываются).
8. Изменения в `backend/` сопровождены изменениями в `tests/` — да,
   `cli.py` + `test_cli_run.py`.
9. Diff сверен с задачей и разделом «Границы» — совпадает, отклонений нет.
10. YAML с двоеточием — `pytest tests/test_team_docs.py` (часть полного
    набора, 719 passed включает и его) зелёный; новый handoff YAML исполнителя
    визуально проверен — двоеточий в значениях, требующих кавычек, не найдено.

## Советы (не блокируют)
1. Специального теста-стража секретов именно на пути `cli._run` (по аналогии с
   `test_params_line_has_no_secret_values` в `tests/test_run_visibility.py`,
   который проверяет только путь `ScoutPipeline.run`) сейчас нет — ручная
   проверка (раздел «г») показала, что дописанное в вызов значение не поймал
   ни один тест. Риска сегодня не создаёт (функция и её вход не изменились),
   но если печать в `cli.py` когда-нибудь обрастёт своими полями — стоит
   завести зеркальный тест.
2. Название `_params_line_index` и inline-хелпер в конце `test_cli_run.py`
   дублируют похожую функцию в `test_run_visibility.py` почти дословно —
   вкусовщина, функционального следствия нет.

## Проверено (фактический вывод команд)
- `pytest -q`: 719 passed, 1 warning (StarletteDeprecationWarning, не от задачи)
- `ruff check .`: All checks passed!
- `ruff format --check .`: 132 files already formatted
- `pytest tests/test_cli_run.py tests/test_run_visibility.py`: 27 passed
- `git diff 47e27e8^..47e27e8 --stat`: только `backend/app/cli.py` (+13),
  `tests/test_cli_run.py` (+91)
- `git diff 9a63365..e30d01d -- tests/test_run_visibility.py`: пусто (файл не правился)
- Мутация (а) прицельно: 2 failed из 3 целевых тестов (ожидаемо)
- Мутация (б) прицельно: 2 failed из 3 целевых тестов (ожидаемо)
- Секретная регрессия (ручная): 27 passed при дописанном фиктивном ключе —
  see совет 1
- `git status --porcelain` после всех мутаций: пусто
