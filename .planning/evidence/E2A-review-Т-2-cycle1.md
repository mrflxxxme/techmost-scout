# Ревью Т-2 · Устойчивость интейка · цикл 1

Вердикт: **ДОРАБОТАТЬ** (блокирующих 1, советов 2)

## Блокирующие

1. **[инвариант №8 / §5 — новое поведение без теста]**
   `backend/app/pipeline/stages.py:144-160`, функция `brief_from_raw_text`.
   Задача (`PLAN.md`, Т-2, п. 1) требует обрезку сырого текста на
   `_FALLBACK_SUMMARY_LIMIT = 500` знаков с многоточием и запасное значение
   `"не указано"` для пустого текста — две отдельные ветки новой логики. Ни
   одна не покрыта: единственный вызов функции в тестах
   (`tests/test_cli_run.py::test_run_intake_failure_falls_back_to_brief_from_raw_text_and_warns`)
   передаёт короткий текст («сырой текст задачи про катализатор», заведомо
   короче 500 знаков), поэтому строка `summary = summary[:500].rstrip() + "…"`
   не исполняется ни разу во всём наборе — `grep -rn "brief_from_raw_text"
   tests/` даёт одно вхождение с тем же коротким текстом. Веткой
   `summary or "не указано"` тоже никто не пользуется.

   Код по чтению корректен (`raw_text.strip()`, срез, `rstrip()`, добавление
   «…»), и логика ничего не домысливает. Блокирует именно отсутствие теста:
   без него регрессия при следующей правке — смещённый срез, потерянный
   `.rstrip()` — пройдёт незамеченной. Это дыра в гейте по §12.

   **Правка:** добавить минимум два случая — текст длиннее 500 знаков
   (проверить, что длина `task_summary` не превышает 501, что срез не рвёт
   факт непредсказуемо и что строка оканчивается на «…») и пустой либо
   пробельный текст (`task_summary == "не указано"`).

## Советы (не блокируют)

1. `test_cost_limit_exceeded_during_intake_propagates_and_run_fails` проверяет
   `CostLimitExceededError` только для ветки `run_intake` (`cli.py:181-182`).
   Симметричная ветка в `refine_brief` (`cli.py:205-206`) кодом идентична и
   проверена чтением, но отдельным тестом не покрыта — зеркальный тест
   недорог, раз в задаче явно выделены «оба места вызова, а не одно».
2. `brief_from_raw_text` не имеет собственного модульного теста (только
   косвенно, через `cli._run`). Для чистой функции без побочных эффектов
   естественное место — `tests/test_stages.py`. *Примечание оркестратора:
   `tests/test_stages.py` в список файлов Т-2 не входит, поэтому в рамках
   этой задачи совет неисполним; тесты кладутся в `tests/test_cli_run.py`
   либо `tests/test_pipeline_resilience.py`.*

## Проверено

- Дифф соответствует списку файлов Т-2: `backend/app/cli.py`,
  `backend/app/pipeline/orchestrator.py`, `backend/app/pipeline/stages.py`,
  `tests/test_cli_run.py`, `tests/test_pipeline_resilience.py` — ровно эти
  пять, посторонних нет.
- `pytest -q`: **221 passed, 1 warning in 32.90s** (базовая линия ветки
  подэтапа 217, прирост совпадает с заявленным).
- `pytest tests/test_cli_run.py -v`: **7 passed**.
- `pytest tests/test_pipeline_resilience.py -v`: **9 passed**.
- `ruff check .`: **All checks passed!**
- `ruff format --check .`: **123 files already formatted**.
- `git status --porcelain` после ревью: пусто — ни один файл не создан и не
  изменён.

### Инварианты — все десять

| № | Статус | Как проверено |
|---|---|---|
| 1 | соблюдён | `brief_from_raw_text`: `industry` — только из подсказки либо «не указана», `task_summary` — обрезка без переформулирования, остальные поля — дефолты `Brief`. Ответы владельца дописываются в `constraints` буквально («вопрос — ответ»), ничего не добавляется от себя; `test_refine_brief_failure_keeps_owner_answers_in_constraints` зелёный |
| 2 | не затронут | Экспорт и `validated` в диффе не участвуют |
| 3 | соблюдён | `grep -rE "sk-\|gigachat\|Api-Key" backend/ tests/` — совпадения только на имена провайдера и переменных окружения, значений секретов нет; `.env` в `.gitignore`. В консоль уходит фиксированный текст предупреждения без `str(exc)`; `str(exc)` идёт в лог и в `pipeline_logs.error` |
| 4 | не затронут | Промпты и `render_prompt` не задеты; `test_prompts.py` зелёный в составе прогона |
| 5 | соблюдён | Отказ интейка не гасится в тихий успех: предупреждение оператору плюс `StageRecord(stage="intake", error=str(exc))`, статус запроса остаётся `drafted`. `persistence._write` берёт `PipelineLogRow.error` из самой записи, а не из общего `error` запроса, поэтому строка `intake` в БД интерпретируема |
| 6 | соблюдён | `grep -rn "httpx" backend/app`: новых мест вызова в диффе нет, импорт в `cli.py` был раньше |
| 7 | соблюдён | Новые тесты работают на `monkeypatch` для `run_intake`/`refine_brief` и подменённом роутере; сеть не требуется |
| 8 | **нарушен частично** | Большая часть нового поведения покрыта, но ветки обрезки и пустого текста — без теста. См. блокирующее №1 |
| 9 | соблюдён | Дифф строго в границах Т-2: `schemas.py` не тронут, маршрут `intake` не менялся, веб-интерфейс уточнений не задет, разбиения `stages.py` нет — добавлена одна чистая функция на 18 строк |
| 10 | не затронут | YAML в диффе не участвует |

### Прочитано, проблем не найдено

- Порядок `except CostLimitExceededError: raise` перед `except Exception`
  соблюдён на **обоих** местах вызова (`cli.py:181` и `cli.py:205`).
  `CostTracker.add` кладёт исключение в блок `else` после `try/except
  LLMError`, поэтому `except LLMError` в `router.complete` его не
  перехватывает — путь до `cli._run` чист.
- `initial_stage_log` корректно передаётся из `cli._run` в `pipeline.run` и
  склеивается со `stage_log` оркестратора (`orchestrator.py:192`); каждая
  запись пишется в `pipeline_logs` со своим `error` независимо от статуса
  запроса — подтверждено тестом на временной SQLite-базе.

## Handoff

```yaml
handoff:
  from: reviewer
  to: implementer-2
  task: Т-2
  cycle: 1
  did:
    - Проверены дифф (5 файлов, ровно по списку задачи), все десять инвариантов,
      грабли §0.1
    - Прогнаны pytest -q (221 passed), test_cli_run.py (7), test_pipeline_resilience.py (9),
      ruff check и ruff format --check — чисто
    - Найдено 1 блокирующее (инвариант №8: ветки обрезки на 500 знаков и пустого
      текста в brief_from_raw_text без теста), 2 совета
  artifacts:
    - .planning/evidence/E2A-review-Т-2-cycle1.md
  self_audit: pass
  checks:
    - "pytest -q: 221 passed, 1 warning in 32.90s"
    - "pytest tests/test_cli_run.py -v: 7 passed"
    - "pytest tests/test_pipeline_resilience.py -v: 9 passed"
    - "ruff check .: All checks passed!"
    - "git status --porcelain после ревью: пусто"
  escalate: none
  notes: >
    Цикл 1 из двух. Логика brief_from_raw_text по чтению корректна — не
    домысливает и не меняет смысл обрезкой; блокирует отсутствие теста на
    ветку >500 знаков и на пустой текст, а не дефект логики. Остальное
    (CostLimitExceededError, сохранение ответов владельца, след в
    pipeline_logs) реализовано верно и подтверждено зелёными тестами.
    Второй цикл — последний.
```
