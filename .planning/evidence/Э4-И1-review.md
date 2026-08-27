# Ревью Э4-И1 · цикл 1
Вердикт: ПРИНЯТЬ (блокирующих 0, советов 1)

## Разбор

Проверены все файлы задачи: `backend/app/verdicts.py` (новый), `backend/app/config.py`
(две настройки), `backend/app/export/markdown.py`, `tests/test_verdicts.py`,
`tests/test_export_signature.py`, `tests/test_config.py`. Diff `fb2aa20e..e4/i1` не
выходит за список файлов задачи (плюс два файла evidence самого исполнителя);
`.env.example` не тронут — верно, файл под deny агента, эскалация оформлена в
handoff.

Тай-брейк действующего вердикта (`(row.created_at, row.id) >= (...)`), фильтр
reject в `_visible_section` и `signature_line` проверены прицельной мутацией:
убрал тай-брейк по `id` — падает `test_при_равном_времени_побеждает_больший_id`;
подменил `VerdictDecision.REJECT` на `APPROVE` в условии фильтра — падают
`test_visible_report_убирает_reject_из_обеих_секций_и_сохраняет_порядок` и
`test_visible_report_не_добивает_выдачу`. Обе мутации убиты прицельным набором,
до полного прогона дело не дошло.

Ключ `CardKey = (is_contest, position)` сверен с `report_overrides.OverrideKey`
и с индексацией `_apply_to_card` (`enumerate(result.cards)`/`enumerate(result.
contest_cards)`, `False`/`True` для секций) — соглашение то же самое, рассинхронизации
индексации нет.

`visible_report` делает `report.model_copy(deep=True)` в начале и дальше работает
только с копией — по структуре идентично уже проверенному прецеденту в
`report_overrides.apply_overrides` (тест `test_untouched_card_nested_fields_do_not_
leak_into_source`, найдено ревью цикла 1 И-7). Функционально мутации входного
`report` нет — но выделенного теста на немутируемость (по образцу
`test_report_overrides.py::test_original_report_is_not_mutated`) в
`test_verdicts.py` нет. Не блокирую: контракт задачи (критерий 1 PLAN.md) такой
тест не требует явно, и реализация зеркалит безопасный паттерн из уже
провалидированного модуля (deep copy на входе, дальше только над копией) — но
это совет, а не находка, потому что в реальном коде мутации не происходит,
только тест на этот конкретный сценарий регрессии отсутствует.

`REVIEWED_FOOTER = "Подготовлено системой ТехМост Скаут, проверено: {name}, {date}"`
сверен посимвольно с РЕШ-197 (`.planning/DECISIONS.md:3709-3715`) — совпадает
дословно, ёлочек нет. `render_markdown` печатает подпись только при
`validated and reviewer` — пустое имя оставляет пометку ЧЕРНОВИК (инвариант №2,
тест есть). `developer_line` не хвостует «— не найдено» (casefold, strip),
пустой `developer_org` даёт честное «не найдено» — обе ветки покрыты тестами.
Старая логика в `_render_card` удалена полностью, дублирования правды нет.
Прежние 33 теста `test_export_markdown.py` не тронуты (`git diff --stat` пуст)
и зелёные без правок.

`config.py`: `reviewer_name` default `""`, `demo_duration_seconds` default
`150.0, gt=0` — обе настройки читаются из окружения (case_sensitive=False уже
стоит на уровне модели), нигде не печатаются, REVIEWER_NAME — не секрет
(инвариант №3, имя человека).

Литерал `"reject"` вне `schemas.py:70` не найден — сравнения идут через
`schemas.Verdict.REJECT` (`StrEnum`, сравнение со строкой ORM-поля работает
штатно).

Известное красное — подтверждено ровно по описанию: один тест
`test_config.py::test_каждая_переменная_settings_объявлена_в_примере`, ровно по
двум переменным `DEMO_DURATION_SECONDS` и `REVIEWER_NAME`. Это ожидаемая
эскалация исполнителя (`.env.example` под deny), не находка ревью.

## Блокирующих находок нет

## Советы (не блокируют)

1. `visible_report` не имеет отдельного теста на немутируемость входного
   `report` (по образцу `test_report_overrides.py::test_original_report_is_not_
   mutated` / `test_untouched_card_nested_fields_do_not_leak_into_source`).
   Код корректен — структура идентична уже проверенному паттерну
   `report_overrides.apply_overrides` (deep copy сразу на входе, дальше работа
   только с копией), но регрессия в этом месте не будет поймана тестом при
   будущем рефакторинге. Стоит добавить в одну из следующих задач волны 0/1,
   пока файл `verdicts.py` ещё правится (например, вместе с Э4-И2).
2. `reviewer_name` объявлен с явным `validation_alias="REVIEWER_NAME"`, а
   `demo_duration_seconds` — без алиаса (полагается на автоматическое
   сопоставление по имени поля). Обе формы уже сосуществуют в `config.py` до
   этой правки, функционально идентичны — чисто стилевая непоследовательность
   внутри самой задачи.

## Проверено

- `pytest tests/test_verdicts.py tests/test_export_signature.py tests/test_config.py -v`:
  30 passed, 1 failed (`test_каждая_переменная_settings_объявлена_в_примере`, ожидаемо)
- `pytest -q` (полный набор): 988 passed, 1 failed — тот же ожидаемый тест
- `ruff check .`: All checks passed!
- `ruff format --check .`: 177 files already formatted
- `grep -rE "sk-|gigachat|Api-Key" backend/ tests/`: только легитимные (заголовки
  авторизации YandexGPT/поиска, фиктивные ключи в тестах утечки, комментарии) —
  ничего нового и ничего боевого
- `grep -rln "httpx" backend/app --include='*.py' | grep -vE "(llm/|search/|cli_doctor\.py$|pipeline/pipeline_context\.py$|pipeline/pipeline_search\.py$)"`: пусто
- `grep -rn '"reject"' backend/app`: только `schemas.py:70` (член перечисления)
- `rg -n "«ТехМост Скаут»" backend/app/export/markdown.py`: совпадений нет (критерий 4)
- `git diff fb2aa20e..e4/i1 --stat -- tests/test_export_markdown.py`: пусто (файл не тронут)
- `pytest tests/test_export_markdown.py -q`: 33 passed (без правок)
- `pytest tests/test_team_docs.py -q`: 35 passed (инвариант №10)
- Мутация 1 (убран тай-брейк по `id`): падает `test_при_равном_времени_побеждает_больший_id`
- Мутация 2 (REJECT→APPROVE в фильтре): падают `test_visible_report_убирает_reject_из_обеих_секций_и_сохраняет_порядок`, `test_visible_report_не_добивает_выдачу`
- Инварианты 1–10: нарушений не найдено (№2, №6, №7, №8 — ключевые для задачи,
  проверены отдельно; №1/№4/№5/№9 — задача их не затрагивает, границы не
  расширены; №3 — REVIEWER_NAME не секрет, значения не печатаются; №10 — YAML
  handoff валиден `yaml.safe_load`)
- Handoff исполнителя (`Э4-И1-handoff.yaml`): артефакты существуют, `checks`
  содержат фактический вывод, `self_audit: pass` + `escalate` (известный
  красный тест, не пустая эскалация) — комбинация непротиворечива
