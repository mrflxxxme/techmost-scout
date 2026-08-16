"""Замок на лимиты токенов стадий конвейера (Т-13).

На эти грабли проект наступил дважды. Первый раз — extractor: 4096 токенов
не хватало на разбор батча, ответ обрывался, роутер удваивал бюджет и платил
за вход второй раз (РЕШ-61 п. 1). Второй раз — scorer: соседнюю стадию тогда
не посмотрели, и на боевом прогоне рассуждающая модель съела весь лимит в
`reasoning_content`, отдав пустой `content`. Каждый батч скоринга скатывался в
нейтральные оценки — ранжирование умирало молча, при полностью оплаченном
прогоне.

Отсюда правило класса, а не отдельного случая: **у рассуждающей модели ни одна
стадия конвейера не живёт на умолчании роутера и ни одна не опускается ниже
безопасного порога**. Симптом нехватки у такой модели — не ошибка лимита, а
пустой ответ или обрыв JSON, то есть отказ, который сам о себе не сообщает;
поймать его можно только заранее, здесь.

Проверка статическая — по разбору исходника, без вызова модели (инвариант №7).
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

import app.pipeline
from app.pipeline.stages import score_candidates

#: Безопасный порог для рассуждающей модели. Ниже него в проекте не опускается
#: ни одна сознательно выбранная стадия: проверка и сводка — 6000, карточка и
#: extractor — 7000, план поиска, интейк и скоринг — 8000. Порог намеренно
#: выше умолчания роутера (4096): именно на нём и обожглись оба раза.
REASONING_STAGE_MIN_TOKENS = 6000

#: Имена методов роутера, которые тратят токены модели.
_LLM_CALLS = frozenset({"complete", "complete_json"})


def _stage_calls() -> list[tuple[str, int, ast.Call]]:
    """Все вызовы модели в пакете `app.pipeline` — файл, строка, узел.

    Разбирается весь пакет, а не только `stages.py`: новая стадия может завестись
    в соседнем модуле, и замок обязан достать её там же.
    """

    package_dir = Path(app.pipeline.__file__).parent
    found: list[tuple[str, int, ast.Call]] = []
    for source_path in sorted(package_dir.glob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _LLM_CALLS:
                found.append((source_path.name, node.lineno, node))
    return found


def _stage_name(node: ast.Call) -> str:
    """Первый позиционный аргумент вызова — имя стадии, для внятного сообщения."""

    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return "<стадия не распознана>"


def test_pipeline_has_llm_calls_to_guard():
    """Страховка от замка, который сторожит пустоту: если разбор перестал
    находить вызовы (переехал пакет, сменилось имя метода), молчаливо зелёные
    проверки ниже ничего не значат."""

    calls = _stage_calls()
    assert len(calls) >= 8, f"вызовов модели найдено {len(calls)} — разбор исходника сломался, замок ничего не сторожит"


def test_every_stage_sets_explicit_max_tokens():
    """Умолчание роутера — 4096, ровно тот скупой лимит, на котором рассуждающая
    модель отдаёт пустой `content`. Стадия обязана назвать свой лимит явно."""

    missing = [
        f"{file}:{line} стадия {_stage_name(node)}"
        for file, line, node in _stage_calls()
        if not any(keyword.arg == "max_tokens" for keyword in node.keywords)
    ]
    assert not missing, (
        "стадия зовёт модель без явного max_tokens и получает умолчание 4096 — "
        f"у рассуждающей модели это пустой ответ: {missing}"
    )


def test_no_stage_falls_below_reasoning_floor():
    """Ни одна стадия не опускается ниже безопасного порога.

    Это и есть замок: новая стадия с лимитом 4096 или понижение существующей
    роняют проверку до первого рубля, потраченного на боевом прогоне.
    """

    too_small: list[str] = []
    not_literal: list[str] = []
    for file, line, node in _stage_calls():
        for keyword in node.keywords:
            if keyword.arg != "max_tokens":
                continue
            value = keyword.value
            if not (isinstance(value, ast.Constant) and isinstance(value.value, int)):
                # Лимит, вычисляемый выражением, замок проверить не может —
                # значит и разрешать его нельзя, иначе дыра узаконена.
                not_literal.append(f"{file}:{line} стадия {_stage_name(node)}")
            elif value.value < REASONING_STAGE_MIN_TOKENS:
                too_small.append(f"{file}:{line} стадия {_stage_name(node)}: {value.value}")

    assert not not_literal, f"max_tokens задан выражением, а не числом — проверить порог нечем: {not_literal}"
    assert not too_small, (
        f"лимит стадии ниже порога {REASONING_STAGE_MIN_TOKENS} токенов. У рассуждающей модели "
        "лимит уходит в reasoning_content, content приходит пустым, и стадия деградирует молча "
        f"(скоринг так отдавал нейтральные оценки на каждом батче): {too_small}"
    )


def _scorer_max_tokens() -> int:
    """Лимит стадии scorer, вынутый из исходника `score_candidates`."""

    for _file, _line, node in _stage_calls():
        if _stage_name(node) != "scorer":
            continue
        for keyword in node.keywords:
            if keyword.arg == "max_tokens" and isinstance(keyword.value, ast.Constant):
                return int(keyword.value.value)
    pytest.fail("у стадии scorer нет max_tokens числом — либо лимит убрали, либо разбор исходника сломался")


def test_scorer_budget_covers_full_batch_answer():
    """Порога мало: у скоринга лимит обязан покрывать ответ на ПОЛНЫЙ батч плюс
    запас на рассуждение.

    Считаем по факту, а не на глаз: берём `batch_size` из сигнатуры стадии,
    собираем худший ответ `ScoreBatch` (пять оценок и `notes` предельной длины
    на каждого кандидата) и переводим знаки в токены по консервативному курсу
    два знака на токен. Проверка переживёт рост батча: увеличат `batch_size`
    вдвое — порог поедет вместе с ним и потребует поднять лимит.
    """

    batch_size = inspect.signature(score_candidates).parameters["batch_size"].default
    assert isinstance(batch_size, int) and batch_size > 0

    #: Самый длинный `notes` в примере prompts/scorer.md — 118 знаков; берём 160.
    item = {
        "index": batch_size - 1,
        "relevance": 10,
        "maturity": 10,
        "accessibility": 10,
        "freshness": 10,
        "completeness": 10,
        "notes": "О" * 160,
    }
    worst_answer = json.dumps({"scores": [item] * batch_size}, ensure_ascii=False, indent=2)
    answer_tokens = len(worst_answer) / 2  # консервативно: кириллица дешевле

    #: Нижняя граница рассуждения, измеренная боевым прогоном: на лимите 4000
    #: модель не дошла до ответа, значит одних рассуждений здесь больше 4 000.
    reasoning_floor_tokens = 4000

    assert _scorer_max_tokens() >= answer_tokens + reasoning_floor_tokens, (
        f"лимит скоринга не покрывает ответ на батч из {batch_size} кандидатов "
        f"(≈{answer_tokens:.0f} токенов) вместе с запасом на рассуждение ({reasoning_floor_tokens})"
    )
