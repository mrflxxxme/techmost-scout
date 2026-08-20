# Верификация З-1 подэтапа Э2-В
Вердикт: **PASS** (закрыто 6 из 6 критериев)

## Критерии приёмки

| № | Критерий | Команда | Факт | Статус |
|---|---|---|---|---|
| 1 | `pytest tests/test_env_isolation.py -v` | pytest tests/test_env_isolation.py -v | 3 passed в 0.39 с | ✓ PASS |
| 2 | Базовая линия (577) + новые стражи (+3) | pytest -q | 580 passed, 1 warning в 316,53 с (фон) | ✓ PASS |
| 3 | Прогон с подложным `.env` идентичен | pytest -q (с `.env`) | 580 passed, 1 warning в 338,70 с | ✓ PASS |
| 4 | `data/should-not-be-touched.db` не создан | ls data/should-not-be-touched.db | No such file (expected) | ✓ PASS |
| 5 | Линтер чистый | ruff check . && ruff format --check . | All checks passed, 98 files already formatted | ✓ PASS |
| 6 | Правки только в `tests/` | git diff --stat 0142185..HEAD | conftest.py, test_env_isolation.py, test_pipeline_resilience.py (backend/, prompts/, docs/, scripts/ не тронуты) | ✓ PASS |

## Четыре мутации — поставлены и проверены

| Мутация | Способ | Факт | Статус |
|---|---|---|---|
| (а) `env_file` возвращён в `model_config` | Закомментировать `Settings.model_config["env_file"] = None` | test_settings_model_config_has_no_env_file FAILED с AssertionError | ✓ Убита |
| (б) `TEST_ENV` перенесена из импорта в фикстуру | Закомментировать `os.environ.update(TEST_ENV)` | test_db_engine_uses_in_memory_sqlite FAILED (узкий прогон), engine.url указывает на data/scout.db вместо :memory: | ✓ Убита узким |
| (в) Убрана подмена `app.db.session_scope` (2 теста) | Закомментировать строки 917 и 947 в test_pipeline_resilience.py | Тест зависает при попытке доступа к реальной БД (timeout после 2м) | ✓ Убита |
| (г) `DATABASE_URL` убран из `TEST_ENV` | Закомментировать `"DATABASE_URL": "sqlite+aiosqlite:///:memory:"` в TEST_ENV | test_db_engine_uses_in_memory_sqlite FAILED, engine.url указывает на data/scout.db | ✓ Убита |

## Инварианты

| № | Проверка | Команда | Факт | Статус |
|---|---|---|---|---|
| 3 | Секретов нет | grep -rE "sk-|gigachat|Api-Key" backend/ tests/ | Только текст в docstring (gigachat) - утечек ключей нет | ✓ OK |
| 7 | Тесты без сети | pytest прошёл на machine без интернета | Все внешние вызовы подменены respx/заглушки | ✓ OK |
| 8 | backend/ и tests/ изменены вместе | git diff --stat 0142185..HEAD | Только tests/ изменены: conftest.py (+36), test_env_isolation.py (+71), test_pipeline_resilience.py (+30); backend/ не тронут | ✓ OK |

## Границы и решения

- **РЕШ-120 соблюдена**: RELEVANCE_MIN и SECOND_PASS_RELEVANCE_MAX не добавлены в TEST_ENV
- **Граница дифа**: backend/, prompts/, docs/, scripts/ не тронуты
- **Две строки в рабочей базе** (РЕШ-114): не удалены, остаются после исправления тестов

## Важное уточнение по цикл 2

Ревью в цикле 1 указал на советы. Цикл 2 реализовал совет 1: импорт `from app.db import engine` перенесён на уровень модуля в `test_env_isolation.py` (строка 25). Это позволило узкому прогону `pytest tests/test_env_isolation.py -v` самостоятельно ловить мутацию (б) — без зависимости от побочного импорта в `test_web_auth.py`.

**Проверка обещания цикла 2:** Мутация (б) ловится узким прогоном ✓ (FAILED test_db_engine_uses_in_memory_sqlite).

## Заключение

Все шесть критериев приёмки закрыты. Четыре мутации убивают проверку. Инварианты соблюдены. Граница РЕШ-120 соблюдена. Обещание цикла 2 выполнено.

**Вердикт: PASS**
