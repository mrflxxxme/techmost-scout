"""Учёт мест под платные запросы Яндекса — чистый код, без сети (Э6-10, РЕШ-285).

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ
    Предел `max_queries` — деньги владельца. До Э6-10 проверку места и учёт
    разделял сетевой вызов: все формулировки прохода стартуют задачами одной
    `TaskGroup`, и первые запросы всех проходили проверку раньше, чем кто-то
    успевал себя учесть (ревью Э6-2в, п. 4: 5 web + 2 analog давали 7 запросов).
    Здесь проверка и учёт — одно синхронное действие без `await`: в одном
    потоке событий между ними вклиниться некому, замок не нужен. Он и вреден:
    платный запрос с опросом операции длится до минуты, и замок через сеть
    выстроил бы их гуськом.

    Учитывается зарезервированное место: запрос, оборванный транспортом, место
    не возвращает — для денег это безопасная сторона. Отказ источника остаётся
    `SearchError` (инвариант №5), а исчерпанный предел им не является.

РЕЗЕРВ ПОД АНАЛОГИ
    Формулировки `analog` стоят в хвосте плана и без резерва вытеснялись бы
    основной очередью целиком. Проход, объявленный через `begin_pass`, делит
    остаток предела на подпулы:
        мировые `analog` (английские)  — `ANALOG_WORLD_SLOTS`, если они есть в проходе;
        реестровые `analog` (русские)  — `ANALOG_REGISTRY_SLOTS`, если они есть;
        прочие (web, contest, grant)   — остаток минус резерв, не меньше нуля.
    Резерв, которого в проходе нет, уходит прочим. Резерв ключуется наличием
    `analog` в проходе, а не видом решения: при «Науке» `analog` снимает код
    (`normalize_analog_queries`), значит резерв нулевой сам собой.

    Мировые места делятся «в ширину»: каждая английская формулировка получает по
    пачке, прежде чем какая-то получит вторую. Раздача считается заранее, в
    `begin_pass`, а не по порядку прихода ответов — иначе исход зависел бы от
    скорости сети.

    Без `begin_pass` (прямой вызов адаптера, `scout doctor`) — один общий пул.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum

__all__ = [
    "ANALOG_REGISTRY_SLOTS",
    "ANALOG_WORLD_SLOTS",
    "AnalogKey",
    "Verdict",
    "YandexQuota",
    "split_width",
]

#: Мест под мировые (английские) формулировки `analog` в проходе, где они есть
ANALOG_WORLD_SLOTS = 2
#: Мест под реестровую (русскую, Э6-8) формулировку `analog` в проходе, где она есть
ANALOG_REGISTRY_SLOTS = 1

#: Ключ формулировки `analog` в раздаче мест: текст и язык
AnalogKey = tuple[str, str]


class Verdict(Enum):
    """Исход попытки занять место."""

    #: Место занято, запрос можно отправлять
    GRANTED = "granted"
    #: Предел на прогон исчерпан целиком
    LIMIT = "limit"
    #: Предел не исчерпан, но места прочих типов оставлены аналогам
    RESERVED = "reserved"
    #: Предел не исчерпан, но места самих аналогов заняты
    ANALOG_BUSY = "analog_busy"


def split_width(slots: int, demands: Sequence[int]) -> list[int]:
    """Раздать `slots` мест «в ширину»: по кругу, по одному, пока есть и места, и нужда.

    `demands[i]` — сколько пачек у i-й формулировки. Круг глубины `d` касается
    только тех, у кого пачек больше `d`, поэтому вторая пачка не достаётся
    никому, пока у каждой формулировки не будет первой.
    """
    granted = [0] * len(demands)
    left = slots
    for depth in range(max(demands, default=0)):
        for index, demand in enumerate(demands):
            if left <= 0:
                return granted
            if demand > depth:
                granted[index] += 1
                left -= 1
    return granted


class YandexQuota:
    """Счёт платных запросов прогона и раздача мест внутри прохода."""

    def __init__(self, max_queries: int) -> None:
        self.max_queries = max_queries
        #: Сколько мест занято за прогон; не сбрасывается между проходами
        self.used = 0
        self._declared = False
        self._other_left = 0
        self._has_reserve = False
        self._analog_left: dict[AnalogKey, int] = {}

    def begin_pass(self, world: Sequence[tuple[AnalogKey, int]], registry: Sequence[tuple[AnalogKey, int]]) -> None:
        """Объявить проход: мировые и реестровые `analog` с числом их пачек.

        `used` не трогается — предел действует на весь прогон, а не на проход.
        """
        world_slots = ANALOG_WORLD_SLOTS if world else 0
        registry_slots = ANALOG_REGISTRY_SLOTS if registry else 0
        reserve = world_slots + registry_slots
        self._declared = True
        self._has_reserve = reserve > 0
        self._other_left = max(0, self.max_queries - self.used - reserve)
        self._analog_left = {}
        for slots, group in ((world_slots, world), (registry_slots, registry)):
            granted = split_width(slots, [batches for _, batches in group])
            for (key, _), count in zip(group, granted, strict=True):
                self._analog_left[key] = self._analog_left.get(key, 0) + count

    def reserve_other(self) -> Verdict:
        """Занять место прочей формулировке (web, contest, grant). Синхронно."""
        if self.used >= self.max_queries:
            return Verdict.LIMIT
        if self._declared:
            if self._other_left <= 0:
                return Verdict.RESERVED if self._has_reserve else Verdict.LIMIT
            self._other_left -= 1
        self.used += 1
        return Verdict.GRANTED

    def reserve_analog(self, key: AnalogKey) -> Verdict:
        """Занять место пачке `analog`. Синхронно."""
        if self.used >= self.max_queries:
            return Verdict.LIMIT
        if self._declared:
            if self._analog_left.get(key, 0) <= 0:
                return Verdict.ANALOG_BUSY
            self._analog_left[key] -= 1
        self.used += 1
        return Verdict.GRANTED
