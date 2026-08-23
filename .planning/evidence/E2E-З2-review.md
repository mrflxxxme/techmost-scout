# Ревью З-2 · цикл 1
Вердикт: ПРИНЯТЬ

## Проверено по существу

**а) Единственный источник числа.**
`grep -rnE "RELEVANCE_MIN\s*=\s*[0-9]" backend/app --include="*.py"` — ноль
совпадений. `_resolve_relevance_min` в `metrics.py` при `explicit is None`
делает отложенный импорт `from app.pipeline.stages import get_settings` и
возвращает `get_settings().relevance_min` — дословно тот же приём, что
`select_top` в `selection.py:40-49` (тот же отложенный импорт из `stages`,
та же причина — разрыв цикла `stages` ⇄ модуль). `config.py` не тронут,
`relevance_min` в нём по-прежнему `default=0`.

**б) Тест против расхождения ловит расхождение (главная проверка задачи).**
Поставил обе указанные в карточке мутации сам, прицельным прогоном
`tests/test_metrics.py tests/test_stages.py`:
- мутация (а) — в `metrics.py` `_resolve_relevance_min` при `explicit is None`
  возвращает захардкоженную `4` вместо чтения настроек:
  `test_metrics_and_selection_agree_on_the_same_settings_threshold` падает
  (`AssertionError: измеритель разошёлся с ожиданием при пороге 3`).
- мутация (б) — в `selection.py` `select_top` при `relevance_min is None`
  берёт захардкоженную `4` вместо `get_settings().relevance_min` (измеритель
  при этом остаётся на настройках): тот же тест падает
  (`AssertionError: отбор разошёлся с ожиданием при пороге 3`).
Оба раза 1 failed из 114 (остальные проходят), дерево после каждой мутации
восстановлено `git checkout --`, `git status --porcelain` пуст. Тест
действительно ловит расхождение, а не совпадение по случайности.

**в) Умолчание конфига не тронуто.** `backend/app/config.py` отсутствует в
diff обоих коммитов (`git diff 87575fe HEAD --stat`), `relevance_min` —
`default=0`. `tests/test_stages.py` и `tests/test_contest_gate_and_order.py`
не входят в diff.

**г) Правка чужого теста — вынужденная, не ослабляющая.**
`test_share_counts_score_exactly_at_threshold` (бывшая строка 122): раньше
читала удалённую константу `RELEVANCE_MIN` напрямую, теперь подставляет порог
через `monkeypatch.setattr(stages, "get_settings", …)`. Проверяемый факт (порог
нестрогий, значение ровно на границе — годно) и защита от подмены `>=` на `>`
сохранены дословно.
`test_failed_batch_does_not_change_max_and_share`: получил явный `minimum=4`
в трёх местах. Без второй копии константы умолчание функции стало бы
settings-based (в тестовом окружении — 0), и исторический критерий Т-20
(число 4 из РЕШ-86) перестал бы проверяться тем же числом — тест либо считал
бы по 0 и терял смысл, либо стал бы недетерминированным относительно
`Settings`. Проверяемые значения (max=9, share=0.3, count=3) не изменились —
покрытие не выхолощено, изменение вынужденное.

**д) Числа эффекта.** Пересчитал независимым запросом к `data/scout.db`
(`?mode=ro`, `source_type != 'contest'`) без использования кода исполнителя —
своей копией `model_relevance`:
```
request_id=1 (Э2-Г): total=72, model_scored=72, count(>=3)=5, count(>=4)=5
request_id=2 (Э2-Д): total=200, model_scored=200, count(>=3)=9, count(>=4)=2
```
Совпадает дословно с заявленным в коммите и `E2E-З2-effect-check.py`/`.txt`.

**е) Документ среды.** `docs/remote-setup.md:58` — `RELEVANCE_MIN=3` со
ссылкой на РЕШ-162; про код документ не врёт (значение соответствует
изменённому поведению). Про среду: в текущем окружении ревью фактически стоит
`RELEVANCE_MIN=4` — это ожидаемо и зафиксировано в постановке задачи как
работа оркестратора для воротного прогона волны Э2-Е, не находка к
исполнителю.

**ж) Полный набор и линт.**
```
$ .venv/bin/python -m pytest -q
700 passed, 1 warning in 37.04s
```
```
$ ruff check backend/app/metrics.py tests/test_metrics.py docs/remote-setup.md
All checks passed!
$ ruff format --check backend/app/metrics.py tests/test_metrics.py
2 files already formatted
```
Полный `ruff check .` даёт 15 замечаний — все в `docs/marketing/deck/add-faq-slide.py`,
файл не в списке задачи, вне линтуемой зоны по РЕШ-165 (не работа
исполнителя, подтверждено — замечания там же, где были на базовой линии).

**з) Мутации и коммит.** Обе воспроизведённые мною мутации (а, б) убиты тестом
исполнителя. `git show 55b26eb --stat` не содержит `selection.py` — временная
правка мутации (б) в коммит не попала, как заявлено в handoff.

**и) Инвариант №3.** Секреты не приводились. При проверке документа среды в
окружении встретилась переменная `RELEVANCE_MIN=4` — это открытое числовое
значение конфигурации, а не секрет; значения ключей (`GIGACHAT_*`, `sk-...` и
т. п.) в отчёте не упоминаются и не проверялись на печать.

## Дополнительно проверено (не в списке карточки, но по роли)
- `pytest tests/test_team_docs.py tests/test_prompts.py`: 79 passed — YAML во
  frontmatter корректен, подстановки промптов не пострадали (диф их не
  касается).
- `grep -rn "httpx" backend/app` вне `llm/`/`search/`: есть совпадения в
  `cli.py`, `pipeline/pipeline_search.py`, `pipeline/pipeline_context.py` —
  все три файла не входят в diff этой задачи, предсуществующее состояние,
  не находка к З-2.

## Блокирующие
Нет.

## Советы (не блокируют)
1. Исполнитель назвал свой файл доказательств `.planning/evidence/E2E-З2-check.md` —
   это имя зарезервировано за отчётом проверяющего (ревьюера/верификатора) по
   конвенции волны. Путаницы в этот раз не случилось только потому, что мне
   заранее указали занять имя `E2E-З2-review.md`, но на будущее исполнителям
   стоит называть свои доказательства иначе (`…-evidence.md`, `…-proof.md`),
   не `…-check.md`.
2. `docs/remote-setup.md:58` можно было явно упомянуть, что боевое значение
   переменной в среде выставляет оркестратор перед воротным прогоном (сейчас
   это знание живёт только в PLAN.md и в разговоре волны) — но это уже
   территория оркестратора, не блокирует эту задачу.

## Проверено (сводка команд)
- `pytest -q`: 700 passed, 1 warning
- `ruff check backend/app/metrics.py tests/test_metrics.py docs/remote-setup.md`: All checks passed
- `ruff format --check backend/app/metrics.py tests/test_metrics.py`: 2 files already formatted
- `grep -rnE "RELEVANCE_MIN\s*=\s*[0-9]" backend/app --include="*.py"`: без совпадений
- Мутация (а) прицельным прогоном `tests/test_metrics.py tests/test_stages.py`: 1 failed (ожидаемый тест), дерево восстановлено
- Мутация (б) тем же прогоном: 1 failed (тот же тест), дерево восстановлено
- Пересчёт эффекта на `data/scout.db` независимым запросом: 5/5 и 9/2 — совпадает
- `git show 55b26eb --stat`: `selection.py` отсутствует
- `git diff 87575fe HEAD -- backend/app/config.py tests/test_stages.py tests/test_contest_gate_and_order.py`: пусто
