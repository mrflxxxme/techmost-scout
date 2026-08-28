# Ревью Э4-П13 · цикл 1
Вердикт: ПРИНЯТЬ (блокирующих 0, советов 1)

## Проверка по пунктам задания

1. **Второго расчёта нет.** `hidden_reason` — единственное место, где
   стоит условие «отклонена или без подтверждений» (`backend/app/verdicts.py:110-136`).
   `_kept_positions` только вызывает её (`verdicts.py:139-159`), от неё же
   транзитивно зависят `_visible_section`, `_visible_summary` и
   `visible_position_count`. `web/routes/review.py::_hidden_reason` зовёт
   публичную функцию напрямую, своей копии условия не осталось. Пройдено
   `grep -rn "REJECT\b" backend/app` — совпадение вне `verdicts.py` есть
   только в `backend/app/export/view.py:300` (`_reindex_after_filter`), но
   это код, существовавший до 5e544e5 (не тронут этим диффом и не входит в
   список файлов задачи) — см. совет ниже.
2. **Поведение клиентского вида не изменилось.** Собрал стенд, прогнал
   `visible_report`/`visible_position_count` на 5e544e5 и на ветке для 11
   сценариев (без вердиктов, reject, rework, пустой evidence, reject и
   пустой evidence на одной и на разных карточках, конкурсная секция,
   несовпадение числа строк сравнения с reject и без, tie-break вердиктов) ×
   оба значения `require_evidence`: JSON-снимки результатов побайтово
   совпали (`diff base_out.json branch_out.json` — пусто).
3. **Порядок причин.** `hidden_reason` проверяет `REJECT` раньше пустого
   `evidence` (`verdicts.py:132-135`); `review.py::_hidden_reason` мапит
   `"rejected"` в `HIDDEN_REJECTED` независимо от состояния `evidence`
   (`review.py:158-159`) — при reject и пустом evidence вместе экран
   по-прежнему покажет «отклонена экспертом», как и до задачи (в старой
   версии тот же порядок: сперва проверка зонда, потом явная проверка
   `verdict.decision == REJECT`).
4. **`require_evidence=False` по умолчанию.** `web/routes/report.py:187`
   зовёт `visible_report(...)` без флага — поведение операторской страницы
   не изменилось; клиентский путь (`export/view.py`, `review.py::
   _hidden_reason`) везде передаёт `require_evidence=True` явно.
5. **Тесты и мутация.** `tests/test_web_review_visibility_source.py`
   монкипатчит `review_module.hidden_reason` (имя, под которым `review.py`
   импортировал функцию) — верно для перехвата на этом экране. Поставил
   собственную мутацию прямо в тело `hidden_reason` (убрал ветку
   `require_evidence`) — красными стали и `tests/test_verdicts.py` (4 теста:
   `..._блок6`, `..._и_reject_вместе`, `..._из_contest_cards`,
   `test_visible_position_count_считает_reject_и_evidence_вместе`), и
   `tests/test_web_review_visibility_source.py::test_подсказка_экрана_ревью_
   меняется_вместе_с_общим_признаком_verdicts` — итого 5 failed, 18 passed,
   совпадает с числами из handoff `mutation_criterion_5`. Вернул файл,
   `git diff -- backend/app/verdicts.py` — пусто.
6. **Мёртвый код.** `Report`, `Brief`, `ReportSummary`, `datetime`,
   `visible_report` действительно больше не нужны в `review.py`: `grep -n
   "VerdictDecision\|CardKey" backend/app/web/routes/review.py` показывает,
   что оставшиеся импорты (`VerdictDecision`, `CardKey`) используются в
   других местах файла (метки решений, типы `card_ids`). `ruff check .`
   подтверждает — замечаний по неиспользуемым импортам нет.
7. **Тексты подсказок.** `HIDDEN_REJECTED`/`HIDDEN_NO_EVIDENCE` в
   `review.py` дословно те же, что на 5e544e5 (сверено построчно). Границы:
   диффом задеты `backend/app/verdicts.py`, `backend/app/web/routes/
   review.py`, `tests/test_web_review_visibility_source.py` и файл
   handoff в `.planning/evidence/` — три кодовых файла из четырёх,
   перечисленных в постановке (`tests/test_verdicts.py` в списке
   разрешённых, но не тронут — это не нарушение границ, только он и не
   понадобился: существующие тесты `test_verdicts.py` уже накрывают
   `hidden_reason` транзитивно и ловят мутацию её тела, что подтверждено
   прогоном выше).

## Проверено

- `pytest -q`: 1202 passed, 1 warning (starlette/httpx deprecation, не
  относится к задаче)
- `pytest -q tests/test_web_review_visibility_source.py tests/test_verdicts.py
  tests/test_web_report.py tests/test_web_report_verdicts.py
  tests/test_web_flow_e2e.py tests/test_web_review.py`: все проходят
- Мутация тела `hidden_reason` (убрана ветка `require_evidence`):
  `pytest -q tests/test_web_review_visibility_source.py tests/test_verdicts.py`
  → 5 failed, 18 passed (те же тесты, что заявлены в handoff); после
  возврата файла `git diff -- backend/app/verdicts.py` пуст,
  `pytest -q` снова 1202 passed
- `ruff check .`: All checks passed!
- `ruff format --check .`: 204 files already formatted
- Собственный стенд сравнения `visible_report`/`visible_position_count` на
  5e544e5 и на ветке для 11 сценариев × 2 значения `require_evidence`:
  результаты идентичны
- `grep -rE "sk-|gigachat|Api-Key" backend/ tests/`: только легитимные
  упоминания заголовков авторизации и тестовые фиктивные ключи (`sk-test`,
  `sk-fake-leak-check-*`), боевых значений нет
- `grep -rn "httpx" backend/app/verdicts.py backend/app/web/routes/review.py`:
  пусто (инвариант №6 не задет этим диффом)
- `pytest -q tests/test_team_docs.py`: 35 passed (YAML handoff парсится,
  двоеточия закавычены штатно)
- `wc -l backend/app/verdicts.py backend/app/web/routes/review.py
  tests/test_web_review_visibility_source.py`: 255 / 387 / 184 — все в
  пределе 500
- `git status --short` в рабочей копии: чисто, диффом задеты только
  заявленные файлы плюс `.planning/evidence/Э4-П13-handoff.yaml`
- Инварианты 1–10: этой задачей не нарушен ни один (задача — чистый
  рефакторинг существующего условия видимости, новых внешних вызовов,
  секретов, промптов и YAML-полей не заводит)

## Советы (не блокируют)

1. `backend/app/export/view.py:300` (`_reindex_after_filter`) держит свою
   проверку `verdict.decision == VerdictDecision.REJECT` — комментарий в
   коде сам называет её «та же проверка на REJECT, что и внутри
   verdicts._visible_section», то есть это осознанный, документированный
   дубль ради пересчёта позиций и `rework_keys`, а не скрытая ошибка. Он
   существовал до этой задачи (проверил на 5e544e5) и не входит в список
   файлов Э4-П13, поэтому не блокирую, но раз причина скрытия теперь есть
   как публичная функция — стоит отдельной небольшой задачей завести
   `_reindex_after_filter` на `hidden_reason(card, verdict) is not None`
   вместо прямого сравнения с `REJECT`, чтобы четвёртой копии условия
   (пусть пока и корректной) тоже не осталось.

## Handoff

```yaml
handoff:
  from: reviewer
  to: verifier
  task: Э4-П13
  did:
    - Сверены diff и постановка Э4-П13; проверены все десять инвариантов способами из INVARIANTS.md
    - Стендом сравнены visible_report/visible_position_count на 5e544e5 и на ветке — 11 сценариев × 2 require_evidence, расхождений нет
    - Поставлена мутация в тело hidden_reason (снята ветка require_evidence) — упали 5 тестов (4 в test_verdicts.py, 1 в test_web_review_visibility_source.py), возврат чист
    - Прогнаны pytest -q, ruff check ., ruff format --check .
  artifacts:
    - .planning/evidence/Э4-П13-review.md (в рабочей копии ревьюера, коммит в main — за оркестратором)
  self_audit: pass
  checks:
    - "pytest -q: 1202 passed, 1 warning"
    - "ruff check .: All checks passed!"
    - "ruff format --check .: 204 files already formatted"
    - "мутация hidden_reason (без require_evidence): 5 failed, 18 passed; после возврата git diff -- backend/app/verdicts.py пуст"
  escalate: none
  read_first:
    - .planning/PLAN.md (раздел Э4-П13)
    - .planning/DECISIONS.md (РЕШ-215)
    - backend/app/verdicts.py
    - backend/app/web/routes/review.py
  notes: |
    Цикл 1, вердикт ПРИНЯТЬ — второго цикла не требуется. Один совет вне
    границ задачи: export/view.py:300 держит свой, документированный дубль
    проверки REJECT для пересчёта позиций — предлагаю отдельной задачей
    свести его к hidden_reason(...) is not None.
```
