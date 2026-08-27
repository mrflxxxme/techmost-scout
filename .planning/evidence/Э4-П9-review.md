# Ревью Э4-П9 · пополевые ворота печати сводки · цикл 1
Вердикт: ПРИНЯТЬ (блокирующих 0, советов 2)

Ветка `e4/p9-field-gate`, база `d3bf88b`. Закрывает БЛОК-5 финального аудита
Э4 (третья, последняя итерация одного и того же класса дефекта — «гейт слеп»).

## 1. Воспроизведение БЛОК-5 самим ревьюером

Стенд: настоящий `POST /requests/{id}/summary` через `TestClient(main.app)`
(с реальным `resolve_summary_edit`/`SummaryOverrideRow`) + настоящий
`build_export_view` + настоящий `render_print_html`. Отчёт из двух карточек,
карточка 1 («Первый») отклонена экспертом, эксперт правит **только**
оговорки, отправляя в форму рекомендацию дословно нетронутой.

**На базе `d3bf88b`:**
```
POST /requests/1/summary -> 200
summary_text_edited = True
HTML содержит непровленную рекомендацию модели: True
HTML содержит правленую оговорку эксперта: True
```
Клиенту уезжает «Первым берём вариант 1 — «Первый».» — карточки «Первый» в
документе уже нет. Дефект БЛОК-5 воспроизведён дословно.

**На ветке `e4/p9-field-gate`:**
```
POST /requests/1/summary -> 200
recommendation_edited = False
caveats_edited = True
HTML содержит непровленную рекомендацию модели: False
HTML содержит правленую оговорку эксперта: True
```
Раздел «Рекомендация» печатает `SUMMARY_NOT_PRINTABLE_NOTICE` вместо текста
модели; раздел «Ограничения» печатает правленую оговорку эксперта. Дефект
закрыт по существу, не на словах.

## 2. Симметрия по всем четырём константам

* Рекомендация непровлена, оговорки провлены → рекомендация скрыта,
  оговорки печатаются (проверено выше живым маршрутом + `test_summary_field_gate.py`).
* Зеркало (правлена только рекомендация) → оговорки модели скрыты,
  рекомендация эксперта печатается (`test_правка_только_рекомендации_печатает_независимо_зеркало`,
  плюс дублирующий стенд-тест в `test_export_pdf_field_gate.py`/`test_export_docx_field_gate.py`).
* Машинные пометки РЕШ-210 (`PAID_LIMIT_NOTICE` и другие) печатаются
  независимо от обоих флагов — путь `limitations_lines(report,
  include_caveats=caveats_ok)` трогает только прозу модели
  (`test_render_print_html_machine_notice_survives_hidden_card_prose_does_not`,
  `test_render_print_html_intake_refine_notices_survive_hidden_card` в
  `test_export_pdf.py` — не тронуты по логике, только по именам полей).
* Обе заглушки РЕШ-212 (`SUMMARY_NOT_PRINTABLE_NOTICE` /
  `SUMMARY_NOT_PRINTABLE_NO_TABLE_NOTICE`) и честная строка
  `COMPARISON_TABLE_HIDDEN_NOTICE` выбираются по `view.comparison_table_hidden`
  в обоих экспортёрах (pdf.py, docx.py) и в шаблоне — см. мутацию Г ниже.

## 3. Ничего не потеряно

Побайтовое сравнение `render_print_html` на фикстуре без единой скрытой
карточки (две карточки с evidence, обе видимы, сводка не правлена):
вывод на базе `d3bf88b` и на ветке идентичен байт в байт (`diff` — пусто).
Отчёт, где эксперт правил оба поля, печатает оба
(`test_оба_поля_правлены_печатается_всё`, зеркальный тест в docx).

## 4. РЕШ-212: причина «спрятано», а не догадка

`_comparison_table_hidden` в `backend/app/export/view.py` явно проверяет три
условия: исходные строки были, хотя бы одна карточка отклонена, число строк
не совпало с числом карточек. Стенд-тест
`test_comparison_table_not_hidden_когда_строк_нет_с_самого_начала`
(в `tests/test_summary_field_gate.py`, через настоящий `build_export_view`)
ставит карточку с `REJECT` и `comparison_rows=[]` с самого начала —
`comparison_table_hidden` остаётся `False`, честная строка про таблицу не
печатается. Аналог без reject вовсе —
`test_comparison_table_not_hidden_без_отклонений`. Мутация Д (ниже) убила
именно это различение.

## 5. Мутации (поставлены лично, возвращены, `git diff` пуст)

| # | Мутация | Файл | Результат |
|---|---|---|---|
| А | Общий гейт вместо пополевого (`recommendation_edited or caveats_edited or …` в обеих функциях) | `view.py` | 7 failed |
| Б | `caveats_printable` возвращает `True` всегда | `view.py` | 5 failed |
| В | `recommendation_printable` возвращает `True` всегда | `view.py` | 9 failed |
| Г-pdf | `recommendation_notice = SUMMARY_NOT_PRINTABLE_NOTICE` без учёта `comparison_table_hidden` | `pdf.py` | 1 failed |
| Г-docx | `_add_recommendation_section(..., False)` — `table_hidden` игнорируется | `docx.py` | 1 failed |
| Д | `_comparison_table_hidden` → `return not edited_report.summary.comparison_rows` (без причины) | `view.py` | 3 failed |

После каждой мутации — фиксация падения, ручной откат (`git checkout --`),
проверка `git status --short` (пусто). Все шесть мутаций из задания (включая
пункт «признак спрятанной таблицы считается по `not comparison_rows` без
учёта причины») ловятся набором.

## 6. Переименование контракта

```
grep -rn "summary_text_edited\b" backend/ tests/ → только докстрока-упоминание
  термина словами в web/routes/review.py:344 (вне списка файлов задачи,
  код там не тронут — правка не входила в границы Э4-П9)
grep -rn "summary_text_printable\b" backend/ tests/ → пусто
grep -rn "summary_printable|summary_notice\b" backend/app/web/templates/ → пусто
```
Ни один вызывающий не применяет общий флаг к обоим полям — `pdf.py` и
`docx.py` читают `recommendation_printable`/`caveats_printable` раздельно и
передают их в разные разделы (`_add_recommendation_section`,
`_add_limitations_section` — не `_add_comparison_table`, которая печатается
всегда).

## 7. Инварианты и предел 500 строк

* №1 — карточки без `evidence` по-прежнему не печатаются (фильтр не тронут);
  фактических карточек с новыми фактами задача не добавляет.
* №2 — замок черновика (`DRAFT_NOTICE`, `is_draft`) не тронут ни в одном файле.
* №5 — машинные пометки РЕШ-210 печатаются всегда, независимо от гейта (§2 выше).
* №7 — `pytest -q` без обращения к сети (Chromium/`page.pdf` подменены в
  тестах, как и раньше).
* №8 — изменения `backend/` (5 файлов) сопровождены изменениями `tests/`
  (6 файлов, из них 3 новых).
* `wc -l tests/test_export_docx.py` → 500 — ровно на пределе, страж
  `tests/test_tests_layout.py` зелёный (см. «Проверено»); три новых файла
  (257/221/173 строк) ниже предела и не требуют записи в исключения.
* Три новых тестовых файла не дублируют друг друга: `test_summary_field_gate.py`
  — стенд через реальную БД и реальный маршрут (критерий 1, БЛОК-5, РЕШ-212 на
  уровне `ExportView`); `test_export_pdf_field_gate.py`/`test_export_docx_field_gate.py`
  — чистые тесты форматирования конкретного формата (HTML/DOCX-текст) на
  вручную собранном `ExportView`, без БД — второй набор нужен, потому что
  первый не проверяет, что попадает в реальный DOCX/PDF-документ конкретного
  формата (таблицы, заголовки), а обратное было бы дорогим дублированием БД-стенда.

## 8. Границы

```
git diff d3bf88b..e4/p9-field-gate --name-only:
  .planning/evidence/Э4-П9-handoff.yaml
  backend/app/export/docx.py
  backend/app/export/markdown.py
  backend/app/export/pdf.py
  backend/app/export/view.py
  backend/app/web/templates/report_print.html
  tests/test_export_docx.py
  tests/test_export_docx_field_gate.py
  tests/test_export_pdf.py
  tests/test_export_pdf_field_gate.py
  tests/test_summary_field_gate.py
  tests/test_summary_overrides.py
```
Совпадает со списком задачи Э4-П9 плюс три новых тестовых файла плюс handoff.
`verdicts.py`, `summary_overrides.py`, `web/routes/*`, `report.html`,
`schemas.py`, `.planning/gates/Э4.md` — не тронуты (`git diff --stat` по этим
путям пуст).

## 9. Язык — новые клиентские константы (на визу владельца)

1. `SUMMARY_NOT_PRINTABLE_NO_TABLE_NOTICE` (заглушка рекомендации, когда
   таблица тоже спрятана): «Сравнительная сводка не приводится: часть
   найденных вариантов снята экспертом при проверке. Ориентируйтесь на
   карточки ниже.»
2. `COMPARISON_TABLE_HIDDEN_NOTICE` (честная строка на месте спрятанной
   таблицы): «Таблица сравнения не построена: часть вариантов снята
   экспертом при проверке, соответствие строк карточкам не подтверждено.»
3. `SUMMARY_NOT_PRINTABLE_NOTICE` (не менялась, идёт на визу повторно как
   часть той же пары решений РЕШ-211/212): «Сравнительная сводка не
   приводится: часть найденных вариантов снята экспертом при проверке.
   Ориентируйтесь на таблицу сравнения и карточки ниже.»

Обе новые формулировки взяты дословно из постановки П-14 аудита и РЕШ-212 —
исполнитель не переписывал их своими словами. Тон деловой, без канцелярита.

## Проверено

* `pytest -q` → 1182 passed, 1 warning (starlette/httpx deprecation warning,
  не относится к задаче — предупреждение внешнего FastAPI testclient).
* `ruff check .` → All checks passed!
* `ruff format --check .` → 201 files already formatted
* `pytest tests/test_tests_layout.py tests/test_backend_layout.py -q` → 6 passed
* `pytest tests/test_team_docs.py -q` → 35 passed (YAML handoff разбирается)
* `wc -l tests/test_export_docx.py` → 500
* `grep -rE "sk-|gigachat|Api-Key" backend/app/export tests/test_summary_field_gate.py
  tests/test_export_pdf_field_gate.py tests/test_export_docx_field_gate.py
  tests/test_summary_overrides.py` → пусто
* `grep -rn "httpx" backend/app/export` → пусто
* Инварианты 1, 2, 5, 7, 8 — соблюдены (см. §7); остальные (3, 4, 6, 9, 10)
  вне зоны изменений этой задачи и не задеты диффом (промпты, модели,
  внешние источники не тронуты).

## Советы (не блокируют)

1. `web/routes/review.py:344` — докстрока `edit_summary` всё ещё называет
   старый общий концепт словами «делает `summary_text_edited` истинным»,
   хотя кода с таким именем в проекте больше нет. Смысл докстроки не
   пострадал (описывает поведение `resolve_summary_edit`, которое не
   менялось), но термин лучше поправить на «правку сводки» при следующей
   правке этого файла — сама задача Э4-П9 файл не трогала, и заходить в него
   ради одного слова было бы расширением границ.
2. Тест `test_export_pdf.py::test_render_print_html_summary_printable_when_expert_edited_recommendation`
   сохранил старое имя (`summary_printable`) в имени функции, хотя внутри
   давно передаётся `recommendation_edited`/`caveats_edited`. Не влияет на
   поведение, чисто именование — можно переименовать заодно со следующей
   правкой файла.

## Handoff

```yaml
handoff:
  from: reviewer
  to: orchestrator
  task: "Э4-П9"
  did:
    - "Прочитаны PLAN.md (раздел Э4-П9), audit-Э4-финал.md (БЛОК-5, П-14),
       DECISIONS.md (РЕШ-206, РЕШ-210, РЕШ-211, РЕШ-212), INVARIANTS.md,
       CLAUDE.md, весь diff d3bf88b..e4/p9-field-gate постатейно"
    - "Воспроизведён БЛОК-5 сам, через настоящий POST /requests/{id}/summary
       (TestClient(main.app)) и настоящий build_export_view: на базе d3bf88b
       непровленная рекомендация модели уезжает клиенту, на ветке — нет"
    - "Проверена симметрия по всем четырём константам печати (рекомендация,
       оговорки, машинные пометки РЕШ-210, обе заглушки РЕШ-212)"
    - "Побайтовое сравнение render_print_html на фикстуре без скрытых карточек
       — вывод идентичен базе d3bf88b"
    - "Проверено РЕШ-212: пустая comparison_rows без reject не поднимает
       comparison_table_hidden и не печатает честную строку про таблицу"
    - "Поставлены 5 мутаций из задания (общий гейт; caveats_printable=True;
       recommendation_printable=True; заглушка игнорирует table_hidden — в pdf.py
       и docx.py; comparison_table_hidden = not comparison_rows без причины) —
       все пять поймал набор; после возврата git diff пуст"
    - "grep по всему репозиторию: summary_text_edited/summary_text_printable
       нигде в коде не остались (только в докстроке review.py:344 словами,
       вне зоны этой задачи)"
    - "Проверены границы: diff совпадает с списком файлов задачи + 3 новых
       тестовых файла + handoff; verdicts.py, summary_overrides.py,
       web/routes/*, report.html, schemas.py, .planning/gates/Э4.md не тронуты"
    - "Прогнаны pytest -q, ruff check ., ruff format --check ."
    - "Проверен предел 500 строк (test_export_docx.py = 500, страж зелёный)"
  artifacts:
    - .planning/evidence/Э4-П9-review.md (в рабочей копии ревьюера)
    - .planning/evidence/Э4-П9-review-handoff.yaml (в рабочей копии ревьюера)
  self_audit: pass
  checks:
    - "pytest -q → 1182 passed, 1 warning (starlette/httpx deprecation, не относится к задаче)"
    - "ruff check . → All checks passed!"
    - "ruff format --check . → 201 files already formatted"
    - "pytest tests/test_tests_layout.py tests/test_backend_layout.py -q → 6 passed"
    - "pytest tests/test_team_docs.py -q → 35 passed"
    - "wc -l tests/test_export_docx.py → 500"
    - "Воспроизведение БЛОК-5 (TestClient + build_export_view): база d3bf88b —
       непровленная рекомендация в HTML ЕСТЬ; ветка — рекомендации в HTML НЕТ,
       правленая оговорка ЕСТЬ"
    - "diff render_print_html без скрытых карточек: repro_no_hidden(base) ==
       repro_no_hidden(branch), побайтово"
    - "Мутация А (общий гейт вместо пополевого): 7 failed → после возврата 0 failed, diff пуст"
    - "Мутация Б (caveats_printable всегда True): 5 failed → после возврата 0 failed, diff пуст"
    - "Мутация В (recommendation_printable всегда True): 9 failed → после возврата 0 failed, diff пуст"
    - "Мутация Г (заглушка игнорирует table_hidden, pdf.py): 1 failed; (docx.py): 1 failed → после возврата 0 failed, diff пуст"
    - "Мутация Д (comparison_table_hidden = not comparison_rows без причины): 3 failed → после возврата 0 failed, diff пуст"
  escalate: none
  read_first:
    - .planning/evidence/Э4-П9-review.md
    - backend/app/export/view.py
  notes: |
    Вердикт ПРИНЯТЬ, блокирующих нет. Три клиентские фразы (константы
    SUMMARY_NOT_PRINTABLE_NO_TABLE_NOTICE, COMPARISON_TABLE_HIDDEN_NOTICE и
    неизменная SUMMARY_NOT_PRINTABLE_NOTICE) идут владельцу на визу вместе
    с воротами — переданы в отчёте review.md.
```
