# Ревью И-1 · цикл 1
Вердикт: ПРИНЯТЬ (блокирующих 0, советов 1)

## Проверено по критериям приёмки
1. `pytest tests/test_request_status.py -v` — 12 passed (значения перечисления
   равны семи литералам SPEC; у каждого члена — параметризованная проверка
   непустой русской метки; тест на подкласс `str`; тест на отсутствие лишних
   ключей в `STATUS_LABELS`; тест на `status_label`, включая незнакомое
   значение колонки).
2. `grep -rnE '(status=|status = )"(intake|briefing|searching|drafted|validated|exported|failed)"' backend/app --include='*.py'`
   — единственное совпадение `backend/app/cli_run.py:143` (граница задачи И-4,
   файл не в списке И-1 и не тронут).
3. `grep -rn "{{ row.status }}" backend/app/web/templates` — совпадений нет.
4. `pytest -q` целиком — 848 passed, 0 failed (база волны Э2-З — 836, плюс
   12 новых тестов задачи).
5. `ruff check .` — All checks passed; `ruff format --check .` — 152 files
   already formatted.

## Мутационная проверка (прогнана самостоятельно, не со слов handoff)
Убрана запись `RequestStatus.EXPORTED: "Экспортировано"` из `STATUS_LABELS` в
`backend/app/models.py` → `pytest tests/test_request_status.py -v` даёт
`2 failed` (`test_every_status_has_non_empty_russian_label[exported]` и
`test_status_labels_has_no_extra_keys`). Правка возвращена
(`git checkout -- backend/app/models.py`), рабочее дерево чистое.

## Разбор diff
- `backend/app/models.py`: `RequestStatus(StrEnum)` с семью значениями SPEC в
  верном порядке прохождения статусов; `STATUS_LABELS` — словарь рядом,
  докстрока по-русски объясняет, зачем `StrEnum` (сравнение со строкой колонки
  без явного приведения типов); `Request.status_label` читает словарь через
  `.get(self.status, self.status)` — на незнакомом значении не падает, тест
  есть (`test_request_status_label_falls_back_to_raw_value_for_unknown_status`).
- `backend/app/pipeline/persistence.py`: сравнение `RequestRow.status !=
  "failed"` заменено на `RequestRow.status != RequestStatus.FAILED`,
  докстрока рядом обновлена тем же литералом — ровно то, что просил критерий 2.
- `backend/app/pipeline/run_flow.py`: оба литерала статуса (`"failed"`,
  `"drafted"`) заменены на члены перечисления, поведение не меняется
  (`StrEnum` сравнивается и пишется как строка).
- `backend/app/web/routes.py`: правка ограничена одной строкой создания
  запроса (`status=RequestStatus.INTAKE`) и докстрокой рядом — как и
  предписывала граница задачи (инвариант №9). Остальной файл (стабы будущих
  маршрутов) не тронут.
- Шаблоны `index.html`/`history.html`: печать переведена на
  `{{ row.status_label }}`, а условие подсветки badge (`{% if row.status ==
  'validated' %} ok{% elif row.status == 'failed' %} low{% endif %}`)
  **осталось на сыром `row.status`** — сравнение со строковым литералом
  работает и для `StrEnum`-значения из БД (это обычная строка колонки), и
  подсветка не сломана переходом на метки. То, о чём просили присмотреться
  особо в задании ревью, подтверждено чтением: сравнение — по коду статуса,
  печать — по метке, раздельно.
- `tests/test_request_status.py`: новый файл, 12 тестов, ни один не обращается
  к БД или сети; докстрока и тексты — по-русски.

## Инварианты (все десять)
1. Факт без ссылки — не применимо к задаче (нет карточек, нет `evidence`).
2. Путь автоотправки — не применимо, задача не касается вердиктов/отчёта.
3. Секреты — `grep -rE "sk-|gigachat|Api-Key" backend/ tests/` даёт только
   существующие некритичные совпадения (докстроки про заголовок `Api-Key`
   YandexGPT, тестовые фиктивные строки `sk-fake-...`/`sk-test`), ни одна не
   добавлена и не изменена этим diff'ом; `.env` в `.gitignore` — да.
4. `render_prompt`/`{{...}}` — не применимо, промпты не тронуты;
   `pytest tests/test_prompts.py` — 51 passed (не задет diff'ом, прогнан для
   полноты).
5. Отказ источника — не применимо, адаптеров в diff нет.
6. `grep -rln "httpx" backend/app --include='*.py'` за вычетом легитимной
   карты — пусто; diff их не касается.
7. `pytest` целиком проходит без правок сети — 848 passed.
8. Новое поведение (перечисление, метки, свойство) — с тестом
   (`tests/test_request_status.py`, 12 тестов). Есть изменения в `backend/`
   (models.py, run_flow.py, persistence.py, routes.py, шаблоны) — есть
   изменения в `tests/`.
9. Diff сверен со списком файлов задачи И-1 построчно
   (`git diff ba86bf2f e77eaf8 --stat -- backend/ tests/`) — совпадает
   один в один, без лишних файлов; `routes.py` тронут ровно одной строкой
   создания запроса и докстрокой, как требовала граница.
10. YAML: `pytest tests/test_team_docs.py` — 35 passed (diff YAML-шапок не
    касается, прогнан для полноты).

## Грабли §0.1 SPEC.md
Незаполненных `{{...}}`, эха входных данных модели, скупого `max_tokens`,
адреса GigaChat, разбора пустого `content` — в diff нет ни одной точки
соприкосновения (задача не касается LLM/промптов/парсинга ответов).

## Блокирующие
Нет.

## Советы (не блокируют)
1. `persistence.py`: докстрока к `load_history_pool` после замены литерала на
   `RequestStatus.FAILED` стала на пару символов длиннее строки (`status !=
   RequestStatus.FAILED` в тексте докстроки, не в коде) — читается нормально,
   но если бы дробили строку, вышло бы аккуратнее. Чисто стилистически, ruff
   формат не жалуется.

## Проверено (сводка команд)
- `pytest tests/test_request_status.py -v`: 12 passed
- `pytest -q`: 848 passed, 0 failed
- `ruff check .`: All checks passed
- `ruff format --check .`: 152 files already formatted
- `grep -rnE '(status=|status = )"(intake|...|failed)"' backend/app`: только `cli_run.py:143`
- `grep -rn "{{ row.status }}" backend/app/web/templates`: пусто
- `grep -rln "httpx" backend/app --include='*.py'` (за вычетом легитимной карты): пусто
- `grep -rE "sk-|gigachat|Api-Key" backend/ tests/`: только предсуществующие некритичные совпадения, diff'ом не добавлены
- Мутация STATUS_LABELS (убрана метка `exported`): `2 failed` — тест ловит, правка возвращена
- `pytest tests/test_prompts.py`: 51 passed; `pytest tests/test_team_docs.py`: 35 passed (не задеты diff'ом, для полноты)
