# Ревью+верификация З-10 · цикл 1
Вердикт: ПРИНЯТЬ (блокирующих 0, советов 1)

## Сверка переноса по существу (пункт а)
Из старого `orchestrator.py` (`git show d51d9cf:backend/app/pipeline/orchestrator.py`,
1203 строки) выписаны все 23 def/class. Все 21 функция + 2 класса найдены
ровно по одному разу в новых модулях (без пропусков и дублей):

- `cost_delta`, `format_active_run_params` → `run_journal.py`
- `_unwrap_group`, `SearchOutcome`, `_get_adapters`, `adapters`, `aclose`,
  `gather_candidates`, `_safe_search`, `_extract`, `_second_pass` → `pipeline_search.py`
- `_apply_patch`, `enrich_candidates` → `pipeline_enrichment.py`
- `split_contest_candidates`, `candidate_records`, `_cut_by_relevance` → `pipeline_selection_flow.py`
- `__init__`, `_notify` → `pipeline_context.py`
- `_author_cards` → `run_cards.py`
- `run`, `_run_stages`, `_empty_report` (класс `ScoutPipeline` собран
  мультинаследованием миксинов) → `run_flow.py`

Тела всех функций сверены построчно (diff по извлечённым фрагментам,
пробелы/пустые строки нормализованы). Единственные расхождения:
- убраны декоративные разделители-комментарии секций (`# ── Обогащение по
  DOI ──`, `# ── Полный проход ──`), которые описывали границу кода,
  переехавшего в отдельный файл — смысловой нагрузки не несут;
- три константы (`_PATCHABLE_FIELDS`, `CONTEST_CARDS_TARGET`,
  `SECOND_PASS_NO`) переехали к месту использования, значения не менялись
  (проверено `grep`);
- добавлены три блока отложенного импорта (`build_adapters` в
  `pipeline_search.py`, `persist_run` и `PAID_LIMIT_NOTICE` в `run_flow.py`) —
  ожидаемая и необходимая правка для сохранения monkeypatch по атрибуту
  фасада (см. ниже).

**Смысловых расхождений в перенесённой логике не найдено.**

## Фасад (пункт б)
`orchestrator.py` — 51 строка, ни одного `def`/`class`
(grep `^def |^class |^    def |^async def` — пусто). Реэкспортирует
`ScoutPipeline, ProgressHook, SearchOutcome, CONTEST_CARDS_TARGET,
SECOND_PASS_NO, split_contest_candidates, candidate_records, cost_delta,
format_active_run_params, build_adapters, persist_run, PAID_LIMIT_NOTICE,
normalize_doi`. Грепом по всему дереву (`backend`, `tests`) собраны все места
импорта из `app.pipeline.orchestrator` — все нужные имена присутствуют,
включая `tests/test_contest_section_flow.py` (`CONTEST_CARDS_TARGET`,
`split_contest_candidates`), `tests/test_cli_run.py` (monkeypatch
`build_adapters` по строке), `tests/test_openalex.py` (monkeypatch
`persist_run`), `tests/test_partial_source_failure.py` (monkeypatch
`PAID_LIMIT_NOTICE`). `backend/app/pipeline/__init__.py` не тронут и
продолжает работать через фасад без изменений.

## Грабля 3891b08 — монkeypatch и логгер (пункт в)
Три точки монkeypatch проверены мутацией и прогоном:
- **build_adapters**: сохранён отложенный импорт `from
  app.pipeline.orchestrator import build_adapters` внутри `_get_adapters`.
- **persist_run**: сохранён отложенный импорт внутри `run()`. Проверено
  двойной мутацией: (1) штатный прогон — `test_openalex.py
  test_обогащение_встроено_между_дедупликацией_и_скорингом` зелёный, монkeypatch
  `orchestrator.persist_run` реально подменяет вызов; (2) контрольная мутация —
  заменил отложенный импорт на прямой `from app.pipeline.persistence import
  persist_run` наверху `run_flow.py` → тест немедленно падает
  (`KeyError: 'stages'`, монkeypatch перестал долетать). Дерево восстановлено,
  `git status --porcelain` пуст.
- **PAID_LIMIT_NOTICE**: аналогичный отложенный импорт внутри `run()`,
  задействован `tests/test_partial_source_failure.py`.

**Логгер — независимая мутация, не убитая ни одним тестом.** Задание просило
заменить логгер на `__name__` в одном подмодуле и ожидало падения тестов,
«фильтрующих caplog по имени». Проверено: замена `logging.getLogger(
"app.pipeline.orchestrator")` на `logging.getLogger(__name__)` в
`pipeline_search.py` **не уронила ни одного теста** — прогнан весь набор
(698 passed). Для очистки картины та же замена сделана одновременно во всех
пяти подмодулях (`pipeline_context.py`, `pipeline_enrichment.py`,
`run_cards.py`, `run_flow.py`, `pipeline_search.py`) — тоже 698 passed.
Причина выяснена минимальным экспериментом: `caplog.at_level(level,
logger=...)` в pytest выставляет уровень указанного логгера на время
контекста, но **не ограничивает** `caplog.records`/`caplog.text` этим
логгером — записи любого логгера, дошедшие до root, попадают в захват
одинаково. Ни один тест в наборе не фильтрует `record.name` вручную (проверено
`grep -rn "record.name"`). Тот же эксперимент на `cards.py` (логгер
`"app.pipeline.stages"`, грабля 3891b08 в предыдущем дроблении) дал тот же
результат — 698 passed. **Это не регрессия задачи З-10**: явная строка
логгера действительно нужна для совпадения реального имени логгера в проде
(поведение сохранено буквально — было бы `app.pipeline.orchestrator` и до
дробления, раз `__name__` файла `orchestrator.py` равнялся этой строке), но
заявление исполнителя «тесты фильтруют caplog ровно по этой строке» неточно:
де-факто это ничем не проверяется, и тот же незамеченный пробел существовал
уже в принятом дроблении `stages.py`. Дерево после экспериментов
восстановлено, `git status --porcelain` пуст. Вынесено советом ниже, не
блокирует: не регрессия этой задачи, а системный пробел покрытия,
существовавший до неё.

## Тесты (пункт г) и объём файлов (пункт д)
`git diff --name-only d51d9cf..c05d949 -- tests/` → ровно
`tests/test_pipeline_layout.py` (добавлены 3 теста по образцу теста для
`stages.py`, старые тесты не менялись). Подсчёт строк:
`orchestrator.py` 51, `pipeline_context.py` 82, `pipeline_search.py` 417,
`pipeline_enrichment.py` 170, `pipeline_selection_flow.py` 109, `run_cards.py`
59, `run_flow.py` 477, `run_journal.py` 58 — все ≤ 500, фасад ≤ 120.

## git reset --hard (пункт е)
`git merge-base d51d9cf HEAD` == `d51d9cf` — на ветке ровно один коммит
(`c05d949`) поверх верхушки волны, локальной истории не потеряно.
Присутствуют артефакты предыдущих задач волны: `prompts/extractor.md` версии
2.0/2.0.1, `tests/test_partial_source_failure.py`,
`tests/test_run_visibility.py`, `tests/test_fallback_without_guesses.py`,
`tests/test_run_accounting.py`. Находка исполнителя за пределами задачи
(оснастка worktree создавала копию не от актуального HEAD) зафиксирована в
его handoff — по существу подтверждена, тиражирования проблемы не обнаружено.

## Инварианты 1–10
Задача — чистый перенос кода внутри пакета `app.pipeline`, не затрагивает
карточки, промпты, БД и клиентский отчёт — инварианты №1, №2, №4, №9, №10
неприменимы по объёму правки и не нарушены. №3 — `grep -rE "sk-|gigachat|
Api-Key" backend/ tests/`: только имена настроек и переменных окружения,
значений нет; `.env`/`.env.*` в `.gitignore`. №5 — обработка отказов
источников (`_safe_search`, `SearchError`) перенесена дословно, поведение не
менялось. №6 — `grep -rn "httpx" backend/app`: `pipeline_search.py` и
`pipeline_context.py` (создание общего `httpx.AsyncClient`, было в
оркестраторе и раньше), плюс `llm/`, `search/`, `cli.py` — новых мест не
появилось. №7 — сеть не используется, respx-мок не менялся. №8 — новых
поведенческих изменений нет, поэтому новых поведенческих тестов не требуется;
страж раскладки расширен под новую структуру.

## Проверено
- `.venv/bin/python -m pytest -q`: **698 passed, 0 failed** (заявлено то же)
- `ruff check .`: All checks passed
- `ruff format --check .`: 128 files already formatted
- `git diff --name-only d51d9cf..c05d949 -- tests/`: ровно `test_pipeline_layout.py`
- `git diff --name-only d51d9cf..c05d949 -- backend/`: 8 файлов пакета `pipeline`, все в списке задачи
- `git merge-base d51d9cf HEAD` == `d51d9cf`
- Мутация (а, def в фасаде): `test_orchestrator_module_has_no_definitions` падает — воспроизведено независимо
- Мутация (б, run_journal.py 508/559 строк): `test_orchestrator_modules_stay_within_line_limit` падает — воспроизведено независимо
- Мутация (в, `split_contest_candidates` не реэкспортирован): `ImportError` при коллекции `test_contest_section_flow.py` — воспроизведено независимо
- Дополнительная мутация (логгер → `__name__`, все 5 подмодулей): полный набор 698 passed — **ни один тест не поймал**, причина — механика `caplog.at_level(logger=...)` в pytest не фильтрует захват; тот же пробел воспроизведён на `cards.py`/`stages.py` (грабля 3891b08). Не блокирует — не регрессия задачи, найден системный пробел покрытия.
- Дополнительная мутация (прямой импорт `persist_run` вместо отложенного): `test_обогащение_встроено_между_дедупликацией_и_скорингом` падает (`KeyError: 'stages'`) — подтверждает, что приём отложенного импорта действительно нужен, а не декоративен
- После всех мутаций: `git status --porcelain` пуст, дерево восстановлено

## Советы (не блокируют)
1. Утверждение в `did` handoff-а («тесты фильтруют caplog ровно по этой
   строке») фактически не подтверждается: pytest `caplog.at_level(logger=…)`
   не ограничивает захват записей этим логгером, и ни один тест в наборе не
   проверяет `record.name`. Явная строка логгера всё равно правильна (сохраняет
   реальное имя логгера в проде), но опору на «тесты поймают» стоит считать
   иллюзорной. Стоит завести отдельный технический долг — добавить хотя бы
   один тест с явной проверкой `record.name == "app.pipeline.orchestrator"`
   для `pipeline_search.py`/`run_flow.py`, и заодно для `cards.py`/`stages.py`,
   где тот же пробел существовал до этой задачи.
