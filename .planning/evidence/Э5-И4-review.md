# Ревью Э5-И4 · цикл 1

Вердикт: **ВЕРНУТЬ** (блокирующих 1 категория из 2 конкретных мутаций, советов 1)

## Блокирующие

1. **[инвариант №8 / «зелено по неверной причине»] Реальная сборка приложения
   (`main.py`) не покрыта тестами — обе мутации, прямо перечисленные в задаче
   ревью, переживают весь набор из 1271 теста.**

   Поведенческие тесты `test_prod_app_returns_404_on_schema_endpoints` и
   `test_dev_app_returns_200_on_schema_endpoints` (tests/test_web_api_schema.py:32-51)
   строят **одноразовый** `FastAPI(docs_url=docs_url, openapi_url=openapi_url)`
   из значений, которые сами же получили от `schema_urls(...)`, — они проверяют
   только саму функцию `schema_urls`, а не то, как её результат довозится до
   боевого объекта `app` в `main.py`.

   Аналогично `test_base_html_hides_api_link_when_schema_closed` и
   `test_base_html_shows_api_link_when_schema_open` (строки 54-73) не проверяют,
   что `main.py` сам кладёт `api_docs_url` в `templates.env.globals` — они
   подменяют это значение вручную через `monkeypatch.setitem`, минуя реальный
   код инициализации.

   Проверено двумя мутациями (каждая откачена `git checkout --`, не коммичена):

   а) В `main.py` заменил `openapi_url=openapi_url` на захардкоженное
      `openapi_url="/api/openapi.json"` при том, что `docs_url=docs_url`
      остался как есть (частичная разводка — ровно то, о чём предупреждает
      задача ревью: «оставь docs_url живым, но убери openapi_url»).
      Результат: `pytest -q` → **1271 passed**, `pytest
      tests/test_web_api_schema.py -q` → **9 passed**. Ни один тест не
      покраснел. Прямая проверка живого `main.app` под `APP_ENV=prod`
      показывает утечку:
      ```
      GET /api/openapi.json -> 200 OK   (должно быть 404)
      GET /api/docs         -> 404      (корректно)
      ```
      Ручная команда критерия 4 (`print(app.docs_url, app.openapi_url)`)
      печатает `None /api/openapi.json` — расхождение видно, но это не
      pytest-проверка, а разовая команда для протокола; в CI она не гоняется
      и не падает, полагаться на неё как на сторожа нельзя.

   б) В `main.py` закомментировал строку
      `templates.env.globals["api_docs_url"] = docs_url`. Результат:
      `pytest -q` → **1271 passed**. Ни один тест не заметил пропажи.
      Прямая проверка: `"api_docs_url" in main.templates.env.globals` → `False`
      под `APP_ENV=test` — то есть в реальном приложении глобал вообще не
      появляется, ссылка «API» не покажется ни в `dev`, ни в `test`, при этом
      весь набор тестов зелёный.

   Обе мутации — ровно те, что явно перечислены в задаче ревью как критическая
   проверка «дыры в стороже», и обе её не ловят. Это значит: критерии приёмки
   2 и 3 плана проверены не на боевом пути кода, а на его отдельно
   пересобранных копиях, и регрессия в самой разводке `schema_urls() → app` /
   `schema_urls() → templates.env.globals` пройдёт через ревью и CI незамеченной
   бессрочно. Нужен минимум один тест, который берёт `main.app` /
   `main.templates` напрямую (с подменой `settings.app_env` до вызова
   `schema_urls` в `main.py`, либо рефактором в отдельную функцию сборки
   приложения, вызываемую тестом) и бьёт по нему `TestClient`.

   Третья мутация из списка задачи («оставь docs_url живым, но убери
   openapi_url», в самой функции `schema_urls`, а не в разводке) —
   поймана: `pytest tests/test_web_api_schema.py -q` → 3 failed из 9
   (см. «Проверено» ниже). Проблема именно в мосте между `schema_urls()` и
   местами её использования, а не в самой функции.

## Не блокирует, но стоит отметить (совет)

1. Докстрока `schema_urls` утверждает «любое значение `app_env`, кроме `dev`
   и `test` **в нижнем регистре**» и «дают `(None, None)`», подразумевая
   нормализацию регистра. Фактически `.lower()` нигде не вызывается — сравнение
   строгое (`app_env in ("dev", "test")`). Проверено: `schema_urls("DEV")` и
   `schema_urls("Dev")` дают `(None, None)`, а не открывают схему. Это не
   баг безопасности (ошибка регистра закрывает схему, а не открывает —
   отказ в безопасную сторону), но текст докстроки вводит в заблуждение:
   стоит либо добавить `.lower()`, либо переформулировать комментарий, чтобы
   не обещать регистронезависимость, которой нет.

## Существо проверено самостоятельно

- `/api/openapi.json` в `prod` недоступен на боевом `app` (в исходном,
  немутированном коде): `TestClient(app).get(...)` → 404 (см. «Проверено»).
- Третьего пути к схеме нет: под `APP_ENV=prod` проверены `/redoc`,
  `/api/redoc`, `/docs`, `/openapi.json` — все 404. Причина механическая и
  надёжная: в FastAPI 0.141.1 маршрут redoc добавляется только если заданы
  **оба** `openapi_url` и `redoc_url` (`applications.py:1149`); раз
  `openapi_url=None` в `prod`, redoc не поднимается вне зависимости от того,
  что `redoc_url` в `main.py` не задан явно и остаётся дефолтным `/redoc`.
- `/health` открыт без авторизации и в `dev`, и в `prod` — подтверждено
  `TestClient`-ом (`200 OK` без `auth=`).
- Замечание исполнителя о критерии 2 плана — **подтверждено**:
  `inspect.signature(FastAPI.__init__)` в установленной версии (0.141.1)
  даёт `docs_url` на позиции 13, `openapi_url` — на позиции 7; порядок
  параметров начинается с `(self, debug, routes, title, summary,
  description, version, openapi_url, ...)`. Буквальная сборка
  `FastAPI(*schema_urls("prod"))` расставила бы `debug=None` — то есть текст
  плана ошибочен, а не работа исполнителя. Именованная сборка
  `FastAPI(docs_url=..., openapi_url=...)` в тесте — правильный способ
  достичь цели критерия (закрыто в prod, открыто в dev). Дефект в
  формулировке плана, не в реализации.
- Границы задачи соблюдены: диффом подтверждено, что изменены только
  `backend/app/main.py`, `backend/app/web/templates/base.html`,
  `tests/test_web_api_schema.py` и файлы `.planning/evidence/Э5-И4-*`.
  `backend/app/config.py`, `require_auth`, `.env.example` не тронуты.
  `main.py` — 107 строк (лимит ≤110 соблюдён).
- Инвариант №3: `grep -rE "sk-|gigachat|Api-Key" backend/ tests/` находит
  только предсуществующие, не относящиеся к этой задаче совпадения (заголовки
  YandexGPT/Rospatent, тестовые фиктивные ключи вида `sk-fake-leak-check-*`);
  ничего нового этой задачей не добавлено, значения секретов в отчёте и
  командах не печатались.
- Инвариант №7: все тесты используют `TestClient`/`ASGITransport`, сети не
  требуют.
- Инвариант №9: `git diff` не показывает новых настроек, middleware или
  второго признака «мы в бою» — только `schema_urls(settings.app_env)`.

## Проверено (фактический вывод команд)

```
pytest tests/test_web_api_schema.py -v: 9 passed
pytest tests/test_web_templates.py tests/test_web_auth.py tests/test_web_flow_e2e.py -v: 12 passed
pytest -q: 1271 passed
ruff check .: All checks passed!
ruff format --check .: 210 files already formatted
PYTHONPATH=backend APP_ENV=prod python -c "from app.main import app; print(app.docs_url, app.openapi_url)": None None
rg -n 'docs_url="/api/docs"' backend/app/main.py: пусто (exit 1)
rg -n 'href="/api/docs"' backend/app/web/templates/base.html: пусто (exit 1)
TestClient(app) под APP_ENV=prod: /api/docs 404, /api/openapi.json 404, /redoc 404,
  /api/redoc 404, /docs 404, /openapi.json 404, /health 200 (без авторизации)
TestClient(app) под APP_ENV=dev, без авторизации: /api/docs 200, /api/openapi.json 200,
  /redoc 200, /health 200

Мутация 1 (открыто по умолчанию, `app_env != "prod"`):
  pytest tests/test_web_api_schema.py -q -> 2 failed, 7 passed (поймано)
Мутация 2 (убрать регистр — вызова .lower() в коде нет, проверено напрямую):
  schema_urls("DEV") == (None, None); schema_urls("Dev") == (None, None) — закрыто,
  но не по регистронезависимости, а потому что строка не совпадает буквально
Мутация 3а (закрыть в schema_urls только docs_url, оставить openapi_url открытым
  внутри самой функции): pytest tests/test_web_api_schema.py -q -> 3 failed, 6 passed (поймано)
Мутация 3б (docs_url корректно разведён, но openapi_url захардкожен в вызове
  FastAPI(...) в main.py, минуя schema_urls): pytest -q -> 1271 passed (НЕ поймано);
  живой TestClient под APP_ENV=prod: /api/openapi.json -> 200 (утечка)
Мутация 4 (base.html: зашитый /api/docs вместо {{ api_docs_url }}):
  pytest tests/test_web_api_schema.py -q -> 1 failed, 8 passed (поймано)
Мутация 5 (main.py: удалена строка templates.env.globals["api_docs_url"] = docs_url):
  pytest -q -> 1271 passed (НЕ поймано); "api_docs_url" in main.templates.env.globals -> False
```

Все мутации откачены `git checkout --`, рабочее дерево чистое (`git status` —
nothing to commit).

---

# Ревью Э5-И4 · цикл 2 (последний)

Вердикт: **ПРИНЯТЬ**

Коммит `3abaa13` поверх отчёта цикла 1 (`1226831`). Диф: `git diff 1226831 3abaa13`
— тронуты только `backend/app/main.py`, `tests/test_web_api_schema.py` и
`.planning/evidence/Э5-И4-*` (handoff, pytest.txt). Границы задачи не нарушены.

## Закрытие блокирующей находки цикла 1

Введена `build_app(app_env)` — единственная функция сборки приложения:
`docs_url, openapi_url = schema_urls(app_env)` передаются в `FastAPI(...)`,
`templates.env.globals["api_docs_url"] = docs_url` выставляется внутри неё же.
Боевой объект теперь `app = build_app(settings.app_env)` — то есть тесты,
вызывающие `main.build_app("prod")` / `main.build_app("dev")`, гоняют ровно
тот код, которым собирается реальное приложение, а не отдельную копию мимо
него.

Обе мутации цикла 1 поставлены заново лично (каждая через `git checkout --`
после прогона, в ветку не коммитились):

1. **Внутри `build_app` захардкожен `openapi_url="/api/openapi.json"`** при
   корректно разведённом `docs_url=docs_url`:
   ```
   pytest tests/test_web_api_schema.py -q -> 1 failed, 12 passed
     FAILED test_prod_app_returns_404_on_schema_endpoints
   pytest -q -> 1 failed, 1274 passed
   ```
   Красная. Откат — `pytest -q`: 1275 passed.

2. **Удалена строка `templates.env.globals["api_docs_url"] = docs_url`**
   внутри `build_app`:
   ```
   pytest tests/test_web_api_schema.py -q -> 1 failed, 12 passed
     FAILED test_base_html_shows_api_link_when_schema_open
   pytest -q -> 1 failed, 1274 passed
   ```
   Красная. Откат — `pytest -q`: 1275 passed.

Обе находки цикла 1 закрыты фактически, не только текстом: тест теперь бьёт
по `main.build_app`, которым же собирается боевой `main.app`.

## Проверка второй точки правды

`build_app(app_env: str)` принимает окружение параметром и нигде внутри себя
не читает `settings` — единственное обращение к `settings.app_env` во всём
файле находится в последней строке `app = build_app(settings.app_env)`, вне
самой функции. Второго признака «мы в бою» не заведено. Зашитых адресов не
осталось: `rg -n 'docs_url="/api/docs"' backend/app/main.py` и
`rg -n 'href="/api/docs"' backend/app/web/templates/base.html` — оба пустые.
`base.html` в этом цикле не тронут (диф это подтверждает) — прежняя правка
цикла 1 (`{% if api_docs_url %}`) сохранилась.

## Автофикстура `_restore_api_docs_url`

`build_app` пишет в общий модульный `templates.env.globals` — тесты внутри
файла делят это состояние. Проверено:

- Обратный порядок узлов внутри `tests/test_web_api_schema.py` (все 13,
  `tac` от списка `--collect-only`) — 13 passed, включая случаи, когда тест
  на закрытую ссылку идёт после теста на открытую и наоборот.
- Явный запуск в паре `test_base_html_shows_api_link_when_schema_open` →
  `test_base_html_hides_api_link_when_schema_closed` и в паре
  `test_dev_app_returns_200_on_schema_endpoints` →
  `test_prod_app_returns_404_on_schema_endpoints` — 2 passed в обоих случаях.
- Файл вместе с `tests/test_web_templates.py tests/test_web_auth.py
  tests/test_web_flow_e2e.py`, схема-файл **первым** и **последним** в
  команде — 25 passed в обоих порядках.
- Все три прежних файла по отдельности, без файла схемы: `test_web_templates.py`
  — 5 passed, `test_web_auth.py` — 6 passed, `test_web_flow_e2e.py` — 1 passed.
  Совпадает с их независимым поведением, утечки состояния наружу нет.

Порядок прогона на результат не влияет, состояние в другие файлы не течёт.

## Рост `main.py` до 121 строки

Машинного стража на число 110 нет (`grep -n "110" tests/test_backend_layout.py
tests/test_pipeline_layout.py` — пусто); лимит ≤110 в плане был бюджетом
первого цикла, а не стражем. Рост на 14 строк — прямое следствие вынесения
сборки приложения в `build_app(app_env)`, что и было необходимо для закрытия
находки цикла 1: без выделения отдельной вызываемой функции тест не может
дотянуться до настоящего кода сборки, минуя который проходила прошлая дыра.
Рост оправдан по существу, лишнего в функции не появилось (тот же набор
действий, что был раньше на уровне модуля, просто обёрнут в `def`). Общий
лимит 500 строк (`tests/test_backend_layout.py`) далёк.

## Критерии приёмки (повторно, лично)

```
pytest tests/test_web_api_schema.py -v: 13 passed
pytest tests/test_web_templates.py tests/test_web_auth.py tests/test_web_flow_e2e.py -v: 12 passed
pytest -q: 1275 passed
ruff check .: All checks passed!
ruff format --check .: 210 files already formatted
rg -n 'docs_url="/api/docs"' backend/app/main.py: пусто (exit 1)
rg -n 'href="/api/docs"' backend/app/web/templates/base.html: пусто (exit 1)
grep -rE "sk-|gigachat|Api-Key" backend/app/main.py tests/test_web_api_schema.py
  .planning/evidence/Э5-И4-*.{yaml,txt}: пусто
git diff 1226831 3abaa13 --name-only: backend/app/main.py, tests/test_web_api_schema.py,
  .planning/evidence/Э5-И4-handoff.yaml, .planning/evidence/Э5-И4-pytest.txt — границы задачи
  не нарушены
```

## Итог

Класс находки цикла 1 (тест мимо реального кода сборки — «зелено по
неверной причине») закрыт правильным способом: не заплаткой в тесте, а
выделением единственной вызываемой точки сборки, которую тест и вызывает.
Обе мутации, ранее проходившие незамеченными через 1271 тест, теперь красят
ровно тот тест, который обязан их поймать. Побочных эффектов автофикстуры и
регрессий в соседних файлах не найдено. Совет цикла 1 про докстроку
регистрозависимости выполнен и закреплён тестом
`test_schema_urls_comparison_is_case_sensitive`, а не оставлен текстом.

Второй цикл ревью — последний по регламенту; блокирующих находок нет,
третий цикл не требуется.
