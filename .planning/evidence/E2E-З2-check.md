# З-2 · Проверка критериев приёмки

## Критерий 1 — копии числа в поставке нет

```
$ grep -rnE "RELEVANCE_MIN\s*=\s*[0-9]" backend/app --include="*.py"
(нет вывода, exit code 1)
```

## Критерий 2 и 3 — измеритель и отбор берут порог из одного источника; явный аргумент сильнее

```
$ pytest tests/test_metrics.py -v
12 passed
  test_metrics_and_selection_agree_on_the_same_settings_threshold — при
  Settings(relevance_min=3) кандидат relevance=3 годен и измерителем, и
  select_top; при Settings(relevance_min=4) не годен обоими путями.
  test_explicit_minimum_overrides_settings — явный minimum=5 считает по
  пятёрке при настройках relevance_min=3.
```

## Критерий 4 — нейтральные оценки по-прежнему не считаются измерением

```
$ git diff main -- tests/test_metrics.py   # (диапазон коммита волны)
```
Правки: два новых теста добавлены; единственная содержательная правка
существующего теста — `test_share_counts_score_exactly_at_threshold` (бывшая
строка 122, читавшая удалённую константу RELEVANCE_MIN напрямую, теперь
подставляет порог через monkeypatch настроек) и `test_failed_batch_does_not_change_max_and_share`,
получивший явный `minimum=4` (без него дефолт стал бы 0 через настройки, а
тест проверяет исторический критерий Т-20, завязанный на число 4, а не на
текущую настройку). Остальные тесты `model_relevance`/нейтральных оценок не
правились.

## Критерий 5 — документ среды называет 3

```
$ grep -n "RELEVANCE_MIN" docs/remote-setup.md
58:RELEVANCE_MIN=3                   # отсечка по предметному соответствию (РЕШ-74, порог снижен РЕШ-162)
```

## Критерий 6 — базовая линия и линт

```
$ .venv/bin/python -m pytest -q
700 passed, 1 warning in 37.68s     (базовая линия волны — 698, прирост — два новых теста)

$ .venv/bin/python -m ruff check backend/app/metrics.py tests/test_metrics.py docs/remote-setup.md
All checks passed!

$ .venv/bin/python -m ruff format --check backend/app/metrics.py tests/test_metrics.py
2 files already formatted
```
(15 предсуществующих замечаний ruff в `docs/marketing/deck/add-faq-slide.py` —
файл не в задаче, замечания были на базовой линии до коммита волны.)

## Эффект на сохранённых данных (data/scout.db, только чтение)

```
$ .venv/bin/python .planning/evidence/E2E-З2-effect-check.py
request_id=1 (Э2-Г): кандидатов в общем отборе=72, порог4=5, порог3=5
request_id=2 (Э2-Д): кандидатов в общем отборе=200, порог4=2, порог3=9
```

Совпадает с постановкой задачи дословно: запрос 2 — 9 вместо 2, запрос 1 —
без изменений. Считает конкурсный класс исключённым (`source_type != "contest"`),
как это делает реальный отбор (`app.pipeline.run_flow`,
`split_contest_candidates`), измерителем `app.metrics.count_at_least_relevance`.
