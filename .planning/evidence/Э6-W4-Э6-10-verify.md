# Верификация Э6-10 · Резерв под аналоги и честный предел платных запросов Яндекса
Вердикт: PASS (закрыто 6 из 6 критериев дополнения РЕШ-285; открытых DLG-N нет)

Рабочая копия: /home/user/techmost-scout/.claude/worktrees/e6-10, ветка claude/e6-10, HEAD 15a3d3f.
База: claude/sleepy-planck-11w8jv = d6ef0f2 (содержит Э6-6б). Код задачи: d4426fe — предок HEAD (проверено).
Окружение: /home/user/techmost-scout/.venv, `env -u` из регламента, без секретов в выводе.

## Критерии приёмки (PLAN.md, дополнение РЕШ-285, Э6-10)

| № | Критерий | Команда | Факт | Статус |
|---|---|---|---|---|
| 1 | Кейсы а–ж в tests/test_yandex_quota.py проходят | `pytest tests/test_yandex_quota.py -v` | 14 passed | ок |
| 2 | Мутации: каждая ≥1 failed | см. таблицу мутаций ниже | 8 из 8: все ≥1 failed, откат чистый | ок |
| 3 | Прежние тесты зелёные без правок | `pytest <16 файлов из кр. 3> -q` | 344 passed; `git diff --stat claude/sleepy-planck-11w8jv HEAD -- tests/` — только `tests/test_yandex_quota.py | 351 +++` | ок |
| 4 | Деньги и границы не сдвинуты | `rg -n "max_queries: int = 6" backend/app/search/yandex_search.py`; `rg -n "YANDEX_QUERIES_PER_SCAN = 6" backend/app/web/scan_limits.py`; `git diff --stat <база> -- prompts/ backend/app/export/ backend/app/web/ backend/app/pipeline/planning.py backend/app/search/analog_sources.py`; `git diff --name-only <база> -- backend/` | 1 строка (yandex_search.py:159); 1 строка (scan_limits.py:34); diff пуст; ровно 3 файла: pipeline_search.py, yandex_quota.py, yandex_search.py | ок |
| 5 | Замка нет, `begin_pass` только в трёх файлах | `rg -n "self\._lock\b" backend/app/search/yandex_search.py backend/app/search/yandex_quota.py`; `rg -ln "begin_pass" backend/app` | пусто; yandex_search.py, yandex_quota.py, pipeline_search.py | ок |
| 6 | Общие: pytest 0 failed, ruff 0, инв. 6 пусто | `pytest -q`; `ruff check .`; `ruff format --check .`; grep инв. 6 | 1891 passed, 0 failed; All checks passed!; 252 files already formatted; grep инв. 6 пусто | ок |

### Кр. 1: кейсы а–ж (сопоставление с тестами, строки из tests/test_yandex_quota.py)

| Случай | Тест | Что проверяется в ассертах | Строки |
|---|---|---|---|
| а | test_десять_параллельных_web_без_begin_pass_дают_ровно_шесть_запросов | call_count == 6, queries_used == 6, 4 записи, по одной на каждую формулировку по тексту, peak in-flight ≥ 2 | 161–169 |
| б | test_пять_web_и_два_английских_analog_без_begin_pass_дают_ровно_шесть | 6 POST, queries_used == 6 | 182–184 |
| в | test_рынок_отдаёт_аналогам_шесть_минус_три_и_называет_вытесненное | 6 POST; 3 прочие по плану; по 1 пачке каждой en-analog и ru-analog; вторые пачки не отправлены; 4 прочие и 2 домена-пачки в problems с «места оставлены аналогам» и «предел 6»; всего 6 записей | 199–219 |
| г | test_без_русской / test_без_английских / test_без_analog_совсем | 4 / 5 / 6 прочих; записи о резерве 3 / 2 / 0 (для «без analog» — только запись LIMIT, без «оставлены аналогам») | 231–232, 244–246, 256–258 |
| д | test_одна_английская_analog_получает_обе_пачки | обе пачки отправлены, записи о них нет, 6 POST | 271–273 |
| е | test_наука_analog_снят_кодом_и_все_шесть_мест_у_web | план после normalize: analog нет; 6 web; analog 0; записей о резерве нет | 283–301 |
| ж | test_неиспользованные_места_первого_прохода_достаются_второму; test_queries_used_присваивается_а_begin_pass_счёт_не_сбрасывает | 1-й проход 5; 2-й проход отправил 1, queries_used == 6, 2 записи «второй проход»; повторный begin_pass счёт не обнулил | 316–324, 348–351 |
| + | test_оборванный_запрос_место_не_возвращает_и_остаётся_searcherror | место остаётся занятым; limit_problems пуст | 340–341 |

Код (проверено чтением diff d4426fe): `reserve_other()` и `reserve_analog()` стоят до первого `await` запроса; `_run_search` больше не инкрементирует счёт после `post`; `self._lock` в адаптере не используется; `begin_pass` вызывается из `gather_candidates` через `getattr`.

### Кр. 2: мутации (по одной, откат `git checkout` после каждой, `git status` чист)

Прогон: `pytest tests/test_yandex_quota.py -q` на каждой мутации. Скрипт применяет замену, проверяет число вхождений = 1 и откатывает.

| Мутация | Что сделано | Результат |
|---|---|---|
| M1 | Проверка без учёта в `search_raw`/`_search_analog`; `used += 1` после `await post` в `_run_search` | 10 failed, 4 passed (ок) |
| M2 | `async with _MUT_LOCK:` вокруг `await post` (модульный `asyncio.Lock`) | 9 failed, 5 passed (ок) |
| M3 | `begin_pass`: `world_slots = 0`, `registry_slots = 0` (аналоги без мест) | 6 failed, 8 passed (ок) |
| M4 | `reserve = (world_slots + registry_slots) or 3` (резерв 3 без analog) | 4 failed, 10 passed (ок) |
| M5 | `gather_candidates`: `begin_pass = None` (не зовётся) | 5 failed, 9 passed (ок) |
| M6 | `begin_pass` адаптера: `self._quota.used = 0` перед раздачей | 2 failed, 12 passed (ок) |
| M7 | Мировые места «в глубину» (`_mut_deep` вместо `split_width` в `YandexQuota.begin_pass`) | 1 failed, 13 passed (ок) |
| M8 | `_note_displaced` заменён на `pass` (вытесненная прочая не пишется) | 6 failed, 8 passed (ок) |

### Инварианты (INVARIANTS.md)

- №3 секреты: `git diff claude/sleepy-planck-11w8jv HEAD -- backend tests | grep '^+' | grep -E "sk-|gigachat|Api-Key|GigaChat"` — пусто; ключеподобных значений (`sk-` + 20 и более символов) в добавленных строках 0; по дереву совпадения только известные имена заголовков и комментарии, уже присутствующие в базе (`Api-Key` в config.py и llm/yandex.py, `X-API-Key` в web/api_access.py). Значения ключей не выводились.
- №4 подстановки: `pytest tests/test_prompts.py` — 59 passed. Промпты не менялись.
- №5 отказ: транспортный отказ остаётся SearchError (тест «оборванный запрос», строки 340–341); пределный отказ не SearchError. Прежние `test_search_failures.py` и `test_pipeline_counters.py` входят в кр. 3 — зелёные.
- №6 httpx: точная команда инварианта (`grep -rln "httpx" backend/app --include='*.py' | grep -vE ...`) — пусто. `yandex_quota.py` — 0 упоминаний httpx.
- №7 без сети: `unshare -rn` + полный `pytest -q` — 1891 passed; в netns интерфейсов только `lo`.
- №8 тесты с кодом: в диффе `backend/` (3 файла) и `tests/` (1 новый файл `tests/test_yandex_quota.py`).
- №1, №2, №9, №10: к задаче не относятся напрямую; diff не трогает export/, web/, prompts/, клиентских текстов; новых режимов и вне-скоуп фич нет.

## Общие проверки

- `pytest -q` (env -u): 1891 passed, 0 failed. База: 1891 − 14 новых = 1877 (diff tests/ добавляет только этот файл), ожидание совпало.
- `ruff check .`: All checks passed!; `ruff format --check .`: 252 files already formatted.
- Размеры: yandex_search.py 425 строк, pipeline_search.py 434, yandex_quota.py 140 — все ≤ 500.
- `git status`: чисто до записи отчёта; после 8 мутаций `git status --short -- backend tests` пусто; `git diff --stat HEAD` пусто.

## Отложено

- Нет. Все проверки выполнены; живых платных запросов критерии не требуют.

## Причина FAIL

Не применимо (вердикт PASS).
