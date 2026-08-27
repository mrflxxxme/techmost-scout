# Ревью Э4-П1 · цикл 1
Вердикт: ПРИНЯТЬ (блокирующих 0, советов 1)

## Проверка по пяти критериям приёмки

1. **4 карточки, вторая — reject → 3 карточки, 3 строки, порядок сохранён.**
   Тест `test_visible_report_фильтрует_строку_сравнения_вместе_с_карточкой`
   покрывает буквально. Проверено вручную кодом критерия.
2. **Число строк не совпадает с числом карточек — строки не трогаем.**
   `test_visible_report_не_трогает_строки_при_несовпадении_числа`.
   Дополнительно проверено вручную: отчёт с 0 карточками и 1 строкой сводки
   не падает и не режет строку (несовпадение 0 ≠ 1).
3. **Пустые `comparison_rows` и отчёт без reject не меняются.**
   `test_visible_report_пустые_строки_сравнения_без_reject_не_меняются`.
4. **`contest_cards` не влияют на строки сравнения.**
   `test_visible_report_reject_в_contest_cards_не_трогает_строки_сравнения`.
   В коде подтверждено: `_visible_summary` жёстко зовёт
   `_kept_positions(main_cards, card_ids, effective, is_contest=False)` —
   ключи `(True, n)` конкурсной секции в этот подсчёт не попадают в принципе,
   независимо от числа карточек в `contest_cards`.
5. **Адверсариальный тест на «фильтр по позиции после фильтра карточек».**
   `test_visible_report_не_ловится_на_фильтр_по_позиции_после_фильтра_карточек`
   — подобран так, что ошибочная реализация (пересчёт позиции по уже
   отфильтрованному списку) даёт другой набор строк, чем правильная.
   Подтверждено мутацией (см. ниже).

## Мутационная проверка (не декоративность тестов)

Мутации ставились точечно в `backend/app/verdicts.py` в изолированном клоне,
после каждой — `pytest -q tests/test_verdicts.py`, затем файл возвращён к
исходному состоянию (`git diff` после возврата — пусто).

| Мутация | Результат |
|---|---|
| (а) фильтровать строки по позициям уже отфильтрованного `result.cards` вместо исходного `report.cards` | 2 failed: `..._вместе_с_карточкой`, `..._не_ловится_на_фильтр_по_позиции...` |
| (б) всегда убирать первую строку (`rows[1:]`) | те же 2 failed |
| (в) не фильтровать строки вовсе (`result.summary = report.summary`) | те же 2 failed |
| (г) фильтровать, даже когда число строк не совпадает с числом карточек (убрана проверка `len(rows) != len(main_cards)`) | 1 failed: `..._не_трогает_строки_при_несовпадении_числа` |

Все четыре мутации ловятся набором `tests/test_verdicts.py` без необходимости
полного прогона. Тесты не декоративны.

## Соответствие «строка ↔ карточка» и стык с `export/view.py`

- Позиции считаются по **исходному** `report.cards` (до фильтра): в
  `visible_report` вызов — `_visible_summary(report.summary, report.cards, ...)`,
  не `result.cards`. Подтверждено и мутацией (а) выше.
- `_kept_positions` — общая точка решения «оставить/убрать» внутри
  `verdicts.py`, переиспользуется и `_visible_section` (карточки), и
  `_visible_summary` (строки). Это не третье место правды, а ровно
  наоборот: до правки решение о REJECT принималось в одном месте
  (`_visible_section`), после — в одном общем `_kept_positions`, которым
  пользуются оба потребителя. Число мест принятия решения внутри модуля не
  выросло.
- `export/view.py::_reindex_after_filter` — по-прежнему отдельная (не новая,
  существовавшая до этой задачи) реализация того же обхода с той же проверкой
  `decision == REJECT`, нужная только для пересчёта `card_ids`/`rework_keys`
  под уже отфильтрованные позиции. Она не имеет отношения к
  `comparison_rows` и не расходится с `verdicts.py` в решении о видимости
  карточки — обе читают один и тот же `effective` (результат
  `effective_verdicts`) и одно и то же условие REJECT. Третьего, отдельного
  от этих двух, места принятия решения diff не создал. `view.py` в задаче
  Э4-П1 не в списке файлов и не тронут — правильно, трогать его не следовало.

## Отчёт без вердиктов / пустая сводка / без карточек / rework

Проверено вручную (не только штатными тестами):
- `cards=[]`, `card_ids={}`, `effective={}` → `cards == []`,
  `comparison_rows == []`, без исключений.
- `cards=[]`, но `comparison_rows=[{"a":"b"}]` (искусственное несовпадение
  0 карточек / 1 строка) → строки не тронуты, `IndexError` не возникает.
- Вердикт `rework` на карточке при непустой и совпадающей по длине
  `comparison_rows` → и карточка, и её строка сравнения остаются на месте
  (`rework` не входит в условие REJECT ни в `_kept_positions`, ни в
  проверке видимости самой карточки).
Поведение по этим четырём сценариям не изменилось относительно требования
задачи и относительно поведения `_visible_section`, которое было раньше.

## Инварианты и грабли

- №7 (сеть в тестах): `test_verdicts.py` строит `VerdictRow`/`Report` вручную,
  без БД и HTTP — соответствует докстроке модуля.
- №8 (backend + tests вместе): изменены `backend/app/verdicts.py` и
  `tests/test_verdicts.py` синхронно.
- Предел 500 строк: `verdicts.py` — 144 строки, `test_verdicts.py` — 260
  (тестовые файлы под этим пределом не ходят, но и не превышают ничего
  подозрительного).
- `tests/test_tests_layout.py`, `tests/test_backend_layout.py`: зелёные.
- Грабли §0.1 SPEC.md (незаполненные `{{...}}`, `max_tokens`, YAML с
  двоеточием, `extract_json_block`, адрес GigaChat) — к этому диффу не
  относятся: код не работает с промптами, LLM или YAML-контентом.
- Секреты (№3) и `httpx` вне слоёв (№6): диффа не касаются, изменённые файлы
  не содержат ни того, ни другого.
- `tests/test_team_docs.py` (страж №10, кавычки в YAML) — зелёный, значит и
  новый `.planning/evidence/Э4-П1-handoff.yaml` оформлен корректно.

## Границы задачи

`git diff a495b1d..e4/p1-summary-filter --name-only`:
```
.planning/evidence/Э4-П1-handoff.yaml
backend/app/verdicts.py
tests/test_verdicts.py
```
Ровно список файлов задачи из `PLAN.md` плюс handoff в evidence. Выхода за
границы нет.

## Handoff исполнителя

`self_audit: pass`, `escalate: none` — комбинация непротиворечива. Артефакты
(`backend/app/verdicts.py`, `tests/test_verdicts.py`) существуют и совпадают
с диффом. Числа в `checks` (16 passed на модуле, 1080 passed на всём наборе,
`ruff check`/`ruff format` чисто, размеры файлов) воспроизведены мной
независимо и совпали.

## Советы (не блокируют)

1. `export/view.py::_reindex_after_filter` содержит собственную копию условия
   `decision == VerdictDecision.REJECT`, синхронизированную с `verdicts.py`
   только вручную (комментарий в коде это признаёт). Это долг, появившийся до
   Э4-П1, не в её границах — но кандидат на то, чтобы в задаче, которая в
   следующий раз тронет `view.py` (например, Э4-П2), переиспользовать
   `verdicts._kept_positions` вместо параллельного обхода, а не чинить его
   независимо второй раз.

## Проверено (фактический вывод)

```
$ pytest -q
1080 passed, 1 warning in 70.57s

$ pytest -q tests/test_verdicts.py -v
16 passed

$ ruff check .
All checks passed!

$ ruff format --check .
194 files already formatted

$ pytest -q tests/test_backend_layout.py tests/test_tests_layout.py
6 passed

$ pytest -q tests/test_team_docs.py
35 passed

$ wc -l backend/app/verdicts.py tests/test_verdicts.py
  144 backend/app/verdicts.py
  260 tests/test_verdicts.py

$ git diff a495b1d..e4/p1-summary-filter --name-only
.planning/evidence/Э4-П1-handoff.yaml
backend/app/verdicts.py
tests/test_verdicts.py

$ grep -rE "sk-|gigachat|Api-Key" backend/ tests/
(совпадения только в существующих, не тронутых этим диффом местах —
llm/yandex.py, search/yandex_search.py, config.py, тестовые фикстуры;
значения секретов нигде не выведены)

$ grep -rn "httpx" backend/app | grep -v "/llm/" | grep -v "/search/"
cli_doctor.py, pipeline/pipeline_search.py, pipeline/pipeline_context.py
(оркестратор/CLI-диагностика, не тронуты этим диффом)
```

Мутационная проверка (а)-(г) на `_visible_summary`/`_visible_report` —
все четыре мутации ловятся `tests/test_verdicts.py`, файл после проверки
возвращён к исходному состоянию (`git diff` — пусто).
