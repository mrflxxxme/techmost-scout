"""Стражи изоляции тестовой оснастки от боевого `.env` (РЕШ-97, РЕШ-113, РЕШ-120).

Три проверки: настройки не читают файл `.env`; движок базы тестов — временная
память, а не файл рабочей копии; ни одно поле настроек не берёт значение из
боевого окружения мимо умолчания или `TEST_ENV`. Сообщения о провале называют
только имена полей, не значения (инвариант №3): секреты сравниваются как
`SecretStr`, чей `__repr__` маскирует содержимое.
"""

from __future__ import annotations

from app.config import Settings, get_settings


def test_settings_model_config_has_no_env_file() -> None:
    """`Settings` не должна читать файл `.env` — конфигурация обнулена на
    импорте `tests/conftest.py` (РЕШ-120)."""
    assert not Settings.model_config.get("env_file"), (
        "Settings.model_config['env_file'] должен быть пустым: боевой файл .env не читается"
    )


def test_db_engine_uses_in_memory_sqlite() -> None:
    """Движок базы тестов — временная память, а не файл рабочей копии.

    `app.db` создаёт движок на импорте модуля (`_settings = get_settings()`
    на верхнем уровне), поэтому проверка возможна только импортом самого
    модуля — никакая фикстура не успела бы подменить его позже.
    """
    from app.db import engine

    assert ":memory:" in str(engine.url), "движок базы тестов должен быть sqlite в памяти"


def test_settings_fields_trace_to_default_or_test_env() -> None:
    """Каждое поле настроек равно умолчанию либо значению из `TEST_ENV`.

    Эталон строится с явным `_env_file=None` — переопределяет источник даже
    в сценарии, где `Settings.model_config['env_file']` восстановлен
    (мутация «а»): тогда `get_settings()` читает боевой `.env`, а эталон —
    нет, и расхождение ловит эту мутацию, а не только структурный страж выше.
    Обход всех полей `Settings.model_fields`, а не выборочных имён: страж не
    зависит от того, какие переменные перечислены в `TEST_ENV` сегодня.
    """
    reference = Settings(_env_file=None)
    actual = get_settings()
    mismatched = [name for name in Settings.model_fields if getattr(actual, name) != getattr(reference, name)]
    assert not mismatched, f"поля вне умолчания и TEST_ENV: {mismatched}"
