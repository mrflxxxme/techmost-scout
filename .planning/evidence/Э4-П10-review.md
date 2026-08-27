# Ревью Э4-П10 · цикл 1
Вердикт: ПРИНЯТЬ

## Проверка мутаций (поставлены лично, не по описанию исполнителя)

### П-11 — `escape()` у строки оговорок в `edit_summary`
Мутация: `caveats_suffix = f" Оговорки: {'; '.join(caveats_list)}." if caveats_list else " Оговорок нет."`
(убран `escape()`).

```
FAILED tests/test_web_review_coverage_holes.py::test_summary_post_экранирует_html_в_строке_оговорок
AssertionError: assert '<img src=x ...or=alert(1)>' not in '...Оговорки: <img src=x onerror=alert(1)>.</div>'
1 failed, 1 passed
```
После отката — `git diff -- backend/` пуст.

Обратная проверка (п.4 задания реью): мутация «убрать `escape()` у **рекомендации**»
(`recommendation_text` без `escape`) новый тест П-11 не трогает (он проходит),
а падает старый `test_summary_и_экран_ревью_честны_с_экспертом`:
```
FAILED tests/test_web_review.py::test_summary_и_экран_ревью_честны_с_экспертом
AssertionError: assert '<script>' not in '...Рекомендация: <script>x</script>. Оговорок нет.</div>'
1 failed, 17 passed   (файл tests/test_web_review_coverage_holes.py — оба теста зелёные)
```
Тест П-11 ловит именно строку оговорок и не пересекается с тестом на рекомендацию.
После отката — `git diff -- backend/` пуст.

### П-12 — `_normalize_caveats` → `list(caveats)`
Полная мутация (без `strip` и без отсева пустых строк):
```
FAILED test_normalize_caveats_убирает_пробелы_по_краям
FAILED test_normalize_caveats_отсеивает_пустые_строки
FAILED test_normalize_caveats_отличает_пробельную_правку_от_настоящей
3 failed, 1 passed
```
После отката — `git diff -- backend/` пуст.

Раздельные частичные мутации (требование п.3 задания — обе части нормализации
обязаны краснеть по отдельности):

* Мутация «убрать только отсев пустых» (`[item.strip() for item in caveats]`):
  ```
  FAILED test_normalize_caveats_отсеивает_пустые_строки
  FAILED test_normalize_caveats_отличает_пробельную_правку_от_настоящей
  2 failed, 2 passed
  ```
* Мутация «убрать только `strip`» (`[item for item in caveats if item.strip()]`):
  ```
  FAILED test_normalize_caveats_убирает_пробелы_по_краям
  FAILED test_normalize_caveats_отличает_пробельную_правку_от_настоящей
  2 failed, 2 passed
  ```
Обе части нормализации покрыты раздельно, не только совместно. После каждого
отката — `git diff -- backend/` пуст.

### П-13 — `_annotate_client_view` нумерует клиентскими номерами и скрытые карточки
Мутация: инкремент `client_num += 1` перенесён до вызова `_hidden_reason` (то
есть до проверки «карточка скрыта»).
```
FAILED tests/test_web_review_coverage_holes.py::test_скрытая_карточка_перед_видимой_не_сдвигает_клиентский_номер
AssertionError: 'Вариант 2 «Одобренный» — уедет клиенту как вариант №1.' not in response.text
1 failed, 17 passed   (17 — все тесты старого tests/test_web_review.py остались зелёными)
```
То, что старый набор (17 тестов review) пережил эту мутацию целиком, подтверждает
диагноз аудита: прежняя фикстура была слепа именно к этой мутации. После отката —
`git diff -- backend/` пуст.

**Проверка фикстуры лично.** Прочитан код теста
(`tests/test_web_review_coverage_holes.py:172-181`):
```python
cards = [_make_card(title="Отклонённый"), _make_card(title="Одобренный")]
...
rejected_id, _visible_id = card_ids
```
Карточка на позиции 0 (`«Отклонённый»`) — та, что отклоняется (`rejected_id`),
карточка на позиции 1 (`«Одобренный»`) видимая. Скрытая карточка действительно
идёт **первой**, видимая — второй. Это ровно тот порядок, которого не хватало
старой фикстуре (там `main_ids[1]` — вторая карточка — отклонялась, то есть
скрытая шла после видимой). Мутация проявляется только в новом порядке, что
и подтверждено прогоном выше. Не слепа.

## Границы задачи
```
$ git diff d3bf88b..e4/p10-coverage-holes -- backend/ | wc -l
0
$ git diff d3bf88b..e4/p10-coverage-holes --stat
 .../evidence/Э4-П10-handoff.yaml                    |  48 ++++++
 tests/test_summary_caveats_normalization.py        |  44 +++++
 tests/test_web_review_coverage_holes.py            | 188 +++++++++++++++++++++
 3 files changed, 280 insertions(+)
$ git diff d3bf88b..e4/p10-coverage-holes -- tests/test_web_review.py | wc -l
0
```
Кода продукта (`backend/`) диф не касается. Изменены ровно два новых тестовых
файла плюс handoff. `tests/test_web_review.py` не редактировался — подтверждено
и заявленной причиной (498/500 строк на базовом коммите — проверено
`git show d3bf88b:tests/test_web_review.py | wc -l` → 498).

## Стражи
```
$ pytest -q tests/test_tests_layout.py -v
tests/test_tests_layout.py ... [100%]
3 passed
```
Список исключений роста тестовых файлов не правился (в диффе `test_tests_layout.py`
отсутствует). Новые файлы: `tests/test_web_review_coverage_holes.py` — 188 строк,
`tests/test_summary_caveats_normalization.py` — 44 строки, оба далеко под пределом 500.

## Инварианты (все десять)
1. Факт без ссылки — задача не трогает карточки/evidence, не применимо; диф
   не создаёт новых фактических утверждений.
2. Автоотправка клиенту / снятие «ЧЕРНОВИК» — не затронуто, задача только тесты.
3. Секреты: `grep -rE "sk-|gigachat|Api-Key" backend/ tests/` — совпадения
   только в существующем коде (авторизация YandexGPT/патентного API) и в уже
   существующих тестах с фиктивными ключами (`sk-fake-leak-check-*`, `sk-test`),
   ничего нового в диффе не добавляет секретов. `.env` в `.gitignore`
   (`.env`, `.env.*`). Значения секретов в этом отчёте не приводятся.
4. `render_prompt` / `EXPECTED_PLACEHOLDERS` — задача промптов не касается;
   `pytest tests/test_prompts.py` входит в общий зелёный прогон.
5. Отказ адаптера источника — не затронуто, задача не про поиск.
6. `grep -rn "httpx" backend/app` — диф не меняет расположение httpx-вызовов,
   не проверял отдельно, так как `backend/` в этом диффе не тронут вовсе.
7. Сеть в тестах: новые тесты используют `TestClient` (in-process ASGI) и
   прямые вызовы чистой функции `_normalize_caveats` — ни httpx, ни sockets,
   ни requests не используются (`grep -n "httpx|requests\.|urlopen|socket"` —
   ничего). `pytest -q` — 1164 passed без сети.
8. Изменения в `backend/` есть? Нет (0 строк) — значит требование «есть
   изменения в backend — есть изменения в tests» не применяется; в данном
   случае наоборот, весь диф — только тесты без кода, что и есть цель задачи
   (охрана существующего поведения).
9. Диф сверен с `PLAN.md` (раздел Э4-П10, критерии 1-3) — задачи выполнены
   ровно в заявленных границах, ничего лишнего.
10. YAML: `pytest tests/test_team_docs.py` — 35 passed; `Э4-П10-handoff.yaml`
    парсится `yaml.safe_load` без ошибок, `self_audit: pass` +
    `escalate: none` — непротиворечивая комбинация.

## Грабли §0.1 SPEC.md
Не применимы: задача не трогает промпты, LLM-вызовы, YAML-конфигурацию задач,
`extract_json_block`, адрес GigaChat — диф состоит из двух тестовых файлов на
уже существующий код.

## Handoff исполнителя — проверка
Артефакты существуют (`tests/test_summary_caveats_normalization.py`,
`tests/test_web_review_coverage_holes.py`). `checks` в handoff воспроизведены
лично и совпадают:
* `pytest -q: 1164 passed` — воспроизведено (67.58s).
* `ruff check .: All checks passed!` — воспроизведено.
* `ruff format --check .: 200 files already formatted` (в handoff — 165, у меня
  200; расхождение из-за разных наборов установленных extras/окружений между
  сессией исполнителя и моей копией — не влияет на результат, обе команды
  сообщают «всё отформатировано», расхождений форматирования нет).
* `git diff --stat -- backend/: пусто` — воспроизведено.
`self_audit: pass`, `escalate: none` — непротиворечиво.

## Проверено
- `pytest -q`: 1164 passed, 1 warning (StarletteDeprecationWarning, не по теме диффа)
- `ruff check .`: All checks passed!
- `ruff format --check .`: 200 files already formatted
- `pytest tests/test_tests_layout.py -v`: 3 passed
- `pytest tests/test_team_docs.py -v`: 35 passed
- `git diff d3bf88b..e4/p10-coverage-holes -- backend/`: пусто
- Все три мутации (П-11, П-12 полная + 2 частичные, П-13) поставлены лично и
  падают именно на новых тестах; после каждой — откат и пустой `git diff -- backend/`
- Обратная проверка П-11 (мутация рекомендации не задета новым тестом,
  задета старым) — воспроизведена
- Фикстура П-13 прочитана лично: скрытая карточка идёт первой, видимая второй —
  порядок соответствует требованию задачи
- Инварианты 1-10: нарушений нет

## Советы (не блокируют)
1. В `tests/test_web_review_coverage_holes.py` расхождение числа файлов ruff
   между окружениями (165 в handoff, 200 у меня) стоило бы упомянуть в заметке —
   не критично, но при желании можно указать версию/набор extras для
   воспроизводимости отчёта исполнителя.
