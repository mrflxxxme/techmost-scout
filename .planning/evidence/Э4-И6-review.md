# Ревью Э4-И6 · scout export и кнопка выгрузки · цикл 1

Вердикт: ПРИНЯТЬ (блокирующих 0, советов 2)

## Проверено

- `pytest tests/test_cli_export.py -v`: 10 passed
- `pytest tests/test_web_export.py -v`: 6 passed
- `pytest tests/test_cli_run.py tests/test_cli_doctor_schema_guard.py -v`: 27 passed (прежние команды CLI не задеты)
- `pytest -q`: 1045 passed (база 1029 + 16 новых, падений 0)
- `ruff check .`: All checks passed
- `ruff format --check .`: 189 files already formatted
- `pytest tests/test_backend_layout.py tests/test_web_templates.py -q`: 8 passed
- `grep -n "load_report|apply_overrides" backend/app/cli_export.py`: совпадений нет
- `grep -n "httpx" backend/app/cli_export.py`: совпадений нет
- `grep -rE "sk-|gigachat|Api-Key" backend/app/cli_export.py backend/app/web/routes/report.py tests/test_cli_export.py tests/test_web_export.py`: совпадений нет
- `wc -l backend/app/cli_export.py backend/app/cli.py backend/app/web/routes/report.py`: 101 / 194 / 200 строк — предел 500 не нарушен, `cli_export.py` уложился в заявленные ≤200
- Diff сверен со списком файлов задачи (PLAN.md, раздел Э4-И6): `cli.py`, `cli_export.py`, `web/routes/report.py`, `web/templates/report.html`, `tests/test_cli_export.py`, `tests/test_web_export.py` + два ожидаемых evidence-артефакта исполнителя (handoff, pytest.txt) — файлов вне границ задачи нет
- `theme.css` не тронут, использованы только существующие классы `.btn`, `.btn.ghost` — подтверждено diff'ом report.html

## Мутации (прицельный прогон)

1. Отключил проверку `view.is_draft and not draft` в `cli_export.py` (заменил на `False and ...`) →
   `pytest tests/test_cli_export.py -v -k drafted`: `test_drafted_without_draft_flag_refuses_with_code_1_and_no_export_call`
   упал (`exit_code 0` вместо `1`) — мутация поймана нужным тестом. Файл возвращён в исходное состояние
   (`git status` после отката — пусто).
2. Та же мутация в `web/routes/report.py` (маршрут `download_pdf`) →
   `pytest tests/test_web_export.py -v -k drafted`: `test_export_pdf_drafted_without_draft_param_refuses_without_calling_exporter`
   упал (`200` вместо `400`) — поймана. Файл возвращён.

Обе мутации убиты целевыми тестами с первого раза, полный набор не потребовался.

## Разбор по инвариантам (все десять)

1. **Факт без ссылки** — задача не пишет и не показывает новых фактических утверждений
   (только служебные фразы отказов/подписи кнопки), не применимо.
2. **Замок черновика (ключевой)** — соблюдён на обоих путях:
   - CLI: `_export` строит `ExportView` через `build_export_view` (единственный источник `is_draft`,
     `export/view.py:103`, берётся из `request_row.status`, а не задаётся задачей) и отказывает кодом 1
     без вызова экспортёра, если `view.is_draft and not draft` — подтверждено тестом и мутацией №1.
   - Веб: тот же `build_export_view`, тот же признак `is_draft`; без `?draft=true` — 400,
     экспортёр не вызывается — подтверждено тестом и мутацией №2. Кнопка сама подставляет
     `?draft=true` только когда `view.is_draft` истинно (`report.py:93`, `show_report`) — то есть
     параметр веб-маршрута появился не как самостоятельный обход, а как отражение того же
     признака, что уже решил показ пометки «ЧЕРНОВИК» на странице; прямой переход по «голой»
     ссылке на drafted-отчёт (без параметра) получает 400, а не тихую отдачу. Легитимно.
   - validated-запрос + `draft=true`/`--draft`: `view.is_draft` в этом случае ложно, условие
     `view.is_draft and not draft` не срабатывает независимо от флага — поведение безвредно
     (обычный файл), проверено чтением кода и логики (`is_draft` определяется статусом запроса,
     а не параметром вызова).
3. **Секреты** — грепы чистые, `.env` не трогается, значения не печатаются.
4. **`render_prompt`/подстановки** — задача не работает с промптами, не применимо.
5. **Отказ источника → пустой список + `sources_unavailable`** — задача не работает с адаптерами
   источников, не применимо.
6. **`httpx` только в llm/search/оркестраторе** — `grep -n "httpx" backend/app/cli_export.py`
   пусто; в `web/routes/report.py` `httpx` не появляется (только `FileResponse`/`export_pdf`).
7. **Сеть в тестах** — `export_pdf`/`export_docx` подменены заглушками в обоих новых файлах тестов,
   Chromium/python-docx не вызываются; `pytest -q` зелёный без сети.
8. **`backend/` без `tests/`** — на каждый изменённый файл `backend/app` есть парный тест
   (`cli.py`+`cli_export.py` → `tests/test_cli_export.py`; `web/routes/report.py`+`report.html` →
   `tests/test_web_export.py`); регресс проверен прогоном `test_cli_run.py`/`test_cli_doctor_schema_guard.py`.
9. **Диф соответствует задаче** — файлы совпадают со списком PLAN.md, границы («содержимое PDF/DOCX
   не правит», «theme.css не трогает», «печать DOCX по кнопке не заводит») соблюдены; единственное
   не расписанное явно в критериях решение — параметр `?draft=true` веб-маршрута — исполнитель сам
   обосновал текстом задачи («кнопка… передаёт признак черновика») и прозрачно вынес в notes handoff
   на усмотрение ревью; решение оценено выше как легитимное развитие описанного поведения, а не
   расширение скоупа.
10. **YAML с двоеточием в кавычках** — `.planning/evidence/Э4-И6-handoff.yaml` не содержит
    незакавыченных строк с двоеточием (значения `checks`/`did` в кавычках).

## Грабли §0.1 SPEC.md

- Незаполненных `{{...}}` в задаче нет — промпты не затронуты.
- Просьбы вернуть входные данные модели нет — модели не вызываются вовсе (только файловые операции
  и рендер шаблона).
- `max_tokens` не относится к задаче.
- YAML `Э4-И6-handoff.yaml` — двоеточия в значениях закавычены.
- `extract_json_block`/пустой `content` — не применимо, задача не разбирает ответы моделей.
- Адрес GigaChat — не встречается.

## Разбор handoff исполнителя

- Артефакты существуют: `backend/app/cli.py`, `backend/app/cli_export.py`, `backend/app/web/routes/report.py`,
  `backend/app/web/templates/report.html`, `tests/test_cli_export.py`, `tests/test_web_export.py`,
  `.planning/evidence/Э4-И6-pytest.txt` — все на месте.
- `did` описывает результат (поведение, коды возврата, тексты отказов), не факт занятия.
- `checks` — фактический вывод команд, числа совпадают с тем, что воспроизведено ревью (10/6/27/1045,
  ruff чисто).
- `self_audit: pass` при `escalate: none` — непротиворечиво.
- Примечание о базе worktree не своей ветки (РЕШ-158) — отчёт о решении, а не находка по этой задаче;
  исполнитель исправил ситуацию до начала работы («git switch -c e4/i6 81817bb9…»), к самой задаче
  претензий это не добавляет.

## Советы (не блокируют)

1. Веб-маршрут `download_pdf` пишет PDF в `settings.out_dir` при каждом GET (тот же путь, что и у
   CLI-выгрузки того же запроса в тот же день) — при параллельных запросах на один `request_id` возможна
   гонка записи файла (перезапись тем же содержимым, не мусор и не чужие данные, но при очень
   маловероятном стечении обстоятельств потенциальная короткая гонка чтения не до конца записанного
   файла). Для нынешнего объёма трафика (десятки запросов в месяц) не критично — идея на будущее, не
   для этой задачи.
2. `download_pdf` при `ReportNotFoundError` для строки с некорректным/повреждённым `report_json`
   (не только для по-настоящему отсутствующего запроса) отвечает одинаковым текстом «не найден» —
   то же поведение унаследовано от `show_report` до этой задачи, стиль сообщения не разбирает
   причину. Не блокирует (поведение не новое и не хуже уже существующего), но стоит унифицировать
   в будущей задаче по обработке ошибок отчёта.
