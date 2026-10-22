"""Вычистка контактов из строк и ссылок экспресс-скана (Э6-6б, РЕШ-264).

Публичный скан не показывает ни телефонов, ни почты — ни в тексте, ни в адресе
ссылки. Чистых функций две: `scrub_contacts` для строк и `scrub_url` для ссылок.
Имена лиц и почтовые адреса в прозе код не распознаёт: это решение владельца.

Телефон ищется по явному признаку: код в скобках, начало `+` или `8`/`7` с кодом,
группировка 3-3-2-2 или ровно десять цифр с кодом оператора. Общего «много цифр
подряд» нет намеренно: годы, суммы и номера документов телефоном не являются.
Если форма неотличима от суммы и явного признака нет — текст остаётся как есть.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

#: Замена телефонов и почты в строках вида (РЕШ-264).
CONTACT_HIDDEN = "[контакт скрыт]"

#: Тире и дефисы всех видов — в «-», неразрывные и тонкие пробелы — в обычный.
#: Замена строго «один знак на один», поэтому позиции в нормализованной и
#: исходной строках совпадают, и вычищается именно исходный текст.
_LOOK_ALIKE = str.maketrans(
    {
        **{ord(ch): "-" for ch in "‐‑‒–—―−­"},
        **{ord(ch): " " for ch in "           　"},
    }
)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
#: Международная запись: `+`, код страны, код, группы цифр (10–15 цифр всего — проверка ниже).
_PLUS_PHONE = re.compile(r"(?<![\w+])\+\d{1,3}[\s\-]?(?:\(\d{2,5}\)|\d{2,5})(?:[\s\-]?\d{2,4}){2,4}(?!\d)")
#: `8`/`7` и код: «8 (495) 123-45-67», «84951234567», «8-800-100-12-34».
_PREFIXED_PHONE = re.compile(r"(?<![\w.+])[78][\s\-.]?(?:\(\d{3}\)|\d{3})[\s\-.]?\d{3}[\s\-.]?\d{2}[\s\-.]?\d{2}(?!\d)")
#: Код города в скобках — признак телефона: «(495) 123-45-67», «(3822) 12-34-56».
_PAREN_PHONE = re.compile(r"(?<![\w])\(\d{3,5}\)[\s\-]?\d{2,3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)")
#: Группировка 3-3-2-2 с пробелами или дефисами: «495 123 45 67». Слева не цифра и
#: не «цифра + пробел», иначе хвост большой суммы сошёл бы за телефон.
_GROUPED_PHONE = re.compile(r"(?<!\d)(?<!\d\s)(?<!\d-)\d{3}[\s\-]\d{3}[\s\-]\d{2}[\s\-]\d{2}(?![\d\-])")
#: Десять цифр подряд с кодом, какие бывают у российских номеров (3xx, 4xx, 8xx, 9xx).
_SOLID_PHONE = re.compile(r"(?<![\w.,\-])[3489]\d{9}(?![\w])")
#: Городской семизначный номер с дефисами: «123-45-67».
_LOCAL_PHONE = re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{2}(?![\d-])")


def _digits(value: str) -> int:
    return sum(ch.isdigit() for ch in value)


def _is_international(value: str) -> bool:
    return 10 <= _digits(value) <= 15


def _hide(pattern: re.Pattern[str], text: str, *, check=None) -> str:
    """Заменить совпадения шаблона в исходной строке, искав их в нормализованной."""
    normal = text.translate(_LOOK_ALIKE)
    parts: list[str] = []
    last = 0
    for match in pattern.finditer(normal):
        if check is not None and not check(match.group()):
            continue
        parts.append(text[last : match.start()])
        parts.append(CONTACT_HIDDEN)
        last = match.end()
    parts.append(text[last:])
    return "".join(parts)


def scrub_contacts(text: str) -> str:
    """Заменить в строке адреса почты и телефоны на `[контакт скрыт]`."""
    text = _hide(_EMAIL, text)
    text = _hide(_PLUS_PHONE, text, check=_is_international)
    for pattern in (_PREFIXED_PHONE, _PAREN_PHONE, _GROUPED_PHONE, _SOLID_PHONE, _LOCAL_PHONE):
        text = _hide(pattern, text)
    return text


#: Параметры запроса, которые по имени несут контакт.
_CONTACT_PARAMS = re.compile(r"(?:^|[_\-])(?:e-?mail|mail|phone|tel|mobile|contact|login|user)", re.IGNORECASE)


def _clean_piece(piece: str) -> str:
    """Сегмент пути или значение запроса: контакт внутри — весь сегмент в заглушку."""
    decoded = unquote(piece)
    return "_" if scrub_contacts(decoded) != decoded else piece


def scrub_url(url: str) -> str:
    """Ссылка для публичного вида: без логина и пароля, фрагмента и контактов.

    Выбрано чистить ссылку модели, а не подменять её ссылкой кандидата: обе
    записи указывают на одну страницу (сверка идёт по `normalize_url`), а часть
    источников (например, патентный реестр Беларуси) держит номер документа в
    параметрах запроса, и без них ссылка не откроется. Поэтому запрос не
    отбрасывается целиком: убираются параметры с контактом в имени или значении.
    Не `http`/`https` (`mailto:`, `tel:` и подобные) печатать нельзя — пустая строка.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return ""
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{parts.port}" if parts.port else host
    path = "/".join(_clean_piece(piece) for piece in parts.path.split("/"))
    pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _CONTACT_PARAMS.search(key) and _clean_piece(key) == key and _clean_piece(value) == value
    ]
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(pairs), ""))
