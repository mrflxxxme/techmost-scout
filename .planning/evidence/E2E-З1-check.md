# Ревью + верификация З-1 · цикл 1
Вердикт: ПРИНЯТЬ

## Что проверено по существу

### (а) Порядок и размер батча совпадают с боевым
Проследил цепочку боевого прогона по коду (не по описанию):
`run_flow.py:241` `unique = stages.deduplicate(outcome.candidates)` →
`run_flow.py:257` `scored = await stages.score_candidates(router, brief, unique, citations=citations)` →
`run_flow.py:337` `candidate_log.extend(candidate_records(scored, picked))`.

- `deduplicate()` (`extraction.py:292`) держит порядок первого попадания ключа в
  `dict` (Python-словарь сохраняет порядок вставки; замена «лучшего» кандидата
  по ключу не двигает позицию) — порядок совпадает с порядком, в котором
  кандидаты дошли до дедупликации.
- `score_candidates()` (`scoring.py:85-198`) не переупорядочивает: внутри батча
  `for position, candidate in enumerate(chunk): scored.append(...)`, при отказе
  батча `scored.extend(_neutral_scores(chunk))` — тоже в порядке `chunk`. Итог:
  выходной `scored` строго повторяет порядок входного `unique`.
- `candidate_records(scored, selected, pass_no)` (`pipeline_selection_flow.py:84-92`)
  строит список `CandidateRecord` в порядке `scored`, ничего не сортируя.
- `persistence.py:258` `for record in candidates: session.add(CandidateRow(...))`
  — вставка в БД в порядке списка; PK `id` без явного значения — SQLite
  назначает его по возрастанию в порядке вставки внутри одной транзакции.

Вывод: `ORDER BY id` в `load_candidates` скрипта действительно восстанавливает
порядок дедупликации боевого прогона. Правило РЕШ-161 в коде скрипта
(`scripts/rescore_probe.py:123-166`, докстрока `load_candidates`) соответствует
факту, а не только формулировке. Батч 10 — константа `BATCH_SIZE = 10`, та же,
что `scoring.py:90` (`batch_size: int = 10`), нарезка срезами идентична
(`batches_of` vs. `range(0, len(candidates), batch_size)`).

### (б) Зовётся боевой код
`grep -n "LLMRouter" scripts/rescore_probe.py` — импорт и создание из
`app.llm`; `run_live()` вызывает `app.pipeline.stages.score_candidates` —
ту же функцию, что орекстратор (реэкспорт из `app.pipeline.scoring`).
`prompts/scorer.md` не тронут: `git diff b1e9c36..1cf05ac -- prompts/scorer.md`
— пусто. Своей реализации скоринга нет — скрипт только собирает батчи и
печатает.

### (в) `--dry-run` ничего не стоит
Код: при `args.dry_run` возврат до создания `LLMRouter` (`_main_async`,
строки 291-293); тест `test_dry_run_не_создаёт_router_и_печатает_состав_батчей`
подменяет `app.llm.LLMRouter` на функцию, которая падает `AssertionError` при
вызове, и подтверждает, что она не вызывается.

Прогнал сам на реальной базе:
```
$ /home/user/techmost-scout/.venv/bin/python scripts/rescore_probe.py \
    --db /home/user/techmost-scout/data/scout.db --request 1 \
    --brief-file <бриф-заготовка> --dry-run
кандидатов в пуле: 112
размер батча: 10 (как в бою, scoring.py:90)
батчей: 12 — столько же будущих вызовов модели
  батч 1/12 (10 шт.) ... батч 12/12 (2 шт.)
```
112 кандидатов, 12 батчей (11×10 + 1×2) — совпадает с заявленным в handoff.
База до и после прогона: `size=950272 mtime=1787495575`,
`md5=4e09b217c4d817ac665f3a581e5e0b63` — идентичны, запись не производилась.

### (г) Сеть и секреты
`grep -n "httpx" scripts/rescore_probe.py` — совпадений нет.
`grep -rE "sk-|gigachat|Api-Key" scripts/rescore_probe.py tests/test_rescore_probe.py`
находит только фиктивный тестовый ключ `sk-fake-leak-check-777`
(`monkeypatch.setenv`) — не боевой секрет, используется намеренно для проверки
самого механизма нечувствительности к утечке.

Проверил, что тест на утечку ловит регрессию: временно вставил в
`print_summary` строку `print("DEBUG_KEY_LEAK", os.environ.get("DEEPSEEK_API_KEY"))`
и прогнал `pytest tests/test_rescore_probe.py -k живой_прогон` — тест покраснел:
`AssertionError: assert 'sk-fake-leak-check-777' not in ...` с явным попаданием
строки ключа в перехваченный stdout. Дерево возвращено `git checkout --
scripts/rescore_probe.py`, `git status --porcelain` пусто — рабочая копия чистая.

### (д) Мутации карточки — воспроизведены независимо все четыре прицельным прогоном `pytest tests/test_rescore_probe.py`
- (а) `BATCH_SIZE = 10` → `20`: упали 4 теста (`test_batches_of_режет...`,
  `test_batches_of_размер_батча_фиксирован_10`,
  `test_run_live_батчи_порядок_и_citations_none`,
  `test_dry_run_не_создаёт_router_и_печатает_состав_батчей`).
- (б) добавлена сортировка `candidates.sort(key=lambda c: c.title)` в
  `load_candidates`: упал `test_load_candidates_упорядочивает_по_id_и_фильтрует_по_request`.
- (в) `citations=None` заменён на `citations={'x': 1}` в `run_live`: упал
  `test_run_live_батчи_порядок_и_citations_none`.
- (г) `_readonly_uri` перестала добавлять `?mode=ro`: упал
  `test_readonly_uri_содержит_mode_ro_и_запись_отказывает`.

После каждой мутации дерево восстановлено (`git checkout --
scripts/rescore_probe.py`), `git status --porcelain` пуст в финале.

### (е) Границы
`git diff b1e9c36..1cf05ac --stat` — только `scripts/rescore_probe.py`,
`tests/test_rescore_probe.py`, `.planning/evidence/E2E-З1-handoff.yaml`.
`backend/app` и `prompts/` не тронуты (`git diff b1e9c36..1cf05ac --
backend/app` — пусто). Подкоманды `scout rescore` нет: `backend/app/cli.py`
не изменён.

### (ж) Полный набор тестов и ruff
```
$ /home/user/techmost-scout/.venv/bin/python -m pytest -q
714 passed, 1 warning in 37.82s
```
Совпадает с заявленным (698 базовых + 16 новых).

```
$ /home/user/techmost-scout/.venv/bin/python -m ruff check scripts/rescore_probe.py tests/test_rescore_probe.py
All checks passed!
$ /home/user/techmost-scout/.venv/bin/python -m ruff format --check scripts/rescore_probe.py tests/test_rescore_probe.py
2 files already formatted
```
Полный `ruff check .` даёт 15 замечаний в `docs/marketing/deck/*.py`
(семиколоны, mixedCase-переменная) — это довесок слияния маркетинговых
материалов до старта задачи (РЕШ-165 выводит `docs/marketing` из-под
линтера у оркестратора; копия исполнителя старше этого решения). К файлам
З-1 отношения не имеет, не блокирует.

## Инварианты (все десять)
1. Факты со ссылкой — не применимо: скрипт не производит карточек с фактами.
2. ЧЕРНОВИК/автоотправка — не применимо: скрипт вне поставки продукта.
3. Секреты — соблюдён (см. (г)); `.env` в `.gitignore` (проверено).
4. `render_prompt`/`EXPECTED_PLACEHOLDERS` — не применимо, промпт не тронут,
   `tests/test_prompts.py` не задет.
5. Отказ источника → пустой список + `sources_unavailable` — не применимо,
   скрипт не источник; отказ батча скоринга обрабатывается в самой боевой
   стадии (`score_candidates`, не в скрипте) — тем же путём, что и в бою.
6. `grep -n "httpx" scripts/rescore_probe.py` — пусто; вызов модели —
   исключительно через `LLMRouter`. Соблюдён.
7. `pytest -q` — 714 passed, без сети (respx/подмены `FakeProvider` в
   `tests/test_rescore_probe.py`). Соблюдён.
8. Изменений в `backend/` нет, значит требования «есть тесты» не возникает;
   для `scripts/` тесты есть (16 шт.). Соблюдён.
9. Diff сверен с задачей и разделом «Границы» карточки — совпадает, выхода
   за рамки нет.
10. `pytest tests/test_team_docs.py` — 35 passed; `.planning/evidence/E2E-З1-handoff.yaml`
    парсится `yaml.safe_load` без ошибок, двоеточия в значениях отсутствуют.

## Handoff исполнителя
`did` содержит результат, а не процесс; `checks` — фактический вывод команд,
воспроизведён независимо и совпал (714 passed, dry-run 112/12, ruff чисто).
`self_audit: pass` при `escalate: none` — непротиворечиво. Артефакты
(`scripts/rescore_probe.py`, `tests/test_rescore_probe.py`) существуют и
закоммичены (`9ced099`).

## Советы (не блокируют)
1. `provider_and_model` печатает «назначенный» провайдер, а не тот, что
   реально ответил (роутер может уйти на резерв) — уже честно оговорено
   докстрокой функции, ничего добавлять не нужно.
2. `docs/marketing/deck/*.py` не проходит `ruff` — не имеет отношения к
   задаче, требует отдельной задачи и решения владельца по итогам РЕШ-165
   (границы линтера для `docs/marketing`).

## Проверено (сводка команд)
- `pytest -q`: 714 passed
- `pytest tests/test_rescore_probe.py -v`: 16 passed
- `ruff check scripts/rescore_probe.py tests/test_rescore_probe.py`: All checks passed
- `ruff format --check scripts/rescore_probe.py tests/test_rescore_probe.py`: 2 files already formatted
- `pytest tests/test_team_docs.py`: 35 passed
- Живой `--dry-run` на `data/scout.db --request 1`: 112 кандидатов, 12 батчей (11×10+1×2); БД не изменена (размер/mtime/md5 идентичны до и после)
- 4/4 мутации карточки воспроизведены независимо, все убивают ожидаемые тесты
- Инвариант №3: тест на утечку ключа проверен на регрессию (временная правка ловится, дерево восстановлено, `git status --porcelain` пуст)
