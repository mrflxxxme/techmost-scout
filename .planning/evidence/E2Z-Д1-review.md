# Ревью Д-1 волны Э2-З · цикл 1
Вердикт: ДОРАБОТАТЬ (блокирующих 1, советов 3)

Коммиты: 5251eae (код+тесты), 884549b (handoff и доказательства, отдельным
коммитом — см. раздел «Отступление от процесса» ниже).

## Блокирующие

1. **[инвариант №8] Заявление «briefs пишется и на отказавшем прогоне»
   не покрыто тестом, идущим через реальный код `run_flow.py`.**

   В `run_flow.py` (`except`-ветка вызова `_run_stages`, строка с комментарием
   `# РЕШ-146/156: briefs пишется и на отказавшем прогоне`) добавлен параметр
   `brief=brief` в вызов `persist_run`. Это новое производственное поведение,
   явно вынесенное в заголовок коммита.

   Тест `test_persist_run_writes_brief_row_on_failed_run_too`
   (test_report_persistence.py) действительно проверяет, что
   `persist_run(status="failed", brief=brief)` пишет строку `briefs` — но
   вызывает `persist_run` напрямую, минуя `run_flow.py`. Тест
   `test_pipeline_failure_persists_failed_status_and_error` и
   `test_cost_limit_exceeded_during_scoring_raises_and_persists_failed`
   (test_persistence.py) действительно гоняют реальный отказ через
   `ScoutPipeline.run()`, но проверяют только `RequestRow` — ни один не
   смотрит на таблицу `briefs`.

   Проверено мутацией (снята строка `brief=brief,  # РЕШ-146/156: ...` из
   except-ветки `run_flow.py`, воспроизведено и снято, `git status` чист):
   **полный набор pytest (762 теста) остаётся зелёным** — ни один тест не
   заметил регресс. По протоколу ревью (§ «мутация не убила ожидаемый тест —
   обязателен полный набор») это дыра в гейте, а не стилистическое
   замечание: production-код может незаметно потерять запись брифа на
   отказавшем прогоне, и об этом никто не узнает до экрана Э3-5.

   **Что нужно:** тест уровня `test_persistence.py` (по образцу
   `test_pipeline_failure_persists_failed_status_and_error`), который гонит
   `ScoutPipeline.run()` до отказа стадии ПОСЛЕ интейка и проверяет, что в
   `BriefRow` появилась ровно одна строка с ожидаемыми полями. Один тест
   закрывает разрыв между «механизм умеет» и «механизм подключён».

## Советы (не блокируют)

1. **run_flow.py — ровно 500 строк, край лимита стража.** Страж
   (`test_backend_layout.py`, условие `count > 500`) формально зелёный: 500
   не больше 500. Но это последняя свободная строка — следующая же правка
   файла (а он живёт в горячей точке оркестрации, правки вероятны) начнётся
   с дробления, а не с задачи. Стоит рассмотреть вынос одной функции (кандидат
   — `_run_stages`, который и так логически самостоятелен и мог бы уехать в
   отдельный модуль по образцу уже выделенных `pipeline_search.py` /
   `pipeline_enrichment.py`) сейчас, не дожидаясь, пока лимит станет
   блокирующим сам по себе.

2. **Круговой тест сериализации использует значения по умолчанию для двух
   полей.** `_full_report`/`_full_card` заявлены как «все поля непустыми/
   нестандартными», но `SolutionCard.confidence = Confidence.MEDIUM` и
   `Brief.geography = "РФ"` совпадают со схемными умолчаниями по умолчанию
   (`Confidence.MEDIUM`, `default="РФ"`). Сегодня в схеме нет ни одного
   `exclude=True` или кастомной сериализации, поэтому реальной дыры нет —
   `model_dump_json()` включает все поля независимо от значения. Но при
   будущей правке схемы (например, добавление `exclude` полю) этот тест
   молча перестанет ловить регресс по этим двум полям. Дешёвая правка:
   заменить на нестандартные значения (`Confidence.HIGH`, `geography="США"`).

3. **Отдельный коммит `884549b` для handoff и evidence — отступление от
   правила «тем же коммитом, что и работу» (§13а п. 6).** Причина
   документирована в `notes` handoff: сессия оборвалась на сетевой ошибке
   после того, как код (models.py/persistence.py/run_flow.py) уже был
   отредактирован, но до коммита; реанимация продолжила ту же рабочую копию,
   несохранённые правки подтвердились исправными по `git diff`, довели дело
   до конца вторым коммитом. Работа не потеряна, оба коммита в ветке
   присутствуют и оба до ответа ревьюеру — по факту деливерабл цел. Прецедент
   такого же неблокирующего решения уже есть в волне Э2-Ж. Не блокирую, но
   отмечаю: если реанимация происходит второй раз подряд у одного агента —
   по §13а п. 4 второй реанимации не полагается, нужен свежий агент.

## Проверено

- **Сериализация обратима (Report → JSON → Report).** Прочитан
  `test_report_round_trips_through_json_without_field_loss`: `_full_report`
  строит `Report` со всеми 9 полями класса заполненными не по умолчанию (кроме
  двух исключений — см. совет 2), включая вложенные `Brief` (все 9 полей),
  `SolutionCard` (все 18 полей, включая `evidence`, `gov_support`,
  `requirement_checks`, `unsourced_claims`, `pdf_urls`) и `ReportSummary`
  (все 3 поля). Сверено полями класса `Report`/`Brief`/`SolutionCard` в
  `schemas.py` — пропусков нет.
- **briefs на отказе после интейка.** Заявлено, механизм в
  `persistence._write` корректен и покрыт юнит-тестом на уровне `persist_run`
  напрямую; интеграционного теста через `run_flow.py` нет — см. блокирующее
  №1.
- **load_report.** Возвращает типизированный `Report`
  (`Report.model_validate_json`), не сырой `dict`; `ReportNotFoundError` на
  отсутствии фабрики, строки, пустом `report_json`, битом JSON. Отказ НЕ
  гасится — исключение поднимается наружу с текстом на русском. Обоснование
  (это чтение для экрана Э3-5 по прямому запросу, а не адаптер источника,
  РЕШ-43 к нему не относится) корректно: РЕШ-43 и инвариант №5 адресуют отказ
  ВНЕШНЕГО ИСТОЧНИКА в конвейере, который не должен ронять прогон; здесь же
  прогон уже завершён, читает оператор по явному запросу, скрывать нечего и
  незачем.
- **run_flow.py = 500 строк.** `wc -l`: 500 ровно. Страж
  `test_backend_layout.py::test_backend_app_stays_within_line_limit` и
  `test_exceptions_do_not_grow_unnoticed` зелёные (файл не в списке
  исключений, условие `count > 500` не срабатывает на равенстве). См. совет 1.
- **ALTER-команды (`E2Z-Д1-alter.sql`) — проверены на КОПИИ живой базы
  самостоятельно**, не на слово:
  - `cp data/scout.db` во временный файл; `pragma table_info` ДО правки:
    `requests` — 4 строки, без `report_json`; `briefs` — 0 строк, без
    `owner_answers`. Совпадает с комментарием в SQL-файле.
  - `ALTER TABLE requests ADD COLUMN report_json TEXT;` — применена,
    `pragma table_info` после: колонка `report_json TEXT`, `notnull=0`
    (nullable) — как заявлено.
  - `ALTER TABLE briefs ADD COLUMN owner_answers JSON NOT NULL DEFAULT '[]';`
    — применена, `pragma table_info` после: `owner_answers JSON`,
    `notnull=1`, `dflt_value="'[]'"` — как заявлено.
  - **Не повторяет инцидент Э2-Ж** (там `NOT NULL` без `DEFAULT` ронял
    `INSERT` на непустой таблице): здесь `DEFAULT '[]'` присутствует на
    уровне БД, и `briefs` пуста (0 строк) на момент ALTER — колонка
    `NOT NULL` безопасна даже без записи через ORM.
  - Проверил и ORM-путь: вставка `BriefRow(...)` БЕЗ явного `owner_answers`
    (полагаясь только на `default=list` модели) на изменённой копии базы —
    строка пишется, `owner_answers == []`. Отдельно проверил и путь мимо ORM
    (`INSERT` сырым SQL без колонки `owner_answers`) — БД сама подставляет
    `'[]'` через DEFAULT. Оба пути безопасны. В боевом коде `_write` всегда
    передаёт `owner_answers` явным списком (пустым или заполненным), так что
    расхождение теоретическое, но и оно не ломается.
- **Мутация (а) report_json не пишется.** Воспроизведена (закомментирована
  строка присвоения `request_row.report_json = ...`), прогнаны
  `tests/test_report_persistence.py tests/test_persistence.py`: 2 failed
  (`test_persist_run_writes_report_json_and_load_report_reads_it_back`,
  `test_successful_run_persists_report_json_and_brief_row`) — мутация поймана
  ровно теми тестами, что заявлены в докстроках. Восстановлено
  (`git checkout`), `git status` чист.
- **Мутация (б) load_report возвращает сырой dict.** Воспроизведена
  (`return json.loads(stored)` вместо `Report.model_validate_json(stored)`),
  прогнаны те же файлы: 2 failed на `isinstance(loaded, Report)`. Поймана.
  Восстановлено, `git status` чист.
- **Числа.** `pytest -q --collect-only`: 762 теста собрано (было 753 на базе
  волны, `.planning/evidence/E2Z-baseline.txt`); прирост 9 = 8
  (test_report_persistence.py) + 1 (test_persistence.py) — сходится.
  `pytest -q`: 762 passed, 0 failed, 44.8 с. `ruff check .`: All checks
  passed. `ruff format --check .`: 137 files already formatted.
  `wc -l backend/app/pipeline/persistence.py`: 496 (≤ 500).
- **Инварианты 1–10:**
  - №1 (evidence) — не задет, задача не трогает карточки/evidence.
  - №2 (черновик/автоотправка) — не задет, веб-маршруты вне границ задачи, не
    тронуты (проверено diff'ом).
  - №3 (секреты) — `grep -rE "sk-|gigachat|Api-Key" backend/ tests/`: только
    предсуществующий код (`llm/yandex.py`, `search/yandex_search.py`,
    существующие тесты секретов) — ничего нового в diff'е задачи; значения не
    печатались нигде в моих командах.
  - №4 (подстановки `{{...}}`) — не задет, промпты не тронуты.
  - №5 (отказ источника) — не задет напрямую; `load_report` разобран отдельно
    выше и признан корректным (не адаптер источника).
  - №6 (прямой httpx) — `grep -n "httpx" backend/app/models.py
    backend/app/pipeline/persistence.py backend/app/pipeline/run_flow.py`:
    пусто.
  - №7 (сеть в тестах) — новые тесты используют временный файл SQLite
    (`tmp_path`) и уже проверенный сетевой фейк `_OneHitAdapter`
    (существующий в test_persistence.py, задачей не создан); прямых сетевых
    вызовов нет.
  - №8 (новое поведение без теста) — **нарушен**, см. блокирующее №1.
  - №9 (скоуп) — `git diff 5d6e3f0 884549b --stat`: только заявленные файлы
    (models.py, persistence.py, run_flow.py, два тестовых файла) плюс
    evidence/handoff; веб-маршруты не тронуты, экспорт markdown не тронут —
    границы задачи соблюдены.
  - №10 (YAML с двоеточием) — `pytest tests/test_team_docs.py`: 35 passed.

## Handoff

```yaml
handoff:
  from: reviewer
  to: implementer
  task: "Д-1"
  did:
    - "Проверены diff, все 10 инвариантов, ALTER на копии живой базы (свой прогон, не на слово), 2 обязательные мутации (а/б) — обе пойманы, полный pytest и ruff"
    - "Найдено 1 блокирующее (инвариант №8: briefs-на-отказе не покрыт тестом через реальный run_flow.py — доказано мутацией и полным набором), 3 совета"
  artifacts:
    - .planning/evidence/E2Z-Д1-review.md (закоммичен до ответа)
    - .planning/evidence/E2Z-Д1-review-handoff.yaml
  self_audit: pass
  checks:
    - "pytest -q: 762 passed, 0 failed"
    - "pytest -q --collect-only: 762 tests collected (753 база + 9 новых, сходится)"
    - "ruff check .: All checks passed"
    - "ruff format --check .: 137 files already formatted"
    - "мутация 'brief=brief убран из except-ветки run_flow.py': полный pytest 762 passed — регресс не пойман никем (доказательство блокирующего №1)"
    - "мутация (а) report_json не пишется: 2 failed из test_report_persistence.py/test_persistence.py — поймана"
    - "мутация (б) load_report возвращает dict: 2 failed на isinstance — поймана"
    - "ALTER на копии data/scout.db: requests.report_json TEXT nullable, briefs.owner_answers JSON NOT NULL DEFAULT '[]' — оба применились и совпали с pragma table_info из alter.sql; ORM-вставка без явного owner_answers тоже безопасна (default=list)"
    - "wc -l backend/app/pipeline/run_flow.py: 500 (страж зелёный, но на самом краю — совет 1)"
  escalate: none
  read_first:
    - .planning/PLAN.md (задача Д-1, критерии и мутации)
    - backend/app/pipeline/run_flow.py (except-ветка persist_run, строка brief=brief)
    - tests/test_persistence.py (test_pipeline_failure_persists_failed_status_and_error — образец для недостающего теста)
    - docs/team/INVARIANTS.md (№8)
  notes: >
    Это цикл 1. Второй цикл — последний, дальше третьего не будет.
    Блокирующее узкое: нужен один интеграционный тест (BriefRow после
    реального отказа ScoutPipeline.run после интейка), не переделка кода.
    Отдельный коммит handoff (884549b) не блокирует — прецедент Э2-Ж, см.
    совет 3 в отчёте.
```
