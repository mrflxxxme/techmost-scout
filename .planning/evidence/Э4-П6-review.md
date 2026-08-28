# Ревью Э4-П6 · машинные оговорки печатаются всегда · цикл 2 (финальный)

Вердикт: **ПРИНЯТЬ**

Циклов ревью для этой задачи было отведено два, это второй. Блокирующее
замечание цикла 1 закрыто; новых блокирующих не найдено. Третьего цикла нет.

## Что проверено (независимо, не на слово)

Работа велась в изолированном клоне (`git fetch` + `git reset --hard
origin/e4/p6-machine-caveats` поверх клона цикла 1), ветка обновлена до
`023760e`. Основной каталог не трогал.

1. **Диф цикла 2 не содержит кода продукта.**
   `git diff 19952ca..023760e -- backend/` → **пусто**. Полный `--stat`
   диффа цикла 2: только `tests/test_export_pdf.py` (+20 строк) и
   `.planning/evidence/Э4-П6-handoff.yaml`. `test_export_docx.py` в дифф
   цикла 2 не входит: `git diff 19952ca..023760e -- tests/test_export_docx.py`
   → пусто (файл действительно не трогали).

2. **Обе мутации цикла 1 поставлены заново, каждая ловится своим параметром.**
   - Убрал `INTAKE_FALLBACK_CAVEAT` из `_machine_caveats()` →
     `pytest tests/test_export_pdf.py -k intake_refine` →
     **1 failed, 1 passed**: упал ровно параметр с `INTAKE_FALLBACK_CAVEAT`
     (`AssertionError: assert 'Уточнения по задаче не собраны...' in <html>`),
     параметр с `REFINE_FALLBACK_CAVEAT` остался зелёным. Откат: `git diff
     backend/app/export/markdown.py` → пусто.
   - Убрал `REFINE_FALLBACK_CAVEAT` → та же команда → **1 failed, 1 passed**:
     упал ровно параметр с `REFINE_FALLBACK_CAVEAT` (`AssertionError: assert
     'Постановка задачи осталась черновой...' in <html>`), параметр с
     `INTAKE_FALLBACK_CAVEAT` остался зелёным. Откат: `git diff
     backend/app/export/markdown.py` → пусто.

   Оба отката проверены `git status --short` — рабочее дерево чистое (кроме
   собственного untracked-отчёта ревьюера).

3. **Третья сторона: `PAID_LIMIT_NOTICE`.** Убрал константу из множества
   `_machine_caveats()`, прогнал **весь** набор (не точечно) —
   `pytest -q` → **4 failed, 1136 passed**: упали
   `test_export_docx.py::test_машинная_пометка_и_учёт_конкурсных_карточек`,
   `test_export_markdown.py::test_limitations_lines_false_keeps_machine_notice_drops_prose`,
   `test_export_pdf.py::test_render_print_html_machine_notice_survives_hidden_card_prose_does_not`,
   `test_export_pdf.py::test_render_print_html_limitations_section_appears_for_machine_notice_only`.
   Итого все четыре машинные пометки теперь защищены от тихого выпадения из
   множества — `PAID_LIMIT_NOTICE` даже избыточно (4 теста), `SUMMARY_FALLBACK_NOTICE`
   одним (`test_fallback_summary_caveat_recognized_via_shared_constant`, цикл 1),
   `INTAKE_FALLBACK_CAVEAT`/`REFINE_FALLBACK_CAVEAT` — новым параметризованным
   тестом цикла 2, каждая своим параметром. Откат подтверждён.

4. **Числа воспроизведены независимо:**
   - `pytest -q` → **1140 passed** (заявлено 1140 — совпадает; было 1138 после
     цикла 1, +2 параметра нового теста).
   - `ruff check .` → **All checks passed!**
   - `ruff format --check .` → **198 files already formatted**.
   - `pytest tests/test_backend_layout.py tests/test_tests_layout.py
     tests/test_pipeline_layout.py -q` → **12 passed**.
   - `wc -l tests/test_export_pdf.py` → **479** (заявлено 479 — совпадает).

5. **`tests/test_export_docx.py` остался на 500 строках, страж зелёный.**
   `wc -l tests/test_export_docx.py` → **500**. `test_tests_layout.py`
   использует строгое `>` при сравнении с лимитом — 500 проходит, что уже
   отмечалось советом в цикле 1 (не блокирует, файл не трогали).

## Дополнительно перепроверено (не выборочно, чтобы не полагаться на память
цикла 1)

- Полный набор `pytest -q` на чистом дереве после всех откатов —
  **1140 passed**, без побочных повреждений от serии ручных мутаций.
- `pytest tests/test_team_docs.py` — **35 passed**; `Э4-П6-handoff.yaml`
  парсится `yaml.safe_load` без ошибок (инвариант №10).
- `grep -rE "sk-|gigachat|Api-Key"` по новому тесту и handoff цикла 2 —
  совпадений с боевыми значениями нет (инвариант №3).
- Продуктовые файлы задачи (`markdown.py`, `pdf.py`, `docx.py`,
  `pipeline/summary.py`) идентичны версии, принятой по существу в цикле 1
  (кроме единственного найденного и теперь закрытого пробела в тестовом
  покрытии) — размеры файлов не изменились (412/148/245/108 строк).

## Итог

Единственное блокирующее замечание цикла 1 — отсутствие индивидуальной
защиты `INTAKE_FALLBACK_CAVEAT` и `REFINE_FALLBACK_CAVEAT` в множестве
`_machine_caveats()` — закрыто точечным параметризованным тестом без
изменения кода продукта, что и заявлял исполнитель. Проверено собственными
мутациями, а не на слово. Все пять критериев приёмки задачи (план,
Э4-П6) подтверждены в цикле 1 и не пересматривались (правка цикла 2 их не
затрагивает). Советов из цикла 1 (место теста на общую константу и
предельные 500 строк в `test_export_docx.py`) — по-прежнему не блокируют,
пересмотра не требуют.

## Handoff

```yaml
handoff:
  from: reviewer
  to: orchestrator
  task: "Э4-П6"
  cycle: 2
  did:
    - "Обновил изолированную копию до 023760e (git fetch + reset --hard на origin/e4/p6-machine-caveats)"
    - "Подтвердил: диф цикла 2 не содержит кода продукта (git diff 19952ca..023760e -- backend/ пуст), tests/test_export_docx.py не тронут"
    - "Заново поставил обе мутации цикла 1 (убрать INTAKE_FALLBACK_CAVEAT; убрать REFINE_FALLBACK_CAVEAT) — каждая роняет свой параметр нового теста, откат чист (git diff пуст)"
    - "Проверил третью сторону: мутация PAID_LIMIT_NOTICE ловится 4 существующими тестами; все четыре машинные пометки теперь защищены индивидуально"
    - "Числа воспроизведены независимо: pytest -q 1140 passed, ruff check/format чисты, layout-стражи 12 passed, test_export_pdf.py 479 строк, test_export_docx.py 500 строк (страж зелёный, строгое >)"
  artifacts:
    - ".planning/evidence/Э4-П6-review.md (в рабочей копии ревьюера; закоммитит оркестратор)"
  self_audit: "pass"
  checks:
    - "git diff 19952ca..023760e -- backend/: пусто"
    - "pytest -q: 1140 passed"
    - "ruff check .: All checks passed!"
    - "ruff format --check .: 198 files already formatted"
    - "pytest tests/test_backend_layout.py tests/test_tests_layout.py tests/test_pipeline_layout.py -q: 12 passed"
    - "мутация (убрать INTAKE_FALLBACK_CAVEAT): pytest tests/test_export_pdf.py -k intake_refine -> 1 failed (INTAKE), 1 passed (REFINE); откат чист"
    - "мутация (убрать REFINE_FALLBACK_CAVEAT): pytest tests/test_export_pdf.py -k intake_refine -> 1 failed (REFINE), 1 passed (INTAKE); откат чист"
    - "мутация (убрать PAID_LIMIT_NOTICE): pytest -q -> 4 failed, 1136 passed; откат чист"
    - "wc -l tests/test_export_pdf.py: 479; wc -l tests/test_export_docx.py: 500"
  escalate: "none"
  read_first:
    - ".planning/evidence/Э4-П6-review.md (этот отчёт)"
    - "backend/app/export/markdown.py (_machine_caveats)"
    - "tests/test_export_pdf.py (test_render_print_html_intake_refine_notices_survive_hidden_card)"
  notes: >-
    Финальный цикл (предел 2 исчерпан) — вердикт ПРИНЯТЬ. Задача Э4-П6
    закрывает БЛОК-4 повторного аудита Э4 по существу: все пять критериев
    приёмки подтверждены независимо, обе выявленные в цикле 1 дыры в
    мутационном покрытии закрыты без изменения кода продукта, границы задачи
    не нарушены. Советы (место теста на общую константу; test_export_docx.py
    ровно на пределе 500 строк) не блокируют и адресатов дальше не имеют —
    можно закрывать задачу.
```
