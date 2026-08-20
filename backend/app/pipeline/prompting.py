"""Работа с промптами: загрузка, подстановка данных, обезвреживание меток.

Общие функции, которыми пользуются все остальные стадии пайплайна: интейк,
план поиска, extractor, скоринг, карточки, сводка. Вынесено отдельно из
`stages.py` (РЕШ-94), потому что это единственный слой, от которого зависят
все остальные модули стадий, и держать его рядом с самой стадией значило бы
плодить циклические импорты.
"""

from __future__ import annotations

import json
import re

from app.config import get_settings
from app.schemas import Brief, Candidate, ScoredCandidate

#: Короткая команда в роли пользователя: всё содержательное уже в системном
#: промпте вместе с данными, здесь только сигнал «работай».
_GO = "Выполни инструкцию выше. Верни только JSON без пояснений."


def load_prompt(name: str) -> str:
    """Прочитать промпт из prompts/. Промпты — часть контракта, а не строки в коде."""
    path = get_settings().prompts_dir / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"нет промпта {path}. См. docs/SPEC.md, раздел про prompts/")
    return path.read_text(encoding="utf-8")


def load_gov_support() -> str:
    """Справочник инструментов господдержки — подаётся модели как контекст."""
    path = get_settings().data_dir / "gov_support.yaml"
    return path.read_text(encoding="utf-8") if path.exists() else ""


_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


def render_prompt(name: str, **values: str) -> str:
    """Подставить данные в промпт.

    Промпты содержат метки вида {{BRIEF_JSON}} — это часть их контракта. Если
    метку не заполнить, модель получит её текстом и начнёт рассуждать о самой
    метке вместо данных: именно так пайплайн какое-то время выдавал карточки с
    низкой уверенностью и жалобами «кандидаты не переданы».

    Поэтому незаполненная метка — ошибка, а не мелочь: падаем сразу и громко.
    """
    text = load_prompt(name)
    for key, value in values.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    leftover = sorted(set(_PLACEHOLDER.findall(text)))
    if leftover:
        raise ValueError(f"промпт {name}: не заполнены метки {leftover}. Передайте их в render_prompt")
    return text


def candidates_to_json(group: list[ScoredCandidate]) -> str:
    """Кандидаты для модели: только поля, по которым можно сверять утверждения."""
    return json.dumps(
        [
            {
                "url": str(item.candidate.url),
                "title": item.candidate.title,
                "org": item.candidate.org,
                "authors": item.candidate.authors,
                "year": item.candidate.year,
                "source_type": item.candidate.source_type.value,
                "provider": item.candidate.provider,
                # Обрезаем: длинные аннотации раздувают вход, а для сверки
                # утверждений хватает первых полутора тысяч знаков.
                "snippet": item.candidate.snippet[:1500],
            }
            for item in group
        ],
        ensure_ascii=False,
        indent=2,
    )


def owner_answers_block(brief: Brief) -> str:
    """Ответы владельца на уточняющие вопросы — блоком для сообщения модели.

    Нужен там, где бриф уходит в промпт не целиком: в стадиях `plan_search`,
    `author_card` и `summarize` бриф подаётся как `BRIEF_JSON`, и поле
    `owner_answers` попадает в модель само, а скоринг собирает сообщение из
    отдельных полей руками — без этого блока ответы владельца до него не
    доходили бы (РЕШ-58).

    Пустая строка, когда ответов нет: пустой заголовок в промпте — это шум,
    который дешёвая модель принимается толковать.
    """
    if not brief.owner_answers:
        return ""
    lines = "\n".join(f"— {item.question}\n  Ответ: {item.answer}" for item in brief.owner_answers)
    return f"Уточнения владельца (контекст постановки, не ограничения задачи):\n{lines}\n\n"


#: Настоящая метка шаблона, пришедшая не из шаблона, а из данных: имя внутри
#: — ровно то же, что ищет `_PLACEHOLDER`, потому что обезвреживать надо
#: строго то, на чём падает `render_prompt`, и ничего сверх того.
_DATA_PLACEHOLDER = re.compile(r"\{{2,}([A-Z_]+)\}{2,}")


def neutralize_placeholders(text: str) -> str:
    """Обезвредить метки вида `{{ИМЯ}}`, пришедшие из сырого ввода оператора.

    `render_prompt` ищет незаполненные метки в **уже собранном** тексте, а не в
    шаблоне до подстановки, поэтому метка, попавшая в промпт из данных, роняет
    вызов ничуть не хуже забытой метки шаблона: `brief_from_raw_text` переносит
    текст оператора дословно, и задача с `{{CLIENT_NAME}}` детерминированно
    убивала прогон на планировщике.

    Лечим данные, а не проверку: общий контракт `render_prompt` — «незаполненная
    метка это ошибка, падаем сразу и громко» — стоит дороже, чем этот случай,
    и ослаблять его ради него нельзя. Скобки метки схлопываются в одинарные:
    `{{CLIENT_NAME}}` → `{CLIENT_NAME}`. Текст остаётся читаемым (его увидит
    клиент в отчёте), а под шаблон метка больше не подходит; длина серии скобок
    значения не имеет — `{{{{X}}}}` схлопывается за один проход.

    Правится **только настоящая метка**, а не любые сдвоенные скобки (П-6
    аудита). Прежняя редакция схлопывала их без разбора и портила содержание:
    `{"a": {"b": 1}}` превращался в сломанный `{"a": {"b": 1}`, а `if (x) {{
    y(); }}` — в `{ y(); }`. Для стадии extractor это прямой удар по главному:
    промпт требует ДОСЛОВНОЙ цитаты, а модель цитировала бы уже испорченный
    текст, и в карточку попадала бы цитата, не совпадающая со страницей.
    """
    return _DATA_PLACEHOLDER.sub(r"{\1}", text)


def candidates_listing(chunk: list[Candidate], citations: dict[str, int] | None = None) -> str:
    """Перечисление кандидатов для сообщения скоринга.

    `citations` — цитируемость по ключу склейки кандидата, её добирает
    обогащение по DOI (РЕШ-50). Строка «Цитирований» появляется, только когда
    число действительно известно: «неизвестно» в промпте — это приглашение
    модели домыслить, а выдуманных значений во входе стадии быть не должно
    (инвариант №1).
    """
    known = citations or {}
    blocks: list[str] = []
    for i, c in enumerate(chunk):
        lines = [
            f"[{i}] {c.title}",
            f"URL: {c.url}",
            f"Организация: {c.org or 'не указана'}",
            f"Год: {c.year or 'не указан'}",
            f"Тип: {c.source_type.value}",
        ]
        cited = known.get(c.dedup_key())
        if cited is not None:
            lines.append(f"Цитирований: {cited}")
        lines.append(f"Фрагмент: {c.snippet[:600]}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
