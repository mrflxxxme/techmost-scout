"""Стражи изоляции тестовой оснастки от боевого `.env` и от переменных
окружения процесса (РЕШ-97, РЕШ-113, РЕШ-120; долг 7 ворот Э2-Г, З-1 Э2-Д).

Пять проверок: настройки не читают файл `.env`; движок базы тестов — временная
память, а не файл рабочей копии; ни одно поле настроек не берёт значение из
боевого файла `.env` мимо умолчания или `TEST_ENV`; чистка `purge_settings_env`
снимает ровно переменные настроек и не трогает остальное окружение (`PATH`,
`HOME`, `HTTPS_PROXY`); ни одна переменная окружения поля `Settings` во время
прогона не отличается от значения `TEST_ENV`. Сообщения о провале называют
только имена полей и переменных, не значения (инвариант №3): секреты
сравниваются как `SecretStr`, чей `__repr__` маскирует содержимое.
"""

from __future__ import annotations

import os

from app.config import Settings, get_settings

# Импорт `app.db` — на уровне модуля, а не внутри тела теста ниже. Цикл 2
# ревью (СВ-1): движок создаётся на импорте самого модуля `app.db`
# (`_settings = get_settings()` на верхнем уровне), а модульный импорт ЭТОГО
# файла происходит на этапе сбора тестов pytest — до того, как успела бы
# отработать любая фикстура, в том числе гипотетическая, куда мутация
# «TEST_ENV перенесена из импорта в фикстуру» переносит установку окружения.
# Импорт внутри тела функции этой гарантии не давал: тело теста выполняется
# ПОСЛЕ фикстур своего теста, и узкий прогон одного этого файла не ловил
# перенос установки TEST_ENV в фикстуру — страж проходил не по собственному
# устройству, а по случайному совпадению с чужим импортом `app.main` в
# tests/test_web_auth.py при полном прогоне набора.
from app.db import engine
from conftest import TEST_ENV, purge_settings_env


def test_settings_model_config_has_no_env_file() -> None:
    """`Settings` не должна читать файл `.env` — конфигурация обнулена на
    импорте `tests/conftest.py` (РЕШ-120)."""
    assert not Settings.model_config.get("env_file"), (
        "Settings.model_config['env_file'] должен быть пустым: боевой файл .env не читается"
    )


def test_db_engine_uses_in_memory_sqlite() -> None:
    """Движок базы тестов — временная память, а не файл рабочей копии.

    Проверяет модульный `engine`, импортированный выше вместе с остальными
    импортами файла — то есть на этапе сбора тестов, а не внутри тела этой
    функции. Так страж не зависит от того, что какой-то другой тестовый файл
    (например `tests/test_web_auth.py`, импортирующий `app.main`) успел
    подтянуть `app.db` раньше и тем самым случайно подстраховал проверку под
    узким прогоном одного этого файла.
    """
    assert ":memory:" in str(engine.url), "движок базы тестов должен быть sqlite в памяти"


def test_settings_fields_trace_to_default_or_test_env() -> None:
    """Каждое поле настроек равно умолчанию либо значению из `TEST_ENV`.

    Граница проверки расширена задачей З-1 (Э2-Д, долг 7 ворот Э2-Г): раньше
    здесь была оговорка, что переменная, уже стоящая в `os.environ` до
    запуска pytest, этим стражем не ловится и ловиться не может — это больше
    не так. `purge_settings_env` вычищает окружение процесса от всех имён
    настроек ещё на импорте `tests/conftest.py`, до того как отработает
    любая фикстура и до первого импорта `app.db`, поэтому к моменту вызова
    `get_settings()` в `os.environ` для этих имён остаётся только `TEST_ENV`.
    Эталон `Settings(_env_file=None)` читает тот же (уже вычищенный)
    `os.environ`, поэтому сравнение проверяет отсутствие расхождений, а не
    полагается на то, что боевые переменные никогда не появятся.

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


def test_purge_settings_env_removes_only_settings_names() -> None:
    """`purge_settings_env` снимает ровно переменные настроек, не больше.

    Работает на обычном словаре, а не на `os.environ`: так проверка
    независима от того, что уже стоит в окружении процесса, запустившего
    pytest. Берём по одной боевой переменной на каждый из трёх видов имени
    (с `validation_alias`, без него, и производную из `TEST_ENV`) плюс три
    заведомо системных имени, которых чистка обязана не тронуть.
    """
    fake_environ: dict[str, str] = {
        "GIGACHAT_AUTH_KEY": "боевой-ключ",  # поле с validation_alias
        "ROSPATENT_API_KEY": "боевой-ключ",  # поле без validation_alias
        "RELEVANCE_MIN": "4",  # поле без validation_alias, боевой порог
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "HTTPS_PROXY": "http://proxy.example:8080",
    }
    removed = purge_settings_env(fake_environ)

    assert set(removed) == {"GIGACHAT_AUTH_KEY", "ROSPATENT_API_KEY", "RELEVANCE_MIN"}
    assert "PATH" in fake_environ
    assert "HOME" in fake_environ
    assert "HTTPS_PROXY" in fake_environ
    assert fake_environ["PATH"] == "/usr/bin"


def test_process_env_settings_names_match_test_env_or_absent() -> None:
    """Страж по имени файла: переменная поля `Settings` в окружении процесса
    либо отсутствует, либо равна значению из `TEST_ENV`.

    В отличие от `test_settings_fields_trace_to_default_or_test_env` (который
    сравнивает уже построенные объекты `Settings`), этот страж читает
    `os.environ` напрямую и обходит `Settings.model_fields` целиком — тем же
    способом, каким `purge_settings_env` вычисляет имена. Так проверка не
    зависит ни от кэша `get_settings()`, ни от того, что вычисляет сам
    `Settings`, и ловит саму дыру задачи З-1: боевую переменную процесса,
    просочившуюся в окружение мимо `TEST_ENV`. Сообщение о провале называет
    только имя переменной (инвариант №3); значения сравниваются как
    `SecretStr`, чей `__repr__` их не раскрывает.
    """
    from pydantic import SecretStr

    mismatched: list[str] = []
    for field_name, field_info in Settings.model_fields.items():
        alias = field_info.validation_alias
        name = (alias if isinstance(alias, str) else field_name).upper()
        if name not in os.environ:
            continue
        expected = TEST_ENV.get(name)
        if expected is None or SecretStr(os.environ[name]) != SecretStr(expected):
            mismatched.append(name)
    assert not mismatched, f"переменные окружения процесса вне TEST_ENV: {mismatched}"
