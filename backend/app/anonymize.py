"""Проверка обезличенности текста задачи (FR-4, РЕШ-189 п. 1).

Ищет в свободном тексте фрагменты, похожие на названия компаний-заказчиков:
юридическую форму (ООО, АО, ПАО, ЗАО, ОАО, НАО, ИП) рядом со словом с заглавной
буквы или названием в кавычках-ёлочках, а также латинские имена в стиле
CamelCase (AcmeCorp). Проверка выполняется кодом и регулярными выражениями,
без обращения к языковой модели (РЕШ-189 п. 1) — это дешевле и не требует
сети. Словарь «известных брендов» сознательно не заводится (РЕШ-189 п. 3):
такой список быстро устаревает и создаёт ложное чувство полноты проверки.

Имена ПО из белого списка `reference/software_names.yaml` (TensorFlow, MatLab)
риском не считаются (РЕШ-291): совпадение должно быть целым и точным, без учёта
регистра, — «TensorFlowLabs» остаётся риском.

Модуль не решает, блокировать отправку или только предупредить, — это решение
экрана интейка (И-9/И-10). Здесь только поиск фрагментов.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

_SOFTWARE_NAMES_PATH = Path(__file__).resolve().parent / "reference" / "software_names.yaml"

# Юридические формы из FR-4. Порядок в альтернативе не важен: каждая
# альтернатива огорожена \b с обеих сторон, поэтому «АО» не совпадёт как
# часть «ПАО», «ЗАО», «ОАО» или «НАО» — там перед «АО» нет границы слова.
_LEGAL_FORMS = r"(?:ООО|ПАО|ЗАО|ОАО|НАО|АО|ИП)"

# Юридическая форма плюс название: слово с заглавной буквы (например,
# «ООО Ромашка») или текст в кавычках-ёлочках (например, «АО «Ромашка»»).
# Слово без заглавной буквы после формы («ООО работает над проектом») не
# считается названием и не совпадает — так спелл-аут «Акционерное общество»
# посреди предложения не даёт ложного срабатывания.
_LEGAL_ENTITY = re.compile(rf"\b{_LEGAL_FORMS}\b\s*(?:«[^»\n]{{1,80}}»|[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)")

# Латинское CamelCase-имя: минимум два «горба» заглавных букв, каждый со
# строчными буквами после (AcmeCorp, TechMost). Аббревиатуры из одних
# заглавных букв («ГОСТ», «TRL», «GOST») не совпадают — после заглавной
# буквы регулярное выражение требует хотя бы одну строчную.
_CAMEL_CASE = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b")


@lru_cache(maxsize=1)
def load_software_names() -> frozenset[str]:
    """Прочитать белый список имён ПО (строчными буквами); кэшируется на процесс.

    Единый источник для формы скана, интейка и `/api/v1` (РЕШ-291). Неверная
    структура файла — `ValueError`: тихо пустой список вернул бы ложные отказы.
    """
    data = yaml.safe_load(_SOFTWARE_NAMES_PATH.read_text(encoding="utf-8"))
    names = data.get("names") if isinstance(data, dict) else None
    if not isinstance(names, list) or not all(isinstance(n, str) and n.strip() for n in names):
        raise ValueError("Справочник имён ПО: ожидается список строк в поле names")
    lowered = [n.strip().lower() for n in names]
    if len(set(lowered)) != len(lowered):
        raise ValueError("Справочник имён ПО: дубли имён")
    return frozenset(lowered)


def find_deidentification_risks(text: str) -> list[str]:
    """Ищет в тексте фрагменты, похожие на названия компаний-заказчиков.

    Возвращает список найденных фрагментов в порядке появления в тексте, без
    повторов. Пустой список означает, что подозрительных фрагментов не
    найдено — это не гарантия обезличенности, а лучшая дешёвая проверка,
    которую можно сделать без вызова модели.
    """
    software = load_software_names()
    matches: list[tuple[int, str]] = []
    for pattern in (_LEGAL_ENTITY, _CAMEL_CASE):
        for match in pattern.finditer(text):
            if pattern is _CAMEL_CASE and match.group(0).lower() in software:
                continue
            matches.append((match.start(), match.group(0).strip()))

    matches.sort(key=lambda item: item[0])

    seen: set[str] = set()
    result: list[str] = []
    for _, fragment in matches:
        if fragment not in seen:
            seen.add(fragment)
            result.append(fragment)
    return result
