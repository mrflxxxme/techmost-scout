"""Проба: ловятся ли дубли без общих метаданных пересечением словесных шинглов.

Задача З-2 (Э2-Д). Долг измерен ценой на прогоне Э2-Г: дубль О-2/О-4
(журнальная страница JAST 65(2) и её зеркало на ResearchGate) дошёл до
клиента двумя карточками из четырёх — заголовки не пересекались вовсе,
общих метаданных не было, а общий дословный кусок сниппетов составил
311 знаков при Жаккаре по словам 0,589 (аудит Э2-Г, `E2G-audit.md`, совет С-3).

Скрипт меряет признак «пересечение словесных шинглов сниппета» на реальных
данных прогона (запрос №1 в `data/scout.db`, 112 кандидатов) и на текстах
воротных карточек фармы и нефтегаза — без единого сетевого вызова и без
записи в базу. Это ЗАМЕР по РЕШ-133: код признака в продукт эта проба не
вносит, вносит только числа для решения «брать/не брать» (см. отчёт
`.planning/evidence/E2D-probe-shingles.md`).

Режимы работы:
  --db PATH --request N   — попарный анализ кандидатов запроса N из базы
                            (только чтение, sqlite URI `?mode=ro`);
  --gate-report PATH       — попарный анализ описательных абзацев карточек
                            воротного markdown-отчёта (второй набор данных,
                            п. 4 постановки: результат идёт наблюдением,
                            а не порогом, потому что сниппетов этого прогона
                            в базе нет).

Оба режима используют одну и ту же меру: индекс Жаккара множеств словесных
шинглов длины `--k` плюс отдельной колонкой — длина самого длинного общего
дословного куска (`difflib.SequenceMatcher`, без нормализации регистра —
сверяем то, что реально совпало дословно).

Запуск:
    .venv/bin/python scripts/shingle_probe.py --db data/scout.db --request 1 --k 5 --min 0.05
    .venv/bin/python scripts/shingle_probe.py --gate-report .planning/evidence/E2V-gate-фарма.md --k 3 --min 0.0
"""

from __future__ import annotations

import argparse
import itertools
import re
import sqlite3
import statistics
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

#: Словесный токен: буквенно-цифровая последовательность с дефисом внутри.
#: Дефис не отбрасываем — тот же выбор, что и в `app.pipeline.diversity`,
#: иначе «ViT-H» и «низкочастотного» рвутся на куски и шинглы перестают
#: совпадать там, где по смыслу должны.
_WORD_RE = re.compile(r"[\w\-]+", re.UNICODE)

#: Заголовок карточки воротного отчёта: «### 3. Название разработки».
_CARD_HEADER_RE = re.compile(r"^### (\d+)\.\s*(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class Item:
    """Единица сравнения: кандидат из базы или карточка воротного отчёта.

    Поле `text` — то, по чему считаются шинглы (сниппет кандидата либо
    описательный абзац карточки), поле `title`/`url` — только для печати.
    """

    id: int
    title: str
    url: str
    text: str


@dataclass(frozen=True)
class PairResult:
    """Результат сравнения пары: обе меры и сами элементы."""

    a: Item
    b: Item
    jaccard: float
    common_run: int


def _words(text: str) -> list[str]:
    """Токены нормализованного текста: нижний регистр, без пунктуации."""
    return _WORD_RE.findall((text or "").lower())


def word_shingles(text: str, k: int) -> set[tuple[str, ...]]:
    """Множество словесных шинглов длины `k`.

    Текст короче `k` слов даёт пустое множество: шингл длины `k` из него не
    собрать физически, а не потому что сходства нет.
    """
    words = _words(text)
    if len(words) < k:
        return set()
    return {tuple(words[i : i + k]) for i in range(len(words) - k + 1)}


def jaccard(a: set, b: set) -> float:
    """Индекс Жаккара двух множеств шинглов.

    Оба множества пустые — результат 0, а не 1: пустое ни с чем не
    совпадает, иначе два кандидата с обрезанными до `k-1` слова сниппетами
    считались бы полными дублями просто по факту одинаковой нехватки данных.
    """
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def longest_common_run(a: str, b: str) -> int:
    """Длина самого длинного общего дословного куска.

    Считается на исходном тексте, без приведения регистра: нас интересует,
    сколько символов совпало буквально, а не после нормализации — так же,
    как в независимом замере аудита Э2-Г (сверено числом 311 на паре О-2/О-4,
    см. отчёт).
    """
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    match = matcher.find_longest_match(0, len(a), 0, len(b))
    return match.size


def load_db_candidates(db_path: str, request_id: int) -> list[Item]:
    """Читает кандидатов запроса из базы в режиме только для чтения.

    `mode=ro` в URI — гарантия на уровне sqlite, а не соглашение: попытка
    записи в такое соединение падает ошибкой, а не проходит молча.
    """
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT id, title, url, snippet FROM candidates WHERE request_id = ? ORDER BY id",
            (request_id,),
        )
        return [Item(id=row[0], title=row[1], url=row[2], text=row[3]) for row in cur.fetchall()]
    finally:
        con.close()


def _first_prose_paragraph(body: str) -> str:
    """Первый абзац карточки, не являющийся служебной строкой `**Поле:**`.

    Воротные карточки построены по шаблону: заголовок, `**Стадия
    зрелости:**`, затем свободный описательный абзац, затем снова служебные
    поля («Разработчик», «Сверка с требованиями» и т.д.). Служебные поля
    почти дословно повторяются во всех карточках отчёта («не подтверждено
    источником», «уровень готовности от TRL 6 до TRL 9») — сравнивать по ним
    означало бы мерить шаблон, а не содержание. Описательный абзац —
    единственная часть карточки, аналогичная сниппету источника.
    """
    for para in body.split("\n\n"):
        para = para.strip()
        if para and not para.startswith("**") and not para.startswith("-"):
            return para
    return ""


def load_gate_cards(path: Path) -> list[Item]:
    """Разбирает воротный markdown-отчёт на карточки решений.

    Возвращает по одному элементу на карточку с текстом — её описательным
    абзацем (см. `_first_prose_paragraph`), не всей структурой карточки.
    """
    text = path.read_text(encoding="utf-8")
    matches = list(_CARD_HEADER_RE.finditer(text))
    items = []
    for i, m in enumerate(matches):
        num = int(m.group(1))
        title = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end]
        paragraph = _first_prose_paragraph(body)
        items.append(Item(id=num, title=title, url="", text=paragraph))
    return items


def pairwise(items: list[Item], k: int) -> list[PairResult]:
    """Считает обе меры для всех пар элементов."""
    shingles = {item.id: word_shingles(item.text, k) for item in items}
    results = []
    for a, b in itertools.combinations(items, 2):
        j = jaccard(shingles[a.id], shingles[b.id])
        run = longest_common_run(a.text, b.text)
        results.append(PairResult(a=a, b=b, jaccard=j, common_run=run))
    return results


def percentile(sorted_values: list[float], p: float) -> float:
    """Процентиль по отсортированному списку без внешних зависимостей.

    Линейная интерполяция между соседними значениями — тот же метод, что
    умолчание `numpy.percentile`, но без numpy: в проекте его нет и он не
    нужен ради одного числа.
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * (p / 100)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    if lower == upper:
        return sorted_values[lower]
    frac = rank - lower
    return sorted_values[lower] * (1 - frac) + sorted_values[upper] * frac


def print_report(items: list[Item], results: list[PairResult], min_value: float, k: int) -> None:
    """Печатает сводку: счётчики, распределение, пары выше порога."""
    print(f"Кандидатов: {len(items)}")
    print(f"Пар: {len(results)}")
    print(f"k (длина шингла): {k}")

    values = sorted(r.jaccard for r in results)
    if values:
        print(
            "Жаккар по шинглам — мин/медиана/макс: "
            f"{values[0]:.4f} / {statistics.median(values):.4f} / {values[-1]:.4f}"
        )
        for p in (75, 90, 95, 99, 99.5, 99.9):
            print(f"  перцентиль {p}: {percentile(values, p):.4f}")
    else:
        print("Пар для сравнения нет.")

    above = sorted((r for r in results if r.jaccard >= min_value), key=lambda r: r.jaccard, reverse=True)
    print(f"\nПар с Жаккаром >= {min_value}: {len(above)}")
    for r in above:
        print(
            f"  {r.jaccard:.4f}  (общий кусок {r.common_run} зн.)  "
            f"[{r.a.id}] {r.a.title!r} ({r.a.url}) <-> "
            f"[{r.b.id}] {r.b.title!r} ({r.b.url})"
        )


def build_parser() -> argparse.ArgumentParser:
    """Собирает парсер аргументов командной строки."""
    parser = argparse.ArgumentParser(
        description=(
            "Проба: попарный признак «пересечение словесных шинглов сниппета» "
            "на кандидатах прогона или на карточках воротного отчёта. "
            "Ничего не пишет и не требует сети — только чтение."
        )
    )
    parser.add_argument(
        "--db",
        default="data/scout.db",
        help="Путь к базе SQLite (открывается только на чтение, ?mode=ro). Игнорируется при заданном --gate-report.",
    )
    parser.add_argument(
        "--request",
        type=int,
        default=1,
        help="Номер запроса (request_id) в базе, чьи кандидаты сравниваются попарно.",
    )
    parser.add_argument(
        "--gate-report",
        default=None,
        help="Путь к воротному markdown-отчёту вместо базы: сравнивает описательные "
        "абзацы карточек «### N. Название» (второй набор данных пробы).",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=5,
        help="Длина словесного шингла в словах.",
    )
    parser.add_argument(
        "--min",
        dest="min_value",
        type=float,
        default=0.05,
        help="Порог печати: показываются все пары с Жаккаром не ниже этого значения.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Точка входа: разбирает аргументы, считает признак, печатает отчёт."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.gate_report is not None:
        items = load_gate_cards(Path(args.gate_report))
    else:
        items = load_db_candidates(args.db, args.request)

    if len(items) < 2:
        print("Меньше двух элементов для сравнения — сравнивать нечего.", file=sys.stderr)
        return 1

    results = pairwise(items, args.k)
    print_report(items, results, args.min_value, args.k)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
