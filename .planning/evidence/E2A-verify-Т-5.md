# Верификация Т-5

Вердикт: **PASS**

## Критерии приёмки

| № | Критерий из PLAN.md | Как проверен | Фактический вывод | Статус |
|---|---|---|---|---|
| 1a | `pytest tests/test_persistence.py -v` зелёный | штатная команда из критерия | `10 passed in 6.59s`, все 10 PASSED | взят |
| 1b | Карточка с непустыми `requirement_checks`, `unsourced_claims`, `pdf_urls` пишется и читается обратно **без потерь** на временной базе | `test_new_card_fields_persist_without_loss` (чтение в **новой** сессии из файла `tmp_path`, точное равенство списков словарей) + независимая проба сырым SQL | состав: у каждой проверки все 4 ключа `['evidence_url','note','requirement','status']`; форма: `status` = `confirmed`/`not_confirmed`, `evidence_url` = строка либо `null`, отсутствующий `note` = `""`, URL — строки; порядок: `['R1','R2','R3']` сохранён, `["u1","u2","u3"]` и оба PDF в исходном порядке | взят |
| 1c | Карточка **без** этих полей даёт **пустые списки, а не NULL** | `test_card_without_new_fields_persists_empty_lists_not_null` + `test_new_columns_default_to_empty_list_on_bare_insert` (вставка строки в обход `persist_run`) + проба сырым SQL | в базе лежит литерал `'[]'`, `typeof()` = `text`, `any NULL? [False, False, False]` — и через `persist_run`, и на «голой» вставке | взят |
| 2a | `pytest -q` зелёный | штатная команда | `202 passed, 1 warning in 67.92s` — совпадает с прогонами оркестратора и ревьюера | взят |
| 2b | `ruff check .` — 0 замечаний | штатная команда | `All checks passed!`, exit 0 | взят |
| 2c | `ruff format --check .` — 0 замечаний | штатная команда | `113 files already formatted`, exit 0 | взят |

Расхождений с числами оркестратора и ревьюера нет: 202 / чисто / 113.

## Инварианты

**№1** (в редакции задачи: в БД попадает ровно то же, что печатается в
markdown) — **подтверждён**. Сверка `export/markdown.py:100–152` с
`persistence.py:126–128`: markdown печатает `requirement_checks` (требование,
статус, ссылка при `confirmed`, примечание), `unsourced_claims` и `pdf_urls` —
все три пишутся в базу. Проба сырым SQL показала, что в базу уходят и записи со
статусом `not_confirmed`, и `evidence_url = null`, то есть карточка в базе
больше не «увереннее» отчёта — ровно тот дефект, ради которого задача ставилась.

**№7** (без сети, временная база) — **подтверждён**. База — файл в `tmp_path`,
боевой `data/scout.db` не создаётся (в `data/` только `gov_support.yaml` и
`ru_bundle.pem`). Сверх этого прогнан `tests/test_persistence.py` с
`HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` на закрытый порт `127.0.0.1:1`:
`10 passed`, exit 0 — реального сетевого вызова в тестах нет.

**№8** (новое поведение с тестом) — **подтверждён**.
`git diff session/E2 --stat`: `backend/app/models.py +6`,
`backend/app/pipeline/persistence.py +6`, `tests/test_persistence.py +127/-1`.
На три новые колонки — три новых теста.

**№3** (секретов нет) — **подтверждён**. `grep -riE "sk-|gigachat|Api-Key"
backend/ tests/` даёт только имена настроек, документацию и заглушку `sk-test` в
`tests/conftest.py` (существовала до задачи). Диффом Т-5 не добавлено ни одного
значения.

**№6** (httpx только в `llm/`, `search/`, оркестраторе) — задачей не затронут:
ни `models.py`, ни `persistence.py` httpx не импортируют, дифф его не добавляет.

**№9** (границы MVP) — **подтверждён**. Дифф не выходит за три файла, названные
в PLAN.md; остальные четыре таблицы не наполняются, Alembic и миграции не
вводятся. Требование п. 3 задачи выполнено: строка про отсутствие миграций
стоит в докстроке `persistence.py`.

№2, №4, №5, №10 к задаче не относятся; нарушений в диффе нет.

## Отложено

Нет. Все проверки выполнимы, `DLG-N` не открывался.

## Чем подтверждено

```
$ pytest tests/test_persistence.py -v
10 passed in 6.59s
  test_new_card_fields_persist_without_loss PASSED
  test_card_without_new_fields_persists_empty_lists_not_null PASSED
  test_new_columns_default_to_empty_list_on_bare_insert PASSED

$ pytest -q
202 passed, 1 warning in 67.92s

$ ruff check .
All checks passed!                       (exit 0)

$ ruff format --check .
113 files already formatted              (exit 0)

$ pytest tests/test_persistence.py -q   [HTTP(S)_PROXY=127.0.0.1:1]
10 passed in 7.18s                       (exit 0)

Проба сырым SQL (SELECT ... typeof(...) FROM solution_cards):
  ROW full: requirement_checks = '[{"requirement":"R1","status":"confirmed",
            "evidence_url":"https://doi.org/10.1/x","note":"n1"},
            {"requirement":"R2","status":"not_confirmed","evidence_url":null,"note":"n2"},
            {"requirement":"R3","status":"confirmed",
             "evidence_url":"https://doi.org/10.1/y","note":""}]'   sqltype=text
            unsourced_claims = '["u1","u2","u3"]'   pdf_urls = '[...1.pdf, ...2.pdf]'
            any NULL? [False, False, False]
  ROW bare: '[]' / '[]' / '[]'   sqltype=text   any NULL? [False, False, False]
  ORDER preserved: True · unsourced order/form: True · pdf order/form: True

$ git status --short
(пусто)
```

## Причина PASS

Закрыты все шесть проверок из двух критериев приёмки. Ключевое слово критерия —
«без потерь» — проверено не фактом успешного вызова, а составом, формой и
порядком значений после кругового хода, подтверждёнными независимо от тестов
чтением сырых значений колонок.

*Проверка оркестратора: рабочая копия `E2A-T5` после верификации чиста по
`git status`, проба верификатора лежала в сессионном скретчпаде вне репозитория.
Запрет «кто проверяет — тот не правит» соблюдён.*
