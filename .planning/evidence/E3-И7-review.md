# Ревью И-7 · цикл 1
Вердикт: ДОРАБОТАТЬ (блокирующих 1, советов 3)

## Блокирующие

1. [инвариант №8, критерий приёмки 1 «исходный объект отчёта не мутируется»]
   `backend/app/report_overrides.py:47` — `report.model_copy(deep=True)`
   действительно защищает вложенные изменяемые поля (`risks`, `evidence`,
   `contact_leads`) **непереписанных** карточек от протекания мутации в
   исходный `Report`, но этот факт не проверен ни одним тестом. Мутационная
   проверка: заменил `deep=True` на `deep=False` — `pytest
   tests/test_report_overrides.py -v` (10 тестов) прошёл без единого падения,
   и полный набор `pytest -q` (898 тестов) тоже прошёл целиком. Ни один тест
   не отличает глубокую копию от мелкой.
   Воспроизведение показывает реальный риск, а не теоретический: карточка,
   которую наложение не тронуло (`result.cards[1]`), при `deep=False`
   оказывается тем же объектом, что и `report.cards[1]`
   (`result.cards[1] is report.cards[1] == True`); мутация
   `result.cards[1].risks.append(...)` после вызова `apply_overrides`
   бесшумно меняет исходный `report.cards[1].risks`. При штатном
   `deep=True` (то, что реально в коде) та же операция исходный объект не
   трогает — код написан правильно, но регрессию (например, случайную
   замену `deep=True` на `deep=False` при будущей правке) сегодняшний набор
   тестов не поймает никогда, потому что `test_original_report_is_not_mutated`
   проверяет только скалярное поле `title` **переписанной** карточки, а не
   вложенный список **непереписанной**.
   Нужен тест на неразделяемость вложенных изменяемых полей непереписанной
   карточки (или прямая проверка `is not` между `result.cards[i]` и
   `report.cards[i]` для карточек без правок, или мутация вложенного списка
   результата с проверкой исходника) — это доказательство самого критерия
   приёмки 1, а не стилистика.

## Советы (не блокируют)

1. `apply_overrides` не проверяет типы значений из белого списка:
   `title=None`, `title=123`, `risks="строка вместо списка"` — принимаются
   без ошибки через `card.model_copy(update=updates)` (pydantic не валидирует
   `update=`) и без исключения уезжают в `model_dump_json()` (с предупреждением
   pydantic про несовпадение типа при сериализации, но не с падением). Это
   согласуется с явной границей задачи («падение с 500 на мусорном входе —
   дефект, но решает маршрут И-12») и не блокирует И-7, но исполнителю И-12
   стоит явно приводить типы полей формы к строкам/спискам до сборки
   `overrides`, иначе оператор сможет случайно испортить карточку числом или
   строкой на месте списка без единого сообщения об ошибке.
2. `evidence_removed` ключуется по URL (это выбор плана, не implementer'а):
   если у карточки два разных `Evidence` с одинаковым `url` (два факта с
   одной страницы-источника), удаление одного URL стирает оба. В текущих
   тестовых данных такого случая нет, реальная вероятность невысокая, но
   стоит держать в уме при проектировании формы И-12 (например, ключевать
   удаление по индексу строки, а не по URL, если случай окажется частым).
3. Докстрока модуля (строка 7-8) объясняет глубокую копию как защиту
   «исходного отчёта» в общем, не уточняя, что именно она нужна для
   непереписанных карточек — уточнение сняло бы вопрос ревью с первого
   взгляда, но это стилистика, не функциональный дефект.

## Проверено

- `pytest tests/test_report_overrides.py -v`: 10 passed
- `pytest -q` (полный набор): 898 passed, 1 warning (посторонний
  StarletteDeprecationWarning от `starlette.testclient`, не относится к
  задаче)
- `pytest tests/test_backend_layout.py tests/test_tests_layout.py -q`: 6 passed
- `pytest tests/test_team_docs.py -q`: 35 passed (шапки YAML, инвариант №10)
- `ruff check .`: All checks passed!
- `ruff format --check .`: 165 files already formatted
- `grep -nE "sqlalchemy|app\.db|httpx" backend/app/report_overrides.py`:
  совпадений нет
- `wc -l backend/app/report_overrides.py`: 66 строк (ориентир плана — 80)
- `git diff 74f603ad..37b47d4 --stat`: только
  `backend/app/report_overrides.py`, `tests/test_report_overrides.py` и
  файлы `.planning/evidence/` — вне списка файлов задачи ничего не тронуто
- Ключ `(is_contest, position)`: сверен глазами с
  `pipeline/persistence_write.py:238-244, 298-309` (`_card_row`,
  `enumerate` по каждой секции с нуля) — нумерация совпадает буквально
- Белый список `EDITABLE_FIELDS` сверен с `backend/app/schemas.py:237-278`
  (`SolutionCard`) — все восемь полей существуют в схеме, `confidence`,
  `requirement_checks`, `unsourced_claims`, `evidence` в списке отсутствуют
  и нигде в коде модуля не читаются из `overrides`
- Мутация 1: добавил `"confidence"` в `EDITABLE_FIELDS` — упали
  `test_confidence_override_is_ignored` и `test_all_editable_fields_are_applied`
  (2 failed из 10) — правку откатил
- Мутация 2: заменил `report.model_copy(deep=True)` на `deep=False` —
  ни целевой набор (10 тестов), ни полный `pytest -q` (898 тестов) не упали;
  правку откатил, находка вынесена в блокирующие (см. выше)
- Инварианты №1 (evidence не добавляется наложением — критерий подтверждён
  тестом и грепом), №2 (в `Report`/`SolutionCard` нет поля `validated`/
  пометки «ЧЕРНОВИК» — тронуть их наложение физически не может), №3 (грепом
  секретов в diff нет), №4 (промптов и `render_prompt` модуль не касается),
  №5 (внешних источников нет), №6 (нет `httpx`/`sqlalchemy`/`app.db`), №7
  (тесты не ходят в сеть, весь набор проходит), №8 (см. блокирующую находку
  выше — единственное нарушение), №9 (diff не выходит за список файлов
  задачи, маршрут и форма правок — по границе, оставлены И-11/И-12), №10
  (YAML-шапка handoff исполнителя закавычена, `test_team_docs.py` зелёный)
  — соблюдены, кроме зафиксированного отступления от критерия приёмки 1
  по №8.

## Разбор handoff исполнителя

`handoff.self_audit: pass`, `escalate: none` — комбинация непротиворечива,
блокирующая находка ревью не была видна на этапе самопроверки исполнителя
(что и есть штатная причина существования независимого ревью). Артефакты
существуют (`backend/app/report_overrides.py`,
`tests/test_report_overrides.py`, `.planning/evidence/E3-I7-pytest.txt`,
`.planning/evidence/E3-И7-handoff.yaml`), `checks` в handoff воспроизведены
дословно и подтверждены здесь заново.

---

# Ревью И-7 · цикл 2
Вердикт: **ПРИНЯТЬ** (блокирующих 0, советов 3 — те же, что в цикле 1, не блокируют)

Второй цикл — последний (§4 CONVENTIONS); третьего не будет.

## Проверка закрытия блокирующей находки цикла 1

Исполнитель сдал `e3/i7` @ `03f867c` (родитель `37b47d4`). Заявлено: код
модуля не менялся, добавлен один тест
`test_untouched_card_nested_fields_do_not_leak_into_source`, самопроверка
мутацией `deep=False` ловится этим тестом.

- `git diff 37b47d4..03f867c -- backend/app/report_overrides.py` — пусто,
  подтверждено: модуль правда не тронут.
- `git diff 37b47d4..03f867c --stat` — изменены только
  `tests/test_report_overrides.py` (+18 строк, новый тест) и файлы
  `.planning/evidence/` исполнителя — вне границ задачи ничего не добавлено.
- Новый тест `test_untouched_card_nested_fields_do_not_leak_into_source`
  прочитан целиком: собирает `Report` с двумя карточками, правит только
  карточку 0, проверяет `result.cards[1] is not report.cards[1]` и
  `result.cards[1].risks is not report.cards[1].risks`, затем мутирует
  `result.cards[1].risks` и проверяет, что `report.cards[1].risks` не
  изменился. Это ровно тот тест, который требовался.
- `pytest tests/test_report_overrides.py -v` — 11 passed (было 10 + 1 новый).
- Мутация цикла 1 повторена на этом коде: заменил
  `report.model_copy(deep=True)` на `deep=False` —
  `pytest tests/test_report_overrides.py -v` даёт **1 failed, 10 passed**,
  падает ровно новый тест `test_untouched_card_nested_fields_do_not_leak_into_source`
  (см. трассировку: `assert result.cards[1] is not report.cards[1]` не
  выполняется). Блокирующая находка закрыта именно тем тестом, который был
  нужен — не побочным эффектом. Правка отменена (`git checkout --
  backend/app/report_overrides.py`), дерево снова чистое.
- `pytest -q` (полный набор): **899 passed**, 1 посторонний warning
  (StarletteDeprecationWarning, не от задачи).
- `ruff check .`: All checks passed!; `ruff format --check .`: 165 files
  already formatted.

## Советы

Три совета из цикла 1 (типы значений overrides без валидации — граница
И-12; каскадное удаление evidence при повторяющемся URL — заложено планом;
докстрока могла бы явно объяснять цель `deep=True`) остаются в силе как
советы, не блокируют — они не были предметом блокирующей находки и не
требовали правки в этом цикле.

## Проверено (цикл 2)

- `git diff 37b47d4..03f867c -- backend/app/report_overrides.py`: пусто
- `pytest tests/test_report_overrides.py -v`: 11 passed
- Мутация `deep=True → deep=False`: 1 failed (ровно новый тест), 10 passed —
  находка закрыта тестом по адресу
- `pytest -q`: 899 passed, 1 посторонний warning
- `ruff check .`: All checks passed!
- `ruff format --check .`: 165 files already formatted

## Итог

Блокирующая находка цикла 1 закрыта точным тестом на то поведение, которое
и было предметом претензии. Новых блокирующих находок в цикле 2 нет.
Вердикт: **ПРИНЯТЬ**.
