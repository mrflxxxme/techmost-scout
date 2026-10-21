# Верификация Э6-6а, повторная (цикл 2)
Вердикт: PASS (закрыто 4 из 4 критериев; критерий 1 — в редакции РЕШ-280)
Рабочая копия: `.claude/worktrees/e6-6a`, ветка `claude/e6-6a`, HEAD `ae510ce` до коммита отчёта.
Окружение: `/home/user/techmost-scout/.venv`, перед каждым pytest сброшены переменные секретов и настроек из задания.

## Критерии приёмки

| № | Критерий | Команда | Факт | Статус |
|---|---|---|---|---|
| 1а | Публичный хост: рабочие пути 404 без `WWW-Authenticate` (РЕШ-280) | `pytest tests/test_host_routing.py -v` | 30 passed; 8 параметров пути и POST `/requests` — passed | ок |
| 1б | Каждый путь параметризации существует в роутерах | `rg` по `backend/app/web/routes/`, `main.py` (таблица ниже) | все пути найдены; `/api/docs`, `/api/openapi.json` — при `app_env` `dev`/`test` (`main.schema_urls`), тест собирает `build_app("test")` | ок |
| 1в | `/requests/1/report` есть | `rg -n "requests/\{request_id\}/report" backend/app/web/routes/report.py` | строка 44: `@router.get("/requests/{request_id}/report")` | ок |
| 1г | `/` → 302 на `/scan` | `pytest -k корень_публичного` | `test_корень_публичного_хоста_ведёт_на_scan PASSED` | ок |
| 1д | Мутация «пропускать рабочие пути на публичном хосте» → ≥1 failed | `sed` в `backend/app/web/host_routing.py`: `if not is_public_path(path):` → `if False:`, затем `pytest tests/test_host_routing.py -q` | 12 failed, 18 passed; откат `git checkout --`, дерево чистое | ок |
| 2 | Тесты Э6-6а: `test_scan_limits.py -v` | `pytest tests/test_scan_limits.py -v` | 20 passed | ок |
| 3а | Мутация: резерв незавершённых убран | `unfinished * settings.scan_cost_limit_rub` → `unfinished * 0`, pytest | 1 failed, 19 passed; откат | ок |
| 3б | Мутация: `X-Forwarded-For` доверяется всегда | `if not _is_private(peer):` → `if False:`, pytest | 2 failed, 18 passed; откат | ок |
| 3в | Мутация «пропускать рабочие пути» | см. 1д | 12 failed | ок |
| 4 | `rg -ni "captcha" backend/ tests/` — пусто | `rg -ni "captcha" backend/ tests/` | пустой вывод, exit 1 | ок |
| 5 | Полный `pytest -q`: 0 failed (ожидание ~1567) | `pytest -q` | 1572 passed, 0 failed, 1 warning (deprecation httpx в starlette.testclient) | ок |
| 6 | `ruff check .` | `ruff check .` | All checks passed! | ок |
| 7 | `ruff format --check .` | `ruff format --check .` | 237 files already formatted | ок |
| 8 | Тесты в диффе относительно `claude/sleepy-planck-11w8jv` | `git diff --stat claude/sleepy-planck-11w8jv HEAD -- tests/` | только `test_config.py` (+18/−3), `test_host_routing.py` (+218), `test_scan_limits.py` (+279) | ок |
| 8б | `test_config.py`: 7 новых имён в `_AWAITING_OWNER_IN_EXAMPLE` (РЕШ-274) | `git diff ... -- tests/test_config.py` | добавлены `PUBLIC_SCAN_HOST`, `SCAN_COST_LIMIT_RUB`, `SCAN_DAILY_BUDGET_RUB`, `SCAN_LIMIT_PER_IP_PER_DAY`, `SCAN_ORDER_EMAIL`, `SCAN_ORDER_TELEGRAM_URL`, `YANDEX_SEARCH_RUB_PER_QUERY` — 7 имён; прочих правок множества нет | ок |

### Таблица путей критерия 1б (РЕШ-280)

| Путь из параметризации | Определение в приложении | Подтверждение |
|---|---|---|
| `/requests` | `web/routes/pages.py:45` `@router.get("/requests")` | мутация: 401 без пароля |
| `/requests/1/report` | `web/routes/report.py:44` | мутация: не 404 |
| `/requests/1/review` | `web/routes/review.py:196` | мутация: не 404 |
| `/requests/1/export.pdf` | `web/routes/report.py:108` | мутация: не 404 |
| `/demo` | `web/routes/demo.py:90` | мутация: не 404 |
| `/showcase` | `web/routes/pages.py:57` | мутация: не 404 |
| `/api/docs` | `main.py` `schema_urls`, при `app_env` = `test` | мутация: не 404 |
| `/api/openapi.json` | `main.py` `schema_urls`, при `app_env` = `test` | мутация: не 404 |
| POST `/requests` | `web/routes/intake.py:187` | мутация: 401 |
| `/` | `web/routes/pages.py:33`; редирект на `/scan` даёт прослойка | тест 302 passed |

Все восемь параметров упали под мутацией, то есть каждый путь существует и закрыт проверкой пароля. Буквальных `/request/1` и `/history` в `tests/test_host_routing.py` нет (`rg`, exit 1): по РЕШ-280 они заменены рабочими путями.

## Инварианты

- №3 секреты: `rg -n -e "sk-" -e "gigachat" -e "Api-Key" backend/ tests/` не пуст, 10 файлов — тот же набор, что в прежней верификации: фиктивные ключи-заглушки в тестах, заголовки `Api-Key` в адаптерах и `llm/yandex.py`, комментарий в `config.py`. Совпадений в добавленных строках диффа (`git diff ... HEAD -- backend/ tests/ | rg "^\+.*(sk-|gigachat|Api-Key)"`) нет, exit 1. Значения ключей в отчёт не выведены.
- №6 httpx: `rg -ln httpx backend/app` даёт только `search/`, `llm/`, `cli_doctor.py`, `pipeline/pipeline_context.py`, `pipeline/pipeline_search.py` — все перечислены в карте INVARIANTS (строка 6). Ветка этих файлов не меняет.
- №7 без сети: `unshare -rn pytest -q` — 1572 passed, exit 0.
- №8 backend/ и tests/ изменены вместе: в диффе `backend/` 6 файлов (`config.py`, `db.py`, `main.py`, `models.py`, `web/host_routing.py`, `web/scan_limits.py`), в `tests/` 3 файла.
- №9 персональные данные: `test_адрес_не_пишется_в_базу` и `test_адрес_не_попадает_в_лог` — passed.
- №4 `test_prompts.py` — входит в полный прогон (0 failed); промпты в диффе не менялись.
- Слияние 9de9a0c (Э6-4, РЕШ-271…279): полный прогон, ruff и format чистые; регрессий не выявлено.

## Отложено

- **DLG-1 (контейнерный прогон не выполнен, РЕШ-249 / РЕШ-276).** Бинарь `docker` есть, демон недоступен (`docker info` не отвечает), в задаче Э6-6а критерия про контейнер нет. Действие владельца: приложить вывод CI GitHub (Ubuntu, Python 3.12) или прогон контейнера `python:3.12-slim` к `.planning/evidence/`. Ворота этапа — владелец.

## Замечания к отчёту

- Число тестов: 1572 passed против ожидания «около 1567» оркестратора. Ровно 0 failed. Расхождение на 5 тестов не разбиралось по коммитам: файлы из диффа (`test_host_routing` 30, `test_scan_limits` 20, `test_config` 14 в сборе) дают 64 теста из трёх файлов.
- Сборка по путям из PLAN дословно (`/request/1`, `/history`) в тестах отсутствует; критерий 1 закрыт по редакции РЕШ-280, как и поставлено оркестратором.

## Причина FAIL
Не применимо: вердикт PASS.
