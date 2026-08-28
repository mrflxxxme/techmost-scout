# Ревью Э4-П14 · цикл 1
Вердикт: ПРИНЯТЬ (блокирующих 0, советов 1)

## Проверка по пунктам задания

1. **Согласование позиций не сломано.** `_reindex_after_filter` теперь
   принимает `edited_report` и берёт карточку `cards[key[1]]` для вызова
   `hidden_reason(card, verdict)` **без** `require_evidence` (по умолчанию
   `False`) — тот же режим, каким `build_export_view` собирает
   `ExportView.report`/`card_ids` (операторский вид). Проверил по коду:
   `web/routes/report.py:72,76` берёт `view.card_ids.get((is_contest, index))`
   по `enumerate(view.report.cards)` — тому же `require_evidence=False`
   отчёту; `card_id` из этой пары используется в `_card.html:90` как
   `action="/cards/{{ card_id }}/edit"` — форма правки оператора. Расхождение
   признаков здесь било бы напрямую: правка уехала бы не в ту карточку.
   Собрал стенд (комбинированный тест задачи + свой скрипт, см. «Проверено»)
   с бессылочной карточкой рядом с отклонённой и карточкой на доработку:
   бессылочная карточка в операторском виде не скрывается (`require_evidence`
   не участвует), её позиция и `card_id` после реиндексации совпадают с
   ожидаемым сдвигом только из-за убранного reject — правка/пометка
   `rework` остаются на своей карточке.
2. **Позиции и `rework` не изменились.** Помимо четырёх случаев исполнителя
   (reject, rework, пустой `evidence`, конкурсная секция — в одном
   комбинированном тесте), сам прогнал прямое сравнение старой и новой
   `_reindex_after_filter` (старая функция скопирована из `e7710ba`, новая
   импортирована с ветки, обе вызваны на одинаковых синтетических данных) для
   10 сценариев, включая недостающие из задания: reject и rework вместе (на
   разных карточках — на одной вместе они и не бывают, `effective_verdicts`
   отдаёт один действующий вердикт), несколько отклонённых подряд,
   отклонённая последней, все отклонены, конкурсная секция с reject,
   reject + пустой evidence на одной карточке. Все 10 — `OK`, совпадение
   побайтово (см. вывод ниже).
3. **Мутация.** Поставил мутацию «вернуть прямое сравнение с REJECT,
   игнорируя пустой evidence» (тело `_reindex_after_filter` заменено на
   версию с `e7710ba`) — красным стал ровно
   `test_reindex_after_filter_зовёт_verdicts_hidden_reason_а_не_свою_копию`
   (1 failed, 9 passed), это и есть тест на критерий 4. Вернул код —
   `git diff -- backend/app/export/view.py` пуст. Отдельно поставил обратную
   мутацию — `hidden_reason(card, verdict, require_evidence=True)` — она
   тоже покраснела, но на другом тесте:
   `test_reindex_after_filter_reject_rework_evidence_и_конкурсная_секция`
   (у него в стенде есть карточка без подтверждений — при `require_evidence=
   True` она пропадёт из `card_ids`, а тест ждёт, что она останется). Значит
   параметр не «ничего не решает» — тесты реально проверяют оба направления
   переключателя, а не только факт вызова функции. Вернул код — diff пуст.
4. **Копий условия больше нет.** `grep -rn "REJECT" backend/app --include="*.py"`
   даёт пять строк: сравнение — только `verdicts.py:132`
   (`hidden_reason`); остальные — enum `schemas.py:70`, словарь подписей
   `review.py:68` (`VerdictDecision.REJECT: "Отклонено"`), константа текста
   `review.py:75/159` (`HIDDEN_REJECTED` — маппинг строки `"rejected"`,
   отданной `hidden_reason`, в русский текст, а не сравнение с REJECT), и
   упоминание в докстроке `export/view.py:289`. Четвёртой копии условия нет.
5. **Тест на подмену `export.view.hidden_reason`.** Не тест самого себя:
   монкипатчится импортированное в `view.py` имя, а проверяется результат
   полного прохода `build_export_view` через настоящую БД (`view.report`,
   `view.card_ids`) — то есть тест ловит именно то, вызывает ли
   `_reindex_after_filter` реальную функцию `hidden_reason` как внешнюю
   зависимость, а не считает условие сама. Подтверждено мутацией п. 3: при
   возврате прямого сравнения с REJECT (без обращения к `hidden_reason`)
   подмена не действует, и тест краснеет — ровно ожидаемое поведение.
6. **Границы.** `git diff e7710ba..e4/p14-last-copy --name-only`:
   `backend/app/export/view.py`, `tests/test_summary_field_gate.py`,
   `.planning/evidence/Э4-П14-handoff.yaml` — ровно список задачи плюс
   handoff. Инвариант №7 (сеть): весь `pytest -q` зелёный без сети (стенд на
   временном SQLite, respx не нужен для этого диффа). Инвариант №8: правка
   `backend/` сопровождена правкой `tests/`. `wc -l`: `view.py` — 317 строк,
   `test_summary_field_gate.py` — 451 строка, оба в пределе 500; стражи
   layout (`test_backend_layout.py`, `test_tests_layout.py`,
   `test_pipeline_layout.py`) — 12 passed.

## Проверено

- `pytest -q` (venv `/home/user/techmost-scout/.venv`): 1204 passed, 1
  warning (starlette/httpx deprecation, не относится к задаче)
- `ruff check .`: All checks passed!
- `ruff format --check .`: 204 files already formatted
- Мутация «вернуть прямое сравнение с REJECT» (тело функции заменено на
  версию `e7710ba`): `pytest -q tests/test_summary_field_gate.py` → 1 failed
  (`test_reindex_after_filter_зовёт_verdicts_hidden_reason_а_не_свою_копию`),
  9 passed; после возврата `git diff -- backend/app/export/view.py` пуст
- Обратная мутация `hidden_reason(card, verdict, require_evidence=True)`:
  `pytest -q tests/test_summary_field_gate.py` → 1 failed
  (`test_reindex_after_filter_reject_rework_evidence_и_конкурсная_секция`),
  9 passed; после возврата diff пуст — параметр `require_evidence` в этой
  функции проверяется реально, не декоративен
- Собственный стенд сравнения старой (скопирована с `e7710ba`) и новой
  `_reindex_after_filter` на 10 синтетических сценариях (reject; rework;
  пустой evidence без вердикта; reject+rework на разных карточках;
  конкурсная секция с reject; несколько отклонённых подряд; отклонённая
  последней; все отклонены; reject+пустой evidence на одной карточке;
  reject в основной и конкурсной секциях вместе) — все 10 `OK`, результаты
  побайтово идентичны
- `wc -l backend/app/export/view.py tests/test_summary_field_gate.py`:
  317 / 451 — оба в пределе 500
- `pytest -q tests/test_backend_layout.py tests/test_tests_layout.py
  tests/test_pipeline_layout.py`: 12 passed
- `grep -rn "REJECT" backend/app --include="*.py"`: одно сравнение
  (`verdicts.py:132`), остальное — enum/подписи/докстрока
- `grep -rn "httpx" backend/app/export/view.py tests/test_summary_field_gate.py`:
  пусто (инвариант №6 не задет этим диффом)
- `grep -rE "sk-|gigachat|Api-Key" backend/ tests/`: только легитимные
  заголовки авторизации и тестовый `sk-test`, боевых значений нет
- `pytest -q tests/test_team_docs.py`: 35 passed (YAML handoff парсится,
  двоеточия закавычены штатно)
- `git diff e7710ba..e4/p14-last-copy --name-only`: только
  `backend/app/export/view.py`, `tests/test_summary_field_gate.py`,
  `.planning/evidence/Э4-П14-handoff.yaml`
- Инварианты 1–10: этой задачей не нарушен ни один — задача чистый
  рефакторинг существующего условия видимости (собственного расчёта REJECT
  в `_reindex_after_filter` больше нет, решение берётся у
  `verdicts.hidden_reason`), новых внешних вызовов, секретов, промптов и
  YAML-полей не заводит; операторская страница читает тот же
  `require_evidence=False` отчёт, что и раньше

## Советы (не блокируют)

1. Docstring `_reindex_after_filter` подробно объясняет согласование с
   `ExportView.card_ids`, но не упоминает явно, что `require_evidence` там
   намеренно не передаётся именно потому, что второго потребителя (с
   `require_evidence=True`) для этой функции в проекте нет — это осталось
   только в `notes` хендоффа. Стоило бы перенести одну строку из заметки
   исполнителя в докстроку, чтобы будущий читатель кода не заводил
   параметр `require_evidence` у `_reindex_after_filter` не глядя в историю
   решений.

## Handoff

```yaml
handoff:
  from: reviewer
  to: verifier
  task: Э4-П14
  did:
    - Сверены diff и постановка Э4-П14 (файлы: backend/app/export/view.py, tests/test_summary_field_gate.py); проверены все десять инвариантов способами из INVARIANTS.md
    - Проверено согласование require_evidence=False между _reindex_after_filter и операторской страницей (report.py, _card.html) по коду
    - Стендом сравнены старая и новая _reindex_after_filter на 10 сценариях (reject, rework, пустой evidence, reject+rework, конкурсная секция, несколько отклонённых подряд, отклонённая последней, все отклонены и др.) — расхождений нет
    - Поставлена мутация «вернуть прямое сравнение с REJECT» — упал ровно тест на критерий 4; поставлена обратная мутация require_evidence=True — упал комбинированный стенд-тест; обе возвращены, diff чист
    - Прогнаны pytest -q (1204 passed), ruff check ., ruff format --check ., стражи layout
  artifacts:
    - .planning/evidence/Э4-П14-review.md (в рабочей копии ревьюера, коммит в main — за оркестратором)
  self_audit: pass
  checks:
    - "pytest -q: 1204 passed, 1 warning"
    - "ruff check .: All checks passed!"
    - "ruff format --check .: 204 files already formatted"
    - "мутация REJECT напрямую: pytest -q tests/test_summary_field_gate.py → 1 failed, 9 passed; возврат чист"
    - "мутация require_evidence=True: pytest -q tests/test_summary_field_gate.py → 1 failed (другой тест), 9 passed; возврат чист"
    - "10-сценарийный стенд base vs branch _reindex_after_filter: все OK"
  escalate: none
  read_first:
    - .planning/PLAN.md (раздел Э4-П14)
    - .planning/DECISIONS.md (РЕШ-215, РЕШ-213, РЕШ-198)
    - .planning/evidence/Э4-П13-review.md (совет 1)
    - backend/app/export/view.py
    - tests/test_summary_field_gate.py
  notes: |
    Цикл 1, вердикт ПРИНЯТЬ — второго цикла не требуется. Один необязывающий
    совет: перенести из notes хендоффа исполнителя в докстроку
    _reindex_after_filter пояснение, почему require_evidence там не
    параметризуется (второго потребителя с require_evidence=True сейчас
    нет).
```
