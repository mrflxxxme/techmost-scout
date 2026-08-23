# Мутации З-2 — прицельный прогон

Все мутации внесены поверх коммита `55b26eb`, прогнаны, затем дерево
восстановлено `git checkout -- <файл>` (РЕШ-135). После каждой мутации
`git status --porcelain` пуст.

## (а) измеритель возвращает зашитую четвёрку

Правка `backend/app/metrics.py:_resolve_relevance_min` — тело заменено на
`return 4`.

```
pytest tests/test_metrics.py -q
2 failed, 10 passed in 0.32s
FAILED tests/test_metrics.py::test_metrics_and_selection_agree_on_the_same_settings_threshold
FAILED tests/test_metrics.py::test_explicit_minimum_overrides_settings
```

Критерий 2 и критерий 3 падают — мутация убита.

## (б) измеритель читает настройки, а `select_top` получает жёсткое число

Правка `backend/app/pipeline/selection.py:select_top` — вместо чтения
`get_settings().relevance_min` подставлено `relevance_min = 4`.

```
pytest tests/test_metrics.py tests/test_stages.py -q
1 failed, 113 passed in 5.22s
FAILED tests/test_metrics.py::test_metrics_and_selection_agree_on_the_same_settings_threshold
  AssertionError: отбор разошёлся с ожиданием при пороге 3
```

Критерий 2 падает — мутация убита.

## (в) явный аргумент игнорируется

Правка `backend/app/metrics.py:_resolve_relevance_min` — тело всегда
возвращает `get_settings().relevance_min`, параметр `explicit` не используется.

```
pytest tests/test_metrics.py -q
2 failed, 10 passed in 0.31s
FAILED tests/test_metrics.py::test_failed_batch_does_not_change_max_and_share
FAILED tests/test_metrics.py::test_explicit_minimum_overrides_settings
```

Критерий 3 падает — мутация убита.

## (г) нейтральные оценки перестают исключаться

Правка `backend/app/metrics.py:model_relevance` — убрана проверка
`score.get(NEUTRAL_KEY)`.

```
pytest tests/test_metrics.py -q
5 failed, 7 passed in 0.34s
FAILED tests/test_metrics.py::test_model_relevance_skips_neutral_scores
FAILED tests/test_metrics.py::test_failed_batch_does_not_change_max_and_share
FAILED tests/test_metrics.py::test_neutral_batch_does_not_raise_maximum_above_model_scores
FAILED tests/test_metrics.py::test_measures_are_none_when_model_scored_nothing
FAILED tests/test_metrics.py::test_max_relevance_matches_calibration_line[model-max-below-neutral-five]
```

Существующие тесты `tests/test_metrics.py` падают — мутация убита.

## Восстановление дерева

После каждой мутации:

```
git checkout -- backend/app/metrics.py   # или selection.py для (б)
git status --porcelain                   # пусто
```

Финальный прогон полного набора после восстановления:

```
pytest -q
700 passed, 1 warning in 37.48s
```
