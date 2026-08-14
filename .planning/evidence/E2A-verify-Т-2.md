# Верификация Т-2

**Вердикт: PASS**

Рабочая копия `.claude/worktrees/E2A-T2`, ветка `E2A/T2`, HEAD на момент
проверки `b299ab1` (в задании значился `a69250e`; сверху легли два коммита —
отчёт ревью цикла 2 и слияние. Дифф задачи не изменился, но один из них повлиял
на критерий 4, см. ниже).

## Критерии приёмки

| № | Критерий | Как проверен | Фактический вывод | Статус |
|---|---|---|---|---|
| 1 | `pytest tests/test_cli_run.py -v` зелёный; подменённый `run_intake` бросает исключение → прогон доходит до отчёта, бриф из сырого текста, предупреждение в консоли | Прогон файла плюс разбор тела теста по существу | `10 passed in 0.39s` | **взят** |
| 2 | `pytest tests/test_pipeline_resilience.py -v` зелёный; ответы владельца в `constraints`; строка `intake` с непустым `error`; `CostLimitExceededError` роняет прогон | Прогон плюс разбор четырёх новых тестов и сверка со статусами в `orchestrator.py` | `10 passed in 2.12s` | **взят** |
| 3 | `pytest -q` — все проходят (217 → 225) | Полный прогон | `225 passed, 1 warning in 32.77s` | **взят** |
| 4 | `ruff check . && ruff format --check .` — 0 замечаний | Обе команды по репозиторию, отдельно по пяти файлам задачи и с исключением `.planning` | `ruff check .` → `All checks passed!`; `ruff format --check .` → **exit 1** на файле вне границ задачи | **взят по коду задачи**, расхождение вынесено как DLG-1 |

## Проверка по существу

**«Прогон доходит до отчёта» — не «вызов не упал».** Тест доводит `cli._run` до
конца и утверждает `report_path.exists()`. В прогоне с `-s` виден фактический
вывод: `Отчёт сохранён: ...report.md`, `Стоимость запроса: 0.00 ₽ за 0 вызовов
модели`. Дополнительно утверждается `brief.task_summary == "сырой текст задачи
про катализатор"` и `brief.industry == "не указана"` — бриф собран из сырого
текста, а не подставлен заглушкой. Предупреждение проверено по `capsys`:
фактическая строка — `Предупреждение: интейк не удался, бриф собран из текста
задачи без уточнений`. Отдельно тест запрещает вызов `refine_brief` в этой
ветке через `fail_refine_brief`, бросающий `AssertionError`.

**«Ответы владельца сохраняются» — и вопрос, и ответ.** Утверждение точное, а
не на непустоту: `assert brief.constraints == ["Какая температура процесса? —
380 °C"]`. Пустая строка, потерянный вопрос или потерянный ответ его не
переживают. Код собирает список и дописывает к существующим `constraints` через
`model_copy(update=...)`, то есть не затирает ограничения черновика.

**«Строка `intake` с непустым `error`, статус `drafted`».** Тест идёт в
настоящую базу (SQLite на `tmp_path`, схема из `Base.metadata`) и утверждает
три вещи: ровно одна строка `requests` со `status == "drafted"`, ровно одна
строка `pipeline_logs` со `stage == "intake"`, и `error` непуст.

Проверено, что `drafted` здесь — **реальный различитель, а не константа**:
`orchestrator.py:196-214` пишет `status="failed"` при исключении из
`_run_stages` и `status="drafted"` только при успешном проходе. Пара «`drafted`
плюс непустой `error` в `pipeline_logs`» доказывает ровно требуемое: прогон
дошёл до отчёта, но отказ интейка виден и не растворился в тихом успехе.

Цепочка от `cli._run` до БД покрыта на стыке: тест отказа `refine_brief`
утверждает, что `cli` передаёт в пайплайн `initial_stage_log` длиной 1 со
`stage == "intake"` и непустым `error`; тест БД утверждает, что переданный
`StageRecord` доезжает до `pipeline_logs`.

**`CostLimitExceededError`.** Покрыты оба места вызова. Оба теста утверждают не
только `pytest.raises`, но и `captured == []`: прогон не просто бросил
исключение — он не дошёл до пайплайна и не потратился дальше. В коде это
`except CostLimitExceededError: raise` **до** широкого `except Exception` в
обеих ветках.

## Выживающий мутант — независимое суждение

Воспроизведён без правки репозитория: плагин pytest в скретчпаде подменяет
`stages._FALLBACK_SUMMARY_LIMIT` в `pytest_sessionstart`.

```
MUTANT_LIMIT=500 → 1 passed      (базовая линия)
MUTANT_LIMIT=499 → 1 passed      ← мутант выживает
MUTANT_LIMIT=498 → 1 failed: assert len(summary) == 500
MUTANT_LIMIT=450 → 1 failed: assert len(summary) == 500
```

498 и 450 убиты — контроль, доказывающий, что подмена применяется и тест на
предел чувствителен. Выживает ровно и только 499.

Причина установлена, и она **не в слабости утверждений**. Вход подобран так,
что знак 499 — пробел, а 498 — точка. При таком входе `text[:500].rstrip()`
даёт побайтово то же, что `text[:499]`: `rstrip()` съедает ровно тот знак, на
котором расходятся два предела. На этом входе мутант **эквивалентен** —
различать нечего. Глобально он не эквивалентен (на тексте ровно в 500 знаков
без хвостового пробела пределы расходятся), но на выбранном — да.

**Вывод: на выполнение критерия приёмки 1 мутант не влияет.** Критерий требует
трёх вещей — прогон доходит до отчёта, бриф из сырого текста, предупреждение в
консоли. Точный предел обрезки в нём не назван; нет его и в блоке «Что делать»
плана, где сказано только «`task_summary` из обрезанного сырого текста».
Значение 500 — задокументированный выбор реализации, а не контракт задачи.
Наблюдаемый контракт (обрезка есть, результат является началом исходного
текста, оканчивается многоточием, висящего пробела нет) тест держит и
мутациями 498/450 подтверждает. Классификация ревью цикла 2 верна, повода
понижать вердикт нет.

## Инварианты и границы

**№1.** Проверено исполнением: `brief_from_raw_text('текст задачи')` даёт
`{'industry': 'не указана', 'task_summary': 'текст задачи', 'constraints': [],
'target_maturity': None, 'geography': 'РФ', 'budget_hint': None,
'timeline_hint': None, 'keywords_ru': [], 'keywords_en': []}`. Заполнены только
отрасль из подсказки оператора и суть из сырого текста; `geography: 'РФ'` —
умолчание схемы `Brief`, а не домысел функции. Ключевые слова, зрелость,
бюджет, сроки не выдумываются. Пустой либо целиком пробельный текст даёт
`«не указано»`.

**№3.** Обе строки консоли — статические литералы без интерполяции настроек.
Поиск по `backend/` и `tests/` даёт только имена настроек и заголовков с
обращением через `.get_secret_value()`; захардкоженных значений нет.

**№5 в редакции РЕШ-43.** Отказ виден по трём каналам одновременно:
предупреждение оператору, `log.warning`, строка `intake` с текстом ошибки в
`pipeline_logs`. `CostLimitExceededError` не гасится вовсе.

**№7.** 225 тестов зелёные; провайдер модели подменён, поисковые адаптеры —
заглушки, `ScoutPipeline` — записывающая заглушка, `init_db` — пустышка, база —
временный SQLite. Боевой `data/scout.db` не задействован.

**№8.** Три файла в `backend/` изменены вместе с двумя в `tests/`; +8
тест-кейсов, арифметика 217 → 225 сходится. Блокирующее ревью цикла 1 закрыто
именно тестами.

**№9.** Ровно пять файлов из карточки. `schemas.py` не тронут, `backend/app/web/`
не тронут (Э3-2 не задет), маршрут стадии `intake` не менялся — дифф `stages.py`
состоит только из константы и новой функции (РЕШ-46 соблюдён), разбиения
`stages.py` нет.

Побочно, не находка по Т-2: `cli.py` содержит `import httpx`, что формально
шире формулировки инварианта №6. Строка контекстная, в дифф задачи не входит.

**Записей в репозиторий верификатор не делал:** `git status --porcelain` пуст,
пробы лежат в скретчпаде.

## Отложенность DLG-1 — закрыта оркестратором

`ruff format --check .` по репозиторию падал на
`.planning/evidence/E2A-review-Т-2-cycle2.md:58`: ruff 0.16.2 форматирует
питоновские блоки внутри markdown, а каталог `.planning` не был исключён.

Причина вне границ Т-2 — файл создан оркестратором коммитом `ec11232` уже после
реализации; исполнитель его не писал и по карточке писать не мог, верификатору
правка запрещена. Без решения та же команда валила бы критерий 4 у Т-3, Т-4,
Т-6 и Т-7.

**Закрыто оркестратором (РЕШ-57):** в `[tool.ruff]` добавлено
`extend-exclude = [".planning"]`. После правки — `ruff format --check .` →
`82 files already formatted` (exit 0), `ruff check .` → `All checks passed!`,
`pytest tests/test_team_docs.py` → 35 passed.

## Чем подтверждено

```
$ python -m pytest tests/test_cli_run.py -v
10 passed in 0.39s
  test_run_intake_failure_falls_back_to_brief_from_raw_text_and_warns PASSED
  test_long_raw_text_is_truncated_at_limit_without_dangling_space PASSED
  test_empty_raw_text_falls_back_to_placeholder_summary[] PASSED
  test_empty_raw_text_falls_back_to_placeholder_summary[   \n\t  ] PASSED

$ python -m pytest tests/test_pipeline_resilience.py -v
10 passed in 2.12s
  test_refine_brief_failure_keeps_owner_answers_in_constraints PASSED
  test_intake_stage_error_reaches_pipeline_logs_with_drafted_status PASSED
  test_cost_limit_exceeded_during_intake_propagates_and_run_fails PASSED
  test_cost_limit_exceeded_during_refine_brief_propagates_and_run_fails PASSED

$ python -m pytest -q
225 passed, 1 warning in 32.77s

$ ruff check .                                    All checks passed!   (exit 0)
$ ruff format --check .   (до РЕШ-57)             exit 1: .planning/evidence/E2A-review-Т-2-cycle2.md:58
$ ruff format --check <5 файлов задачи>           5 files already formatted (exit 0)
$ ruff format --check . --exclude .planning       82 files already formatted (exit 0)
$ ruff format --check .   (после РЕШ-57)          82 files already formatted (exit 0)

$ git diff session/E2A --name-only
backend/app/cli.py
backend/app/pipeline/orchestrator.py
backend/app/pipeline/stages.py
tests/test_cli_run.py
tests/test_pipeline_resilience.py

$ git diff session/E2A --stat      5 files changed, 408 insertions(+), 14 deletions(-)
$ git status --porcelain           (пусто)

# мутационная проба верификатора (плагин в скретчпаде)
MUTANT_LIMIT=500 → passed    499 → passed (выживает)    498 → failed    450 → failed
```

## Handoff

```yaml
handoff:
  from: verifier
  to: auditor
  task: Т-2
  did:
    - Прогнаны 4 критерия приёмки, закрыты 4
    - Проверены по существу три заявления, а не названия тестов: отчёт
      действительно пишется на диск; в constraints попали и вопрос, и ответ;
      статус drafted подтверждён как реальный различитель против failed
    - Выживающий мутант предела 499 воспроизведён с контролем (498 и 450
      убиты); установлена причина — эквивалентность на выбранном входе,
      а не слабость утверждений
    - Подтверждены инварианты 1, 3, 5, 7, 8, 9
  artifacts:
    - .planning/evidence/E2A-verify-Т-2.md
  self_audit: pass
  checks:
    - "pytest tests/test_cli_run.py -v: 10 passed"
    - "pytest tests/test_pipeline_resilience.py -v: 10 passed"
    - "pytest -q: 225 passed (217 + 8 новых кейсов)"
    - "ruff check .: All checks passed"
    - "ruff format --check . после РЕШ-57: 82 files already formatted"
    - "git diff session/E2A --name-only: ровно 5 файлов карточки"
    - "git status --porcelain: пусто"
  escalate: DLG-1 (закрыт оркестратором записью РЕШ-57)
  notes: >
    Вердикт PASS. Единственное расхождение с трижды установленными числами —
    ruff format, падавший на отчёте ревью в .planning/; файл вне границ Т-2,
    код задачи отформатирован. Причина устранена конфигурацией.
```
