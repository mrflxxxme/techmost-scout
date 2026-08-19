# Ревью З-2 · цикл 1

Вердикт: ВЕРНУТЬ (блокирующих 3, советов 2)

Дерево: `.claude/worktrees/E2V-Z2`, ветка `E2V/Z2`, база `7c6a674`, вершина
`7c11b36` (подтверждено `git log --oneline -1`). Диф — 24 файла, 1577(+)/7(-).

## Блокирующие

1. **[инвариант №8, дефект логики]** Вся цепочка вызова
   `retry_constraints_if_missing` внутри `backend/app/cli.py:462-488` —
   собственно точка, где срабатывает главный инвариант задачи («вызов ровно
   один раз за прогон», ветка `CostLimitExceededError` с записью
   `persist_run(status="failed", ...)`, и `intake_caveats.append(...)`) —
   **не покрыта ни одним тестом на уровне интеграции**. Проверено лично двумя
   независимыми мутациями (применены, прогнаны, немедленно отменены
   `git checkout --`, дерево чистое):
   - мутация «убрать `if constraints_unresolved:` (заменить на
     `if False and constraints_unresolved:`)» — `pytest -q`: **604 passed**,
     без единого падения;
   - мутация «удалить весь блок вызова `retry_constraints_if_missing` вместе
     с обработкой `CostLimitExceededError`» (строки 466-488 cli.py заменены на
     `constraints_unresolved = False`) — `pytest -q`: **604 passed**, без
     единого падения.

   То есть можно физически стереть код, который постановка называет главным
   инвариантом подэтапа («молчаливого третьего исхода нет»), — и весь набор
   тестов останется зелёным. Это ровно тот класс дефекта, о котором
   предупреждает постановка ревью: «нет ли пути, на котором не происходит ни
   того, ни другого». Путь есть, и он никем не проверяется.

   Существующий прецедент в этом же файле показывает, что интеграционная
   проверка `CostLimitExceededError` на интейке — это принятая практика
   проекта, а не что-то избыточное: `tests/test_pipeline_resilience.py:905-965`
   уже проверяет тот же паттерн (`CostLimitExceededError` из `run_intake` и
   `refine_brief` пробрасывается через `cli._run`) — тем же способом, каким
   нужно было проверить и новую ветку. Кроме того, новая ветка не переиспользует
   существующий `_persist_intake_stop` (cli.py:127), а дублирует его тело —
   ещё один симптом того, что путь писался без теста и без сверки с соседним
   кодом.

   Все восемь заявленных мутаций (см. «Проверено» ниже) бьют по функциям
   внутри `constraints_guard.py`, ни одна не бьёт по проводке в `cli.py`.
   Критерии приёмки задачи (пп. 2, 3, 7 постановки) заявлены как проверяемые
   `tests/test_constraints_guard.py`, но эта функция тестируется в изоляции —
   а её вызов из продукта не проверен вовсе.

2. **[инвариант №8, отсутствующий тест]** Строка лога страховки
   `ensure_requirement_checks` (РЕШ-122: «срабатывание страховки печатает
   строку лога... печатается именно при подстановке, а не всегда») не
   проверена ни одним тестом. В `tests/test_constraints_guard.py` три теста на
   `ensure_requirement_checks` (backfill, no-op при существующих checks, no-op
   при пустом `brief.constraints`), и ни один не использует `caplog`. Проверено
   вручную отдельным скриптом на боевом коде (не изменяет репозиторий): строка
   `log.info("страховка requirement_checks сработала: ...")` фактически
   печатается только при срабатывании подстановки и не печатается, когда
   `card.requirement_checks` уже заполнен моделью — то есть сам код
   поведенчески верный, но РЕШ-122 прямо требует, чтобы это стало «числом,
   которое можно предъявить», а без теста это невозможно гарантировать при
   следующей правке: рефакторинг случайно вынесет `log.info` из-под условия (или
   уберёт условие вовсе) — красным ничего не станет. Постановка ревью отдельным
   пунктом (7) требует проверить именно это; после проверки — не проверено.

3. **[выход за границы задачи, §5 CONVENTIONS]** Diff трогает
   `tests/test_cli_run.py` и `tests/test_pipeline_resilience.py` — оба файла
   отсутствуют в списке файлов задачи З-2 (`PLAN.md:217-222`). Правка
   технически минимальна (добавлен параметр `constraints_unresolved: bool =
   False` в сигнатуру двух тестовых заглушек `.run()`, по 2 строки на файл) и
   вынужденная — без неё существующие тесты падали бы на
   `TypeError: unexpected keyword argument`, потому что `cli.py` теперь зовёт
   `pipeline.run(..., constraints_unresolved=...)`. Находка — не про качество
   правки (она безобидна), а про то, что список файлов задачи в плане не
   предусмотрел это последствие, и правка вне списка ушла без эскалации.
   Пограничный случай: чисто механическая правка ради совместимости сигнатуры,
   но формально — файл вне границ, которые сам план объявил закрытыми
   (§11 CONVENTIONS: «изменения в общих контрактах выполняет только один
   исполнитель», сигнатура `ScoutPipeline.run` — общий контракт).

## Советы (не блокируют)

1. `.planning/evidence/E2V-З2-mutation-d.txt` фактически содержит две
   мутации подряд без разделительного заголовка для первой (только вторая
   помечена строкой «=== мутация (д): ... ===»). По факту все восемь
   заявленных мутаций (а-з) присутствуют в наборе из 7 файлов, просто криво
   разложены (b=б, c=в, d=г+д склеены, z=з, zh=ж) — работа сделана, но
   доказательство неаккуратно оформлено.
2. Обработчик `CostLimitExceededError` в новом блоке `cli.py:471-486`
   дублирует тело `_persist_intake_stop` (cli.py:127) вместо повторного
   использования — тот же паттерн, тот же набор аргументов. Не критично, но
   рассинхронизация двух копий при следующей правке — риск.

## Проверено

- `git -C .claude/worktrees/E2V-Z2 log --oneline -1`: `7c11b36` — совпадает с
  задачей.
- `pytest -q` (эталонный `.venv`): 604 passed, 1 warning in 182.92s —
  совпадает с заявлением исполнителя (было 580 на базовой линии).
- `ruff check .`: All checks passed!
- `ruff format --check .`: 100 files already formatted.
- `grep -rE "sk-|gigachat|Api-Key" backend/ tests/`: только комментарии,
  плейсхолдеры sk-test/SecretStr("sk-test") в тестах конфигурации и
  header-строки Api-Key — значений секретов нет.
- `grep -rn "httpx" backend/app`: только llm/, search/, cli.py,
  orchestrator.py — без новых нарушений.
- `git diff 7c6a674 7c11b36 --stat`: 24 файла — список совпадает с задачей,
  кроме tests/test_cli_run.py и tests/test_pipeline_resilience.py (см.
  блокирующую находку 3).
- Восемь заявленных мутаций (а-з) из .planning/evidence/E2V-З2-mutation-*.txt
  проверены чтением: каждая действительно валит ожидаемый тест по ожидаемой
  причине (проверено по трассе AssertionError/traceback в каждом файле).
- Две собственные мутации (см. блокирующая находка 1) — применены лично,
  pytest -q прогнан, немедленно отменены git checkout --. Рабочее дерево
  подтверждено чистым (git status --short пусто) до и после.
- Двусторонность инварианта РЕШ-115: ветка «поле собрано» —
  test_retry_constraints_fills_only_constraints_field (constraints заполнен,
  task_summary/keywords/industry не переписаны, router.cost.calls == 2);
  ветка «поле не собрано после второго вызова» —
  test_retry_constraints_empty_second_answer_marks_unresolved (unresolved
  True) плюс test_constraints_unresolved_notice_printed_twice (клиент видит
  строку дважды); ветка «нет признаков требований» —
  test_retry_constraints_skipped_without_requirement_signs (0 вызовов). Все
  три ветки закрыты тестами на уровне функции constraints_guard, но не на
  уровне cli.py (находка 1) — то есть двусторонность доказана для модуля, но
  не для прогона целиком.
- Отказ повторного вызова (инвариант №5): LLMError — прогон продолжается,
  unresolved=True, своя строка pipeline_logs (STAGE_INTAKE_CONSTRAINTS);
  CostLimitExceededError — пробрасывается наружу, строка журнала уже
  дописана до броска. Оба случая проверены чтением кода и трассой в
  E2V-З2-mutation-z.txt.
- Границы: RELEVANCE_MIN, SECOND_PASS_RELEVANCE_MAX, scorer.md,
  backend/app/models.py, backend/app/config.py не тронуты (git diff --stat по
  каждому — пусто). Второе поле requirements не заведено — в schemas.py
  правка только описания и клиентской подписи у существующего constraints.
  Строки «не менее двух» и «для солидности» в prompts/intake.md на месте
  (grep подтверждён).
- Обе клиентские формулировки — дословно по решениям владельца: подпись
  "**Требования и ограничения:**" (markdown.py:90), строка «Требования
  задачи не выделены из постановки — построчная сверка карточек не
  проводилась.» (markdown.py:41-49, константа CONSTRAINTS_UNRESOLVED_NOTICE
  печатается в обоих местах одной константой, опознания по подстроке в
  экспортёре нет — печать в «Задаче» идёт по флагу report.constraints_unresolved,
  что подтверждено тестом test_constraints_unresolved_flag_drives_notice_not_caveat_substring
  и трассой мутации (ж)).
- pytest tests/test_prompts.py -v: 44 passed — версия intake.md (1.3)
  совпадает с последней записью CHANGELOG.md.

## Мутации, воспроизведённые лично (см. «Блокирующие», п. 1)

Мутация 1 — отключить печать оговорки в cli.py (заменить `if
constraints_unresolved:` на `if False and constraints_unresolved:` перед
`intake_caveats.append(CONSTRAINTS_UNRESOLVED_NOTICE)`):
результат `pytest -q` — 604 passed, падений нет.

Мутация 2 — убрать вызов `retry_constraints_if_missing` из `cli._run`
целиком (заменить блок try/except на `constraints_unresolved = False`):
результат `pytest -q` — 604 passed, падений нет.

Обе мутации отменены `git checkout -- backend/app/cli.py` немедленно после
прогона; рабочее дерево подтверждено чистым (`git status --short`, вывод
пуст) до и после каждой мутации.

## Handoff

```yaml
handoff:
  from: reviewer
  to: implementer-B
  task: З-2
  did:
    - "Прочитаны PLAN.md (раздел З-2), INVARIANTS.md, CONVENTIONS.md §5/§7,"
    - "CLAUDE.md, DECISIONS.md (РЕШ-109,115,116,122,123,38,45,102), diff целиком"
    - "Прогнаны pytest -q (604 passed), ruff check ., ruff format --check ."
    - "Проверены grep по инвариантам №3 и №6 - нарушений нет"
    - "Прочитаны все 8 мутаций исполнителя построчно - все восемь бьют по"
    - "ожидаемым причинам, все документированы (хотя криво разложены по файлам)"
    - "Применены и прогнаны 2 собственные мутации на cli.py, обе не пойманы"
    - "тестами; обе отменены git checkout сразу после прогона"
    - "Найдено 3 блокирующих (инвариант №8 дважды, выход за границы задачи),"
    - "2 совета"
  artifacts:
    - "отчёт возвращён текстом и сохранён"
    - ".planning/evidence/E2V-З2-review-cycle1.md"
  self_audit: pass
  checks:
    - "pytest -q: 604 passed, 1 warning in 182.92s"
    - "ruff check .: All checks passed!"
    - "ruff format --check .: 100 files already formatted"
    - "собственная мутация (удалить вызов retry_constraints_if_missing из"
    - "cli._run): pytest -q: 604 passed - вызов не покрыт ни одним тестом"
  escalate: none
  read_first:
    - ".planning/PLAN.md (раздел З-2, файлы: constraints_guard.py, cli.py,"
    - "schemas.py, orchestrator.py, markdown.py)"
    - backend/app/cli.py:462-488 (непокрытая проводка)
    - backend/app/pipeline/constraints_guard.py
    - tests/test_constraints_guard.py
    - .planning/DECISIONS.md (РЕШ-115, РЕШ-122, РЕШ-123)
  notes: >
    Это цикл 1. Второй цикл - последний: если после него integration-тест на
    cli.py не появится, вердикт BLOCKED и разбор с владельцем. Просьба
    добавить в tests/test_cli_run.py тест, который монки-патчит
    retry_constraints_if_missing (или роутер целиком) и проверяет, что при
    unresolved=True cli._run кладёт CONSTRAINTS_UNRESOLVED_NOTICE в
    intake_caveats, а при CostLimitExceededError из этого вызова прогон
    падает с записью persist_run(status="failed"). Также нужен caplog-тест
    на строку лога ensure_requirement_checks (печатается только при
    срабатывании страховки, не печатается, когда model уже вернула checks).
    Находка 3 (файлы вне списка задачи) - на усмотрение: если оркестратор
    сочтёт её несущественной технической правкой сигнатуры, можно закрыть
    записью в DECISIONS.md без переделки кода.
```
