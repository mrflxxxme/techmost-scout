# Ревью + верификация З-5 · цикл 1

Вердикт: ПРИНЯТЬ (блокирующих 0, советов 2)

## Проверка по критериям

1. **На откате догадок в карточку не попадает.** `pytest tests/test_fallback_without_guesses.py -v` — 2 passed.
   Тест сквозной и честный: XML Яндекса → `FakeProvider`-заглушка роняет стадию `extractor` (невалидный JSON на
   основном и ремонтном вызове) → `outcome.candidates` с `org is None`/`year is None` → `candidates_to_json`
   отдаёт `"org": null` (проверено grep-ом по самой строке промпта, а не косвенно) → системное сообщение модели
   card_author не содержит домен → отрендеренный `render_markdown` не содержит `pt.2035.university` и содержит
   «не найдено». Тест не подменяет проверку на слабую — утверждения именно о конечном артефакте (markdown), а не
   только о промежуточном состоянии кандидата.
2. **Кандидаты не теряются.** Тот же файл: `len(outcome.candidates) == 2` при двух документах выдачи; в логе
   `caplog` — «беру разбор адаптера», в `outcome.problems` — «взят разбор адаптера». Сообщения совпадают с
   реальными строками `orchestrator.py:555,560,564,570` (код оркестратора не менялся).
3. **Сырые записи по-прежнему полны.** `_raw_records` (yandex_search.py:466-480) не тронута: `domain` и
   `modtime` остаются в словаре. Новый тест `test_search_raw_несёт_domain_и_modtime_для_стадии_extractor`
   проверяет это явно (`raw[0]["domain"]`, `raw[0]["modtime"]`).
4. **Прежние ожидания обновлены, а не удалены.** `tests/test_search_yandex.py:96-104` — старые
   `card.org == "pure.manchester.ac.uk"` / `card.year == 2024` заменены на `is None` с комментарием, что это
   сознательная правка дефекта, а не потеря покрытия. Остальные проверки теста (title, snippet, raw_id) на
   месте. Число тестов в файле: было 24, стало 25 (добавлен один, критерий 3), ни один не удалён —
   `git show 0f6ffd3:tests/test_search_yandex.py | grep -c "^async def test\|^def test"` → 24;
   `grep -c "^async def test\|^def test" tests/test_search_yandex.py` → 25.
5. **Пайплайн и промпты не тронуты.** `git diff --name-only 0f6ffd3 14e2d73 | grep -E "pipeline/|prompts/"` —
   пусто.
6. **Полный набор и ruff.**
   `env -u RELEVANCE_MIN -u SECOND_PASS_RELEVANCE_MAX -u YANDEX_SEARCH_API_KEY -u YANDEX_CATALOG_ID
   -u LLM_STAGE_ROUTES -u LLM_FALLBACK_ORDER pytest -q --deselect tests/test_team_docs.py::test_state_stays_short`
   → **666 passed, 1 deselected** (воспроизведено самостоятельно, совпадает с заявленным). `ruff check .` →
   All checks passed. `ruff format --check .` → 117 files already formatted.

## Мутации (воспроизведены независимо, 2 из 4 обязательных + проверен откат дерева)

- **(а) `org` снова из домена.** Правка `org=None` → `org=(doc.findtext("domain") or "").strip() or None`.
  Прогон `pytest tests/test_fallback_without_guesses.py tests/test_search_yandex.py -q` → **4 failed**: оба
  теста отката и `test_разбирает_ответ_в_кандидатов`, `test_search_raw_несёт_domain_и_modtime_для_стадии_extractor`.
  Совпадает с отчётом исполнителя.
- **(б) `year` снова из `modtime`.** Правка `year=None` → инлайн-эквивалент `_year_from_modtime`. Тот же прогон
  → **4 failed**, те же тесты, падение на `year`. Совпадает с отчётом исполнителя.
- Дерево после каждой мутации восстановлено копией оригинала; `diff backend/app/search/yandex_search.py
  <(git show HEAD:backend/app/search/yandex_search.py)` → идентично; `git status --porcelain` пуст.
- Мутации (в) и (г) не воспроизводились повторно — обе описаны в `.planning/evidence/E2D-З5-mutations.md` с
  конкретным падающим тестом и правдоподобны по чтению кода (в) убирает ключи из `_raw_records`, что напрямую
  ловится новым тестом через `KeyError`; (г) обнуляет `_to_candidates` в `search_raw`, что ловится подсчётом
  кандидатов в тесте отката. Двух независимо воспроизведённых мутаций достаточно по заданию цикла; обе упавшие
  ожидаемым образом — признаков «зелено по неверной причине» не найдено.

## Инварианты

- №1 (главный) — соблюдён: см. критерий 1 выше, конечная проверка на markdown, а не только на промежуточном
  объекте.
- №3 — `grep -rE "sk-|gigachat|Api-Key" backend/ tests/` даёт много совпадений, но все — имя провайдера
  («gigachat» как строковый идентификатор) и формат HTTP-заголовка `f"Api-Key {self._api_key}"`
  (yandex_search.py:328, не тронут этой задачей); значений ключей нет нигде, включая evidence-файлы задачи
  (отдельно проверено grep-ом по `sk-|Api-Key` на трёх изменённых файлах — нашлась только форматная строка
  заголовка).
- №5 — откат по-прежнему не роняет прогон, виден в логе и `problems` (критерий 2).
- №6 — `grep -rln "httpx" backend/app` не даёт новых мест вне `llm/`, `search/`, `orchestrator.py`, `cli.py` —
  без изменений от базовой линии.
- №7 — весь набор проходит без сети (respx/заглушки), воспроизведено выше.
- №8 — изменения в `backend/` (`yandex_search.py`) сопровождаются изменениями в `tests/` (оба тестовых файла).
- №9 — diff строго в границах: `backend/app/search/yandex_search.py`, `tests/test_search_yandex.py`,
  `tests/test_fallback_without_guesses.py` (новый) плюс evidence. `docs/SPEC.md`/`prompts/`/`pipeline/` не
  тронуты.
- №10 — evidence-YAML (`E2D-З5-handoff.yaml`) визуально проверен: строки со значениями, содержащими двоеточие,
  закавычены; `pytest tests/test_team_docs.py` — 34 passed, 1 failed (`test_state_stays_short`, известная и
  заранее объявленная в задании деселекция из-за роста `STATE.md`, к этой задаче не относится и файл
  `STATE.md` этой задачей не трогался).

## Оценка критерия (г): `health()` пересчитан на `raw_id` вместо `org`

Вынужденное следствие фикса, а не выход за границы: без правки `scout doctor` показывал бы «доменов 0» на
каждом успешном прогоне (после занятия `org=None` у всех веб-кандидатов), то есть диагностика начала бы лгать
именно там, где она обязана предупреждать о проблеме с ключом/квотой. Правка в том же файле, из явного списка
задачи, границы не нарушены (домены `sources_unavailable`, `max_queries`, другие адаптеры и «улучшение
извлечения организации» не тронуты). Логика `{(c.raw_id or "").split("/", 1)[0] for c in found if c.raw_id}`
корректно даёт хост и для `raw_id` без пути (`normalize_url` не добавляет хвостовой слэш к голому домену).

## Советы (не блокируют)

1. Новая ветка `health()` (подсчёт доменов по `raw_id`) не покрыта ни одним тестом — ни до, ни после правки
   успешный путь `health()` с непустой выдачей в `test_search_yandex.py` не тестировался вовсе. Это
   унаследованный пробел, не введённый этой задачей, но раз поведение подсчёта доменов изменилось, стоило бы
   закрыть его отдельным тестом (не обязательно в этой задаче — граница задачи явно запрещала трогать другие
   темы, а не запрещала добавить тест на уже изменённую функцию; решил не блокировать, так как это не входит в
   перечисленные критерии приёмки и не относится к находке В-2).
2. Комментарий в `notes` handoff исполнителя про `_card_platform` в `export/markdown.py` — полезное наблюдение
   за пределами задачи, оставлено на усмотрение владельца волны, действие не требуется сейчас.

## Что теперь видит клиент на откате (было / стало)

- **Было** (дефект В-2): «Организация: pt.2035.university» (домен, выданный за организацию) и «Год публикации
  проекта — 2025» (время правки страницы, выданное за год публикации) — обе строки в «Чем подтверждено» без
  права на это по инварианту №1.
- **Стало**: обе строки печатаются как «не найдено» (проверено в `render_markdown` в
  `tests/test_fallback_without_guesses.py`), домен остаётся доступен в `url`/`raw_id` кандидата, стадия
  `extractor`, если сработает, получает исходные `domain`/`modtime` в сырых записях и вправе сама сделать
  обоснованный вывод — уже не адаптер выдаёт догадку за факт.

## Проверено (полный список фактических прогонов)

- `pytest tests/test_fallback_without_guesses.py -v`: 2 passed
- `pytest tests/test_search_yandex.py -v`: 31 passed
- `env -u RELEVANCE_MIN -u SECOND_PASS_RELEVANCE_MAX -u YANDEX_SEARCH_API_KEY -u YANDEX_CATALOG_ID
  -u LLM_STAGE_ROUTES -u LLM_FALLBACK_ORDER pytest -q --deselect tests/test_team_docs.py::test_state_stays_short`:
  666 passed, 1 deselected
- `ruff check .`: All checks passed!
- `ruff format --check .`: 117 files already formatted
- `pytest tests/test_team_docs.py -q`: 34 passed, 1 failed (`test_state_stays_short`, вне границ задачи)
- Мутация (а) `org` из домена: 4 failed (ожидаемо)
- Мутация (б) `year` из `modtime`: 4 failed (ожидаемо)
- Дерево после мутаций: побайтово идентично HEAD, `git status --porcelain` пуст
- `git diff --name-only 0f6ffd3 14e2d73`: только `backend/app/search/yandex_search.py`,
  `tests/test_search_yandex.py`, `tests/test_fallback_without_guesses.py` и файлы `.planning/evidence/E2D-З5-*`
