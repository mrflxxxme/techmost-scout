# Ревью Э4-П15 · правки пятой проверки аудита (П-23…П-26) · цикл 2 (финальный)

Вердикт: **ПРИНЯТЬ** (блокирующих 0, советов 1)

Дерево: ветка `e4/p15-audit5-fixes`, верхушка `7b93f01` (коммит правки —
`6c989fc`), база `416bf95`. Клон обновлён до верхушки в
`/tmp/.../scratchpad/p15review`; основной каталог и боевая база не
открывались. Это цикл 2 — последний, третьего не будет; блокирующего нет,
эскалация не требуется.

## Главный вопрос цикла 1 — снят

Цикл 1 заблокировал задачу на `hidden_reason(None, verdict)` в
`_reindex_after_filter`: вызов лгал сигнатуре `card: SolutionCard` и держался
на короткого замыкания `require_evidence and not card.evidence`, которое
работало только пока единственный вызов жёстко нёс `require_evidence=False`.

Исполнитель сузил тип до `card: SolutionCard | None` и добавил явную охрану:

```python
if require_evidence:
    if card is None:
        raise ValueError(
            "hidden_reason: require_evidence=True требует переданную карточку, "
            "а получен None — без неё решение о пустом evidence принять нечем"
        )
    if not card.evidence:
        return "no_evidence"
```

Из двух направлений, предложенных ревью цикла 1 (отдельная функция без
`card`, либо сузить тип и упасть явно), выбрано второе; первое отклонено
обоснованно — вынос условия `REJECT` завёл бы второе место, решающее «скрыта
ли карточка», а именно от вторых мест этот этап отбивался четыре волны подряд
(П-11…П-15, РЕШ-213/214/215). Логика согласуется с духом РЕШ-215: «единственный
источник причины скрытия» — про одну реализацию с явным контрактом, а не про
единственную обязательную сигнатуру параметра.

### Проверено лично, не на слово

**1. Сигнатура и докстрока называют контракт.** `card: SolutionCard | None` —
явно в сигнатуре (`backend/app/verdicts.py:117-121`). Докстрока функции
(строки 132-150) и докстрока модуля (строки 22-28) прямо говорят: карточка
нужна только под `require_evidence=True`; без флага (единственный вызов из
`_reindex_after_filter`) `None` — законное значение; с флагом и `None` —
`ValueError`. Контракт виден из сигнатуры и текста, не нужно читать тело
функции, чтобы его понять.

**2. `ValueError` — до работы с карточкой, только в описанном случае, текст
человеческий.** Порядок в теле: проверка `REJECT` (не трогает `card`) → блок
`if require_evidence:` → внутри него `if card is None: raise` **первой
строкой**, до `if not card.evidence`. Прогнал:
```
>>> hidden_reason(None, None, require_evidence=True)
ValueError: hidden_reason: require_evidence=True требует переданную карточку,
а получен None — без неё решение о пустом evidence принять нечем
```
Текст по-русски, объясняет причину и следствие, не техножаргон. Проверил
также, что `require_evidence=True` с настоящей карточкой (без пустого
`evidence`) не падает и не задет — только `card is None` при включённом
флаге поднимает исключение, остальные три сочетания флага/карточки отрабатывают
как раньше (`hidden_reason(None, verdict)`,
`hidden_reason(None, verdict, require_evidence=False)`,
`hidden_reason(card, verdict, require_evidence=True)`).

**3. Три мутации цикла 1 — поставлены заново на новом коде.**

*П-23* (`_comparison_table_hidden`, `require_evidence=True → False` в вызове
`visible_position_count`):
```
$ pytest -q tests/test_export_view_p15_audit5.py
FAILED …test_comparison_table_hidden_карточка_скрыта_только_пустым_evidence
AssertionError: assert False is True
1 failed, 1 passed
```
Откат (`git checkout -- backend/app/export/view.py`), `git diff` пуст.

*П-24* (порядок причин в `hidden_reason` — переставлены местами блок
`require_evidence`/`ValueError`/`no_evidence` и проверка `REJECT`):
```
$ pytest -q tests/test_verdicts.py
FAILED …test_hidden_reason_reject_важнее_пустого_evidence
AssertionError: assert 'no_evidence' == 'rejected'
1 failed, 25 passed
```
Откат (`git checkout -- backend/app/verdicts.py`), `git diff` пуст.

*П-25* (код `_reindex_after_filter` до правки — `git show
416bf95:backend/app/export/view.py > backend/app/export/view.py`):
```
$ pytest -q tests/test_export_view_p15_audit5.py
FAILED …test_reindex_after_filter_переживает_рассинхрон_строк_с_report_json
IndexError: list index out of range
  at backend/app/export/view.py:309, in _reindex_after_filter: card = cards[key[1]]
1 failed, 1 passed
```
Откат, `git diff` пуст, `git status --porcelain` чист.

Все три мутации по-прежнему краснеют именно на заявленных тестах — на новом
коде цикла 2 они не «случайно ожили» другой причиной.

**4. Новая мутация — снята проверка `card is None`, возврат к опоре на
короткое замыкание:**
```python
-    if require_evidence:
-        if card is None:
-            raise ValueError(...)
-        if not card.evidence:
-            return "no_evidence"
+    if require_evidence and not card.evidence:
+        return "no_evidence"
```
```
$ pytest -q tests/test_verdicts.py
FAILED …test_hidden_reason_require_evidence_без_карточки_падает_явно
AttributeError: 'NoneType' object has no attribute 'evidence'
  at backend/app/verdicts.py:155
1 failed, 25 passed
```
Именно тест на явный контракт (`pytest.raises(ValueError, match=
"require_evidence=True")`) краснеет, и краснеет ровно тем способом, который
охрана обязана поймать — код без явной проверки роняется `AttributeError`,
а не поднимает ожидаемый `ValueError`. Откат (`git checkout -- backend/app/
verdicts.py`), `git diff` пуст.

**5. Поведение не сдвинулось.** `tests/test_summary_field_gate.py -k reindex`
(сценарий reject + rework + пустой `evidence` + конкурсная секция; сверяет
`card_ids`, `rework_keys`, `view.report`, `view.client_report`) — **2 passed**
на верхушке цикла 2 и **2 passed** с идентичными ассертами на коде базы
`416bf95` (`git show 416bf95:backend/app/export/view.py > …`, затем
восстановлен) — тот же стенд, что и в цикле 1, перепрогнан на новом коде.
`export/view.py` в коммите цикла 2 (`6c989fc`) не менялся вовсе (правка только
в `verdicts.py` и `tests/test_verdicts.py` — см. `git show --stat 6c989fc`),
поэтому поведение `_reindex_after_filter` идентично циклу 1 по построению;
пересверил стенд, а не поверил этому факту на слово.

## Границы (санкционированное расширение проверено)

```
$ git diff --name-only 416bf95..e4/p15-audit5-fixes-c2
.planning/evidence/Э4-П15-handoff.yaml
backend/app/export/view.py
backend/app/verdicts.py
backend/app/web/routes/review.py
tests/test_export_view_p15_audit5.py
tests/test_verdicts.py
```
Ровно список из сообщения оркестратора, ничего сверх. `backend/app/verdicts.py`
добавлен в границы задачи явным указанием координатора цикла 2 — расширение
не самовольное, исполнитель зафиксировал это отдельным пунктом `did` и
`notes` в handoff.

## Инварианты

Задача не касается фактов карточек, промптов, источников — пункты 1, 4, 5
неприменимы (как и в цикле 1). Остальные:

3. `grep -rE "sk-|gigachat|Api-Key" backend/ tests/` — только существующий код
   авторизации Yandex и тесты-негативы (проверяют отсутствие утечки), значений
   секретов нет; `.env`/`.env.*` в `.gitignore`.
6. `grep -rln httpx backend/app` (без `__pycache__`) — карта не изменилась:
   `llm/`, `search/`, `pipeline/pipeline_context.py`,
   `pipeline/pipeline_search.py`, `cli_doctor.py`.
7. `pytest -q` — 1210 passed; тот же результат при заблокированных сокетах
   (плагин, форсирующий `OSError` на любое сетевое обращение) — 1210 passed.
8. Изменения в `backend/` — есть изменения в `tests/` (`test_verdicts.py`,
   `test_export_view_p15_audit5.py` без изменений в этом коммите, но покрытие
   на месте).
9. Diff сверен с сообщением координатора — см. «Границы» выше.
10. `pytest tests/test_team_docs.py` — зелёный в общем прогоне стражей ниже.

## Прочее

* Предел 500 строк: `backend/app/verdicts.py` — 282 строки (было 214),
  `backend/app/export/view.py` — 326, `backend/app/web/routes/review.py` — 393.
  В пределе.
* Стражи layout (`test_backend_layout.py`, `test_scripts_layout.py`,
  `test_pipeline_layout.py`, `test_tests_layout.py`, `test_team_docs.py`) —
  **49 passed**.
* `ruff check .` → All checks passed!
* `ruff format --check .` → 205 files already formatted.
* `pytest -q` → **1210 passed** (заявленное число воспроизведено, было 1208
  на верхушке цикла 1, +2 новых теста цикла 2).

## Совет (не блокирует)

1. Докстрока `_reindex_after_filter` (`backend/app/export/view.py:297-308`)
   не обновлена в коммите цикла 2 и по-прежнему объясняет безопасность
   передачи `None` цитатой кода, которого в `hidden_reason` больше нет
   (`if require_evidence and not card.evidence` — это структура ДО цикла 2,
   сейчас там `if require_evidence: if card is None: raise …`). Ложных
   утверждений о поведении нет (при `require_evidence=False`, единственном
   значении здесь, карточка по-прежнему не читается — это по-прежнему верно
   и даже более прямо видно в новой структуре кода), только устаревшая
   цитата и рамка объяснения через «опору на порядок проверок», хотя теперь
   есть явный контракт типа, которым естественнее было бы объяснять. Не
   блокирует: не искажает поведение, не вводит в заблуждение о корректности —
   но при следующей правке рядом стоит поправить заодно.

## Прогнанные проверки (фактический вывод)

```
pytest -q                                              → 1210 passed, 1 warning (httpx/starlette deprecation, не наше)
pytest -q (сокеты заблокированы)                       → 1210 passed
ruff check .                                           → All checks passed!
ruff format --check .                                  → 205 files already formatted
стражи layout ×4 + team_docs                           → 49 passed
tests/test_summary_field_gate.py -k reindex (HEAD c2)   → 2 passed
tests/test_summary_field_gate.py -k reindex (база 416bf95) → 2 passed, ассерты идентичны
Мутация П-23 (require_evidence True→False)             → 1 failed (нужный тест), код возвращён, diff пуст
Мутация П-24 (порядок веток в hidden_reason)            → 1 failed (нужный тест), код возвращён, diff пуст
Мутация П-25 (код 416bf95 в _reindex_after_filter)      → 1 failed IndexError (нужный тест), код возвращён, diff пуст
Мутация новая (снята проверка card is None)             → 1 failed AttributeError на ожидающем ValueError тесте, код возвращён, diff пуст
git diff --name-only 416bf95..HEAD                     → ровно санкционированный список файлов
grep -rln httpx backend/app (без __pycache__)           → llm/, search/, pipeline_context.py, pipeline_search.py, cli_doctor.py
grep -rE "sk-|gigachat|Api-Key" backend/ tests/         → только код авторизации и тесты-негативы, секретов нет
```

Это цикл 2 — последний. Блокирующего нет, третьего цикла не требуется, ворота
этапа готовы к подписи по этой задаче.
