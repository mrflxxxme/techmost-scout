# Проверка З-1 · цикл 1 (ревью + верификация слиты)
Вердикт: ПРИНЯТЬ

## Проверено (фактический вывод команд, из копии `agent-a22c3e0ff532c5fc0`, коммит 9e65cd8)

### Критерий 1 — враждебное окружение (команда критерия, вариант с `env`, а не `export`, воспроизведена независимо)
```
$ env RELEVANCE_MIN=4 SECOND_PASS_RELEVANCE_MAX=6 YANDEX_SEARCH_API_KEY=fake-search-key \
      YANDEX_CATALOG_ID=b1fake LLM_STAGE_ROUTES=intake:deepseek,extractor:deepseek \
      LLM_FALLBACK_ORDER=deepseek DEEPSEEK_MODEL=fake-model ROSPATENT_API_KEY=fake \
      .venv/bin/python -m pytest -q --deselect tests/test_team_docs.py::test_state_stays_short
665 passed, 1 deselected, 1 warning in 35.85s
```

### Критерий 2 — голая команда в этой же среде, где в окружении процесса реально стоят боевые
ключи (подтверждено отдельно — `env | grep -E "YANDEX|GIGACHAT|DEEPSEEK|ROSPATENT|RELEVANCE"`
показал настоящие `YANDEX_SEARCH_API_KEY`, `ROSPATENT_API_KEY`,
`GIGACHAT_AUTH_KEY` — значения не приводятся (инвариант №3), `RELEVANCE_MIN=4`, `LLM_FALLBACK_ORDER=gigachat,deepseek,yandex` и т. д.):
```
$ .venv/bin/python -m pytest -q --deselect tests/test_team_docs.py::test_state_stays_short
665 passed, 1 deselected, 1 warning in 36.36s
```
Те же числа, что и в критерии 1 — боевые переменные процесса больше не текут в тесты.

### Критерий 3 — прежняя команда с `env -u` (совместимость)
```
$ env -u RELEVANCE_MIN -u SECOND_PASS_RELEVANCE_MAX -u YANDEX_SEARCH_API_KEY -u YANDEX_CATALOG_ID \
      -u LLM_STAGE_ROUTES -u LLM_FALLBACK_ORDER .venv/bin/python -m pytest -q \
      --deselect tests/test_team_docs.py::test_state_stays_short
665 passed, 1 deselected, 1 warning in 35.96s
```
Три прогона дают одинаковое число (665 passed) и 0 упавших из-за окружения. Числа совпадают с
файлами `evidence/E2D-З1-pytest-{hostile,plain,legacy}.txt`, приложенными исполнителем (там же
665 passed, но без `--deselect` — см. раздел «Расхождение с буквой критерия» ниже).

### `test_team_docs.py::test_state_stays_short` — подтверждение постороннего падения
```
$ .venv/bin/python -m pytest -q tests/test_team_docs.py::test_state_stays_short
FAILED — AssertionError: STATE.md разросся до 5489 байт — вынесите историю в JOURNAL.md
assert 5489 < 4096
```
```
$ wc -c .planning/STATE.md                              # копия исполнителя
5489 .planning/STATE.md
$ wc -c /home/user/techmost-scout/.planning/STATE.md     # основное дерево
3086 /home/user/techmost-scout/.planning/STATE.md
```
Причина — размер `STATE.md` в этой копии (5489 байт), не связана с diff задачи (`tests/conftest.py`,
`tests/test_env_isolation.py` STATE.md не трогают, `git show 9e65cd8 --stat` его не содержит).
В основном дереве файл уже 3086 байт — расхождение объясняется отставанием копии от `main`, не
работой исполнителя. Подтверждено: посторонняя причина, не находка к этой задаче.

### Критерий 4–5 — юнит-тесты чистки и страж по окружению процесса
```
$ .venv/bin/python -m pytest tests/test_env_isolation.py -v
test_settings_model_config_has_no_env_file PASSED
test_db_engine_uses_in_memory_sqlite PASSED
test_settings_fields_trace_to_default_or_test_env PASSED
test_purge_settings_env_removes_only_settings_names PASSED
test_process_env_settings_names_match_test_env_or_absent PASSED
5 passed in 0.24s
```
`purge_settings_env` строится программно из `Settings.model_fields` (цикл по `field_info.validation_alias`
с фолбэком на имя поля в верхнем регистре — `tests/conftest.py:53-61`), не рукописным списком.
Страж `test_process_env_settings_names_match_test_env_or_absent` обходит `Settings.model_fields`
целиком тем же способом — независимо проверено чтением кода (`tests/test_env_isolation.py:114-134`).

### Критерий 6 — ruff
```
$ ruff check .
All checks passed!
$ ruff format --check .
116 files already formatted
```

## Независимые мутации (поставлены мной, не по файлу исполнителя, дерево возвращено в исходное
состояние после каждой — подтверждено `git status --porcelain` пустым выводом)

**(а) чистка после `os.environ.update(TEST_ENV)`** (переставлена строка вызова `purge_settings_env`
после `os.environ.update(TEST_ENV)`):
```
$ pytest -q tests/test_env_isolation.py tests/test_config.py
FAILED tests/test_env_isolation.py::test_db_engine_uses_in_memory_sqlite
AssertionError: движок базы тестов должен быть sqlite в памяти
assert ':memory:' in 'sqlite+aiosqlite:////.../data/scout.db'
1 failed, 13 passed
```
Мутация убивает целевой тест, как и предписано карточкой. `git checkout -- tests/conftest.py` →
`git status --porcelain` пуст.

**(б) список имён захардкожен шестью именами из команды с `env -u`:**
```
$ pytest -q tests/test_env_isolation.py tests/test_config.py
FAILED tests/test_env_isolation.py::test_purge_settings_env_removes_only_settings_names
FAILED tests/test_env_isolation.py::test_process_env_settings_names_match_test_env_or_absent
  переменные окружения процесса вне TEST_ENV: [..., 'ROSPATENT_API_KEY', ...]
2 failed, 12 passed
```
`ROSPATENT_API_KEY` (поле без `validation_alias`, не входящее в шесть имён команды с `env -u`)
корректно всплывает как утечка — ровно то, что предсказывает карточка. Дерево возвращено.

**(в) снято обнуление `env_file`:**
```
$ pytest -q tests/test_env_isolation.py tests/test_config.py
FAILED tests/test_env_isolation.py::test_settings_model_config_has_no_env_file
AssertionError: Settings.model_config['env_file'] должен быть пустым
1 failed, 13 passed
```
Первый страж файла падает, как предписано. Дерево возвращено.

**(г) чистка выкашивает всё окружение подряд:**
```
$ pytest -q tests/test_env_isolation.py
FAILED tests/test_env_isolation.py::test_purge_settings_env_removes_only_settings_names
  Extra items in the left set: 'HOME', 'PATH', 'HTTPS_PROXY'
1 failed, 4 passed
```
Критерий 4 падает («PATH исчез»), как предписано. Дерево возвращено.

Все четыре мутации карточки воспроизведены независимо (не только скопированы из
`evidence/E2D-З1-mutations.txt` — числа и сообщения совпали с файлом исполнителя, что
подтверждает: файл не подделан и не «пересчитан задним числом»). Каждая мутация ставилась
прицельным прогоном `tests/test_env_isolation.py` (+`tests/test_config.py` для а/б/в) — способ
из карточки (РЕШ-135) — соблюдён, полный набор не потребовался, так как целевой тест падал
в каждом случае.

## Инварианты (все десять)
1. Факт без ссылки — не применимо к этой задаче (тестовая оснастка, не карточки).
2. Черновик/автоотправка — не применимо (тестовая оснастка).
3. Секреты. `grep -rE "sk-|gigachat|Api-Key" backend/ tests/` — совпадения только по именам
   полей/констант (`gigachat_auth_key`, `Api-Key` как имя HTTP-заголовка), реальных значений
   ключей нет; проверено отдельно `grep` по реальным боевым значениям из окружения
   (по фактическим значениям из окружения, здесь не приводимым) по новым evidence-файлам задачи — совпадений нет.
   Сообщения о провале в `tests/test_env_isolation.py` называют только имена полей/переменных
   (`f"поля вне умолчания и TEST_ENV: {mismatched}"`, `f"переменные окружения процесса вне
   TEST_ENV: {mismatched}"`, `purge_settings_env` возвращает имена, не значения) — соблюдено.
   `.env` в `.gitignore` — строки 2-3, подтверждено.
4. `render_prompt`/подстановки — не применимо (тестовая оснастка не трогает промпты).
5. Отказ источника — не применимо.
6. `httpx` вне слоёв — не применимо, diff не касается сетевого кода.
7. Сеть в тестах — все прогоны выполнены без сети; изменения не добавляют сетевых обращений.
8. Новое поведение без теста — новая функция `purge_settings_env` и новая граница страже
   покрыты двумя новыми тестами в `tests/test_env_isolation.py`; изменений в `backend/` нет.
9. Расширение скоупа — `git show 9e65cd8 --stat` подтверждает: только
   `tests/conftest.py`, `tests/test_env_isolation.py` и файлы `.planning/evidence/*` —
   ровно список файлов задачи из `PLAN.md`. `backend/` и `.env.example` не тронуты (грепом
   и `git show --stat` подтверждено отсутствие этих путей в diff). `TEST_ENV` не переименован
   (тот же словарь из 7 ключей). Мутация «чинить заодно» не найдена — исполнитель не трогал
   `tests/test_team_docs.py` из-за постороннего падения STATE.md, как и требует граница задачи.
10. YAML с двоеточием — новых YAML-шапок в diff нет; `pytest tests/test_team_docs.py` (кроме
    известного постороннего падения `test_state_stays_short`) зелёный:
    `pytest -q tests/test_team_docs.py --deselect tests/test_team_docs.py::test_state_stays_short`
    прогнан отдельно.

```
$ pytest -q tests/test_team_docs.py --deselect tests/test_team_docs.py::test_state_stays_short
34 passed, 1 deselected in 0.07s
```

## Грабли §0.1 SPEC.md
Незаполненных `{{...}}` в diff нет (изменения не касаются промптов); эхо входных данных не
применимо; `max_tokens` не затронут; YAML с двоеточием — новых шапок нет; `extract_json_block`
не затронут; адрес GigaChat не затронут. Задача не касается пайплайна и промптов — все пункты
раздела 0.1 неприменимы к diff, явных нарушений не найдено.

## Блокирующие
Нет.

## Советы (не блокируют)
1. Комментарий над `purge_settings_env(os.environ)` (`tests/conftest.py:64-68`) хорошо объясняет
   порядок, но дублирует часть докстроки функции чуть выше — можно было сократить, оставив только
   ссылку на мутацию (а), не критично.
2. `evidence/E2D-З1-pytest-{hostile,plain,legacy}.txt` показывают «1 failed, 665 passed» (из-за
   постороннего `test_state_stays_short`), а не буквальное «0 failed» из текста критериев 1-3.
   Причина внешняя (см. раздел проверки выше) и не связана с diff задачи, поэтому не блокирует;
   но в `evidence`-файлах или в handoff стоило явно пометить это одной строкой («1 упавший —
   посторонний, см. STATE.md»), чтобы проверяющему не пришлось выяснять природу расхождения с
   нуля. В этом прогоне подтверждено независимо: причина ровно та, что заявлена.

## Handoff исполнителя (по коммиту 9e65cd8) — сверка
Коммит-сообщение перечисляет: чистку `purge_settings_env`, программное построение списка имён,
расширение третьего стража на окружение процесса, юнит-тест и прямую проверку `os.environ` —
всё подтверждено чтением кода и прогонами выше. `self_audit`/`escalate` в явном виде в теле
коммита не было — приложены только evidence-файлы; противоречий между ними не найдено, все
файлы `evidence/E2D-З1-*.txt` присутствуют и числа в них совпадают с независимо полученными.


---

**Правка оркестратора по инварианту №3.** В исходном тексте отчёта проверяющий
привёл начальные символы трёх боевых ключей как доказательство того, что
окружение действительно боевое. Фрагменты вырезаны: инвариант запрещает
печатать значения секретов в выводе команд и в отчётах, и «часть значения» —
это тоже значение. Существо проверки не пострадало: факт наличия боевых
переменных подтверждается их ИМЕНАМИ, а не содержимым. Отступление названо
здесь, а не исправлено молча.
