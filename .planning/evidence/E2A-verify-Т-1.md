# Верификация Т-1

**Вердикт: PASS** (закрыто 5 из 5)

Задача: Т-1 · Отсечка по предметному соответствию (Э2-4 в редакции РЕШ-48).
Рабочая копия: `.claude/worktrees/E2A-T1`, ветка `E2A/T1`, коммит
`d341cfa6343d097b74a1f77dc85cd147e11aa825`. Дифф снят против `session/E2`.
Верификация третья независимая: числа полного набора и линтера сверены с
прогонами оркестратора и ревьюера, расхождений нет.

## Критерии приёмки

| № | Критерий из PLAN.md | Как проверен | Фактический вывод | Статус |
|---|---|---|---|---|
| 1 | `pytest tests/test_stages.py -v` — зелёный, и среди тестов есть три: отсечка режет кандидата с `relevance` ниже порога; кандидат с нейтральной оценкой при том же пороге остаётся; при `relevance_min=0` состав отбора совпадает с прежним поведением | `python -m pytest tests/test_stages.py -v` | `16 passed in 0.33s`. Три обязательных теста присутствуют поимённо: `test_select_top_drops_candidate_below_relevance_min` PASSED, `test_select_top_keeps_neutral_score_at_same_threshold` PASSED, `test_select_top_with_zero_threshold_keeps_previous_behaviour` PASSED. По коду тестов сверено, что первые два стоят на одном и том же пороге `relevance_min=6` — критерий требует именно «при том же пороге» | **взят** |
| 2 | `pytest tests/test_prompts.py -v` — зелёный, включая новую проверку версии `scorer.md` | `python -m pytest tests/test_prompts.py -v` | `30 passed in 0.35s`, в том числе `test_prompt_version_matches_latest_changelog_entry[scorer] PASSED`. Параметр `scorer` добавлен в существующую параметризацию: было `["card_author", "verifier", "intake"]`, стало `["card_author", "verifier", "intake", "scorer"]` | **взят** |
| 3 | `pytest -q` — все тесты проходят (было 199), сеть не требуется | `python -m pytest -q` | `208 passed, 1 warning in 65.22s`, exit 0. Арифметика сходится: 199 + 9 = 208, где 9 = 8 новых тестов в `tests/test_stages.py` + 1 новый параметр `[scorer]` в `tests/test_prompts.py`. Единственное предупреждение — чужое `StarletteDeprecationWarning` из `fastapi/testclient.py` | **взят** |
| 4 | `ruff check . && ruff format --check .` — 0 замечаний | обе команды | `All checks passed!` (exit 0); `113 files already formatted` (exit 0) | **взят** |
| 5 | В выводе прогона стадии скоринга на фикстуре видна строка калибровки с распределением и колонкой «отсеклось бы при 4/5/6/7» | Фактический прогон, а не чтение кода: `score_candidates` на фикстуре из 6 кандидатов со стаб-роутером (без сети; второй батч отказывает → нейтральные оценки), затем `select_top` при `logging.basicConfig(level=logging.INFO)`. Три входа: обычный список при пороге 0, тот же при пороге 6, пустой список | Строка калибровки напечатана во всех трёх случаях, приведена дословно ниже | **взят** |

## Строка калибровки — фактический вывод

Прогон стадии скоринга на фикстуре: `score_candidates(StubRouter(), brief,
6 кандидатов, batch_size=5)`. Первый батч оценён моделью-заглушкой
(`relevance` = 3, 5, 5, 8, 10), второй батч отказал → `K5` получил нейтральные
оценки от кода. Сети нет.

```
=== SCORING STAGE on fixture (6 candidates, batch_size=5, 2nd batch refused) ===
WARNING app.pipeline.stages | скоринг батча не удался (batch refused (stub)), беру нейтральные оценки
SCORED: [('K0', 3, 'model'), ('K1', 5, 'model'), ('K2', 5, 'model'), ('K3', 8, 'model'), ('K4', 10, 'model'), ('K5', 5, 'neutral')]
```

**Случай 1 — обычный список кандидатов, порог 0:**

```
INFO app.pipeline.stages | калибровка relevance: кандидатов 6, min/медиана/max 3/5/10, порог 0, отсечено 0; отсеклось бы при 4: 1, при 5: 1, при 6: 3, при 7: 3; нейтральных оценок 1 (не отсекаются никогда)
PICKED: ['K4', 'K3', 'K1']
```

**Случай 2 — тот же список, ненулевой порог 6:**

```
INFO app.pipeline.stages | калибровка relevance: кандидатов 6, min/медиана/max 3/5/10, порог 6, отсечено 3; отсеклось бы при 4: 1, при 5: 1, при 6: 3, при 7: 3; нейтральных оценок 1 (не отсекаются никогда)
PICKED: ['K4', 'K3', 'K5']
```

**Случай 3 — пустой список кандидатов, порог 6:**

```
INFO app.pipeline.stages | калибровка relevance: кандидатов 0, min/медиана/max нет оценок, порог 6, отсечено 0; отсеклось бы при 4: 0, при 5: 0, при 6: 0, при 7: 0; нейтральных оценок 0 (не отсекаются никогда)
PICKED: []
```

**Ноль отсечённых отличим от «отсечка не сработала» в обоих нулевых случаях.**
В случае 1 рядом с `отсечено 0` стоит непустая колонка `при 4: 1, при 5: 1,
при 6: 3, при 7: 3` — механизм отсечки посчитан и вернул честный ноль для
действующего порога 0, а не промолчал. В случае 3 ноль объяснён самим входом:
`кандидатов 0` и `min/медиана/max нет оценок`.

**Данные для калибровки Т-10 в строке присутствуют полностью:** размер выборки,
распределение min/медиана/max, фактическая отсечка при действующем пороге,
колонка «отсеклось бы» по порогам 4/5/6/7 и отдельный счётчик нейтральных
оценок. Это и требовалось: сохранённых оценок Ф-10 не существует, другого
источника данных для подбора порога нет.

**Наблюдение по нейтральным оценкам (подтверждает п. 2 задачи вживую).** В
случае 2 при пороге 6 кандидат `K5` с нейтральной оценкой `relevance = 5`
остался в отборе (`PICKED: ['K4', 'K3', 'K5']`), тогда как кандидаты `K1` и
`K2` с *той же* оценкой 5, но полученной от модели, отсечены. Отсечка различает
не значение оценки, а её происхождение: отказ батча скоринга не выдаётся за
«все кандидаты нерелевантны». Колонка «отсеклось бы» это тоже учитывает — при
пороге 6 она показывает 3, а не 4, то есть нейтральная оценка в прикидку не
попадает и калибровку Т-10 не искажает.

## Инварианты

Только применимые к Т-1, построчно, со способом проверки из `INVARIANTS.md`.

**№8 — новое поведение без теста, никогда.** `git diff session/E2 --name-only`
даёт `backend/app/config.py`, `backend/app/pipeline/stages.py`,
`tests/test_stages.py`, `tests/test_prompts.py` (плюс два файла промптов) —
правка кода и правка тестов идут вместе. Каждое новое поведение покрыто
отдельным тестом: значение по умолчанию 0, отсечка кандидата ниже порога,
неприкосновенность нейтральных оценок, эквивалентность прежнему поведению при
пороге 0, чтение порога из настроек, печать строки калибровки при нулевой
отсечке, отдельный счёт нейтральных в журнале, печать строки на пустом входе.
**Соблюдён.**

**№7 — тест обращается в сеть, никогда.** Полный набор `pytest -q` →
`208 passed` за 65.22 с. Новые тесты работают на локально сконструированных
`ScoredCandidate` и на `caplog`, внешних вызовов не делают;
`test_select_top_reads_threshold_from_settings` подменяет `get_settings` через
`monkeypatch`. Прогон стадии скоринга верификатором шёл на стаб-роутере. Дифф
не вносит ни одного сетевого вызова. **Соблюдён.**

**№4 — незаполненная подстановка `{{...}}` уходит в модель, никогда.**
`pytest tests/test_prompts.py` → `30 passed`. Дополнительно проверено, что
состав меток не изменился: дифф `tests/test_prompts.py` состоит ровно из одной
изменённой строки (список параметризации теста версий), словарь
`EXPECTED_PLACEHOLDERS` цел и сохраняет прежний состав — `intake`, `planner`,
`card_author`, `verifier`, `summary`, `extractor`, `scorer`, причём у `scorer`
значение `set()`. Поиск `{{` в `prompts/scorer.md` не даёт ни одного
совпадения, что согласуется с пустым набором меток: правка промпта в область
подстановок не заходила. **Соблюдён.**

**№9 — расширение скоупа за границы MVP, никогда.** `git diff session/E2
--name-only` даёт ровно шесть файлов, и все шесть входят в список «Файлы»
карточки Т-1. `learning.py` в диффе отсутствует, слово `verdicts` в диффе не
встречается. Новой стадии пайплайна не заведено: добавлены функции
`log_relevance_calibration`, `_cut_by_relevance`, `_has_neutral_score` внутри
существующего `stages.py`, отсечка реализована фильтром внутри `select_top`;
`orchestrator.py` не изменён. **Соблюдён.**

Остальные инварианты (№1, №2, №3, №5, №6, №10) к Т-1 не применимы. Нарушений в
диффе не видно: секретов дифф не добавляет (поиск `sk-|gigachat|Api-Key` по
диффу даёт единственное совпадение — контекстную строку существовавшего
валидатора `@field_validator("gigachat_scope")`), сетевого кода не добавляет.

## Границы задачи

| Требование границ из PLAN.md | Факт | Итог |
|---|---|---|
| Порог не подбирается по догадке и в код не зашивается | `select_top(scored, *, limit, relevance_min: int \| None = None)`; при `None` значение берётся из настроек: `stages.py:379-380` → `relevance_min = get_settings().relevance_min`. Машинно закреплено тестом `test_select_top_reads_threshold_from_settings` | соблюдено |
| Значение по умолчанию `RELEVANCE_MIN` = 0, диапазон 0–10 | `config.py:118`: `relevance_min: int = Field(default=0, ge=0, le=10, ...)`. В `SettingsConfigDict` нет `env_prefix`, стоит `case_sensitive=False` — переменная окружения читается именно как `RELEVANCE_MIN`. Значение по умолчанию закреплено тестом `test_relevance_min_default_is_zero_cutoff_disabled` через `Settings.model_fields["relevance_min"].default` | соблюдено |
| Правка `prompts/scorer.md` не выходит за блок якорей шкалы `relevance` | Дифф файла затрагивает только строки внутри блока «relevance — попадание в задачу»: добавлен вводный абзац об асимметрии шкалы и промежуточные ступени 1–2, 3–4, 5–6, 7–9 вместо прежних якорей «0» и «5». Блоки других шкал, состав ответа, формат JSON и пример неправильного ответа не тронуты. Отдельной строкой изменена метка версии | соблюдено |
| Версия `scorer.md` 1.1 → 1.2, запись в `prompts/CHANGELOG.md` | В конце файла `<!-- версия 1.2 -->`; в CHANGELOG запись «## 1.2 — ужесточён якорь шкалы relevance, 14 августа 2026» с указанием, что структура ответа и состав обязательных полей не изменились. Связка «версия файла = версия последней записи» проверяется машинно | соблюдено |
| Стадии-фильтра не заводить (РЕШ-48) | Новой стадии нет; отсечка — фильтр внутри `select_top` перед сортировкой | соблюдено |

## Наблюдения, не влияющие на вердикт

1. **`RELEVANCE_MIN` отсутствует в `.env.example`.** Файл перечисляет
   сопоставимые настройки — `TARGET_CARDS=6` (стр. 97),
   `COST_LIMIT_RUB_PER_REQUEST=20` (стр. 99), `DEMO_MODE_DEFAULT=false`
   (стр. 108), — но строки `RELEVANCE_MIN` в нём нет. При этом `.env.example`
   не входит в список «Файлы» карточки Т-1, а `.env` рабочей копии числится за
   Т-10. Отсутствие записи лежит внутри объявленных границ Т-1 и критерием
   приёмки не является.
2. **Прямой `httpx` в `backend/app/cli.py`.** Строки `cli.py:15` и `cli.py:92`
   существовали до Т-1: `cli.py` в диффе отсутствует вовсе. Третье совпадение,
   `config.py:154`, — текст комментария, а не вызов. Инвариант №6 к Т-1 не
   применим.

## Чем подтверждено

```
$ python -m pytest tests/test_stages.py -v
rootdir: ...\.claude\worktrees\E2A-T1
collected 16 items

tests/test_stages.py::test_deduplicate_merges_same_doi PASSED
tests/test_stages.py::test_deduplicate_prefers_showcase_over_open_source PASSED
tests/test_stages.py::test_select_top_limits_single_provider_dominance PASSED
tests/test_stages.py::test_relevance_min_default_is_zero_cutoff_disabled PASSED
tests/test_stages.py::test_select_top_drops_candidate_below_relevance_min PASSED
tests/test_stages.py::test_select_top_keeps_neutral_score_at_same_threshold PASSED
tests/test_stages.py::test_select_top_with_zero_threshold_keeps_previous_behaviour PASSED
tests/test_stages.py::test_select_top_reads_threshold_from_settings PASSED
tests/test_stages.py::test_select_top_logs_calibration_even_when_nothing_is_cut PASSED
tests/test_stages.py::test_calibration_line_counts_neutral_scores_separately PASSED
tests/test_stages.py::test_calibration_line_printed_on_empty_input PASSED
tests/test_stages.py::test_group_for_cards_merges_same_organisation PASSED
tests/test_stages.py::test_group_for_cards_respects_target PASSED
tests/test_stages.py::test_select_publishable_cards_drops_empty_evidence_and_placeholder PASSED
tests/test_stages.py::test_select_publishable_cards_caps_at_seven PASSED
tests/test_stages.py::test_select_publishable_cards_keeps_all_when_under_limit PASSED

============================== 16 passed in 0.33s ==============================

$ python -m pytest tests/test_prompts.py -v
collected 30 items
...
tests/test_prompts.py::test_prompt_version_matches_latest_changelog_entry[card_author] PASSED
tests/test_prompts.py::test_prompt_version_matches_latest_changelog_entry[verifier] PASSED
tests/test_prompts.py::test_prompt_version_matches_latest_changelog_entry[intake] PASSED
tests/test_prompts.py::test_prompt_version_matches_latest_changelog_entry[scorer] PASSED
...
============================== 30 passed in 0.35s ==============================

$ python -m pytest -q
208 passed, 1 warning in 65.22s (0:01:05)          [exit 0]

$ ruff check .
All checks passed!                                  [exit 0]

$ ruff format --check .
113 files already formatted                         [exit 0]

$ git status --porcelain
(пусто — рабочая копия чиста, ни один файл верификатором не создан и не изменён)

$ git rev-parse HEAD
d341cfa6343d097b74a1f77dc85cd147e11aa825

$ git diff session/E2 --stat
 backend/app/config.py          |   6 ++
 backend/app/pipeline/stages.py |  75 +++++++++++++++++++++--
 prompts/CHANGELOG.md           |  14 +++++
 prompts/scorer.md              |  17 +++++-
 tests/test_prompts.py          |   2 +-
 tests/test_stages.py           | 133 ++++++++++++++++++++++++++++++++++++++++-
 6 files changed, 236 insertions(+), 11 deletions(-)
```

Числа полного набора (`208 passed`), `ruff check` и `ruff format --check`
совпали с независимыми прогонами оркестратора и ревьюера — расхождений нет.

## Отложено

`DLG` нет. Все пять критериев приёмки прогоняются локально; ключей, платных
вызовов и регистраций ни один из них не требует.

## Причина вердикта

Закрыты все пять критериев, включая критерий 5, проверенный фактическим
прогоном стадии на фикстуре, а не чтением кода. Применимые инварианты №4, №7,
№8, №9 подтверждены способами из `INVARIANTS.md`. Границы задачи соблюдены.
Оснований для FAIL нет — **PASS**.

*Проверка оркестратора: рабочая копия `E2A-T1` после верификации чиста по
`git status`. Запрет «кто проверяет — тот не правит» соблюдён.*
