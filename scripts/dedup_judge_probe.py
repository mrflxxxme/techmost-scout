"""Проба: семантический судья дублей на дословных заголовках Э2-Г (Д-6 Э2-З, РЕШ-152).

ЗАЧЕМ
    РЕШ-152 измерил числами, что лексическое сходство заголовков дубли не
    разделяет (`.planning/evidence/E2G-З6-similarity.md`): пара, которую
    воротный отчёт фармы сам признал одной разработкой (карточки 3–4), даёт
    сходство 0.182 — третье с конца значение во всей таблице; пара разных
    разработок на одну тему (карточки 1–5) даёт 0.571 — максимум. У нефтегаза
    диапазоны настоящих дублей (группа 2-3-4-5) и контрольных недублей
    (карточки 1 и 6) перекрываются без разделяющего порога.

    Прежде чем подключать судью к пайплайну (РЕШ-133 — сначала дешёвая проба,
    это уже дважды окупилось на этом подэтапе), эта проба прогоняет ровно тот
    же промпт `prompts/dedup_judge.md` на тех же дословных заголовках и
    печатает точность против готовой ручной разметки обоих отчётов.

    Заголовки — ДОСЛОВНО из `evidence/E2G-З6-similarity.md`. Сниппетов и
    организаций в этом файле нет (это список одних заголовков, посчитанных
    признаком `_title_similarity`), поэтому проба подаёт судье заголовки БЕЗ
    вспомогательных полей — более трудный случай, чем в бою, где сниппет и
    организация обычно есть.

РАЗМЕТКА (дословно из постановки задачи Д-6, PLAN.md)
    Фарма (5 заголовков, `E2V-gate-фарма.md`): пара 3–4 — дубль (отчёт сам
    признал их одной разработкой), пара 1–5 — НЕ дубль (общая тема, разные
    работы, самый трудный контрольный случай признака).
    Нефтегаз (6 заголовков, `E2B-gate-нефтегаз.md`): группа 2-3-4-5 — дубли
    (одна технология, четыре формулировки), карточки 1 и 6 — контрольные
    недубли (патент на другой мембране и керамический модуль с CFD).
    Остальные пары (не названные явно в постановке) размечены как недубли —
    только пара 3–4 фармы и группа 2-3-4-5 нефтегаза признаны отчётами
    одной разработкой.

ГРАНИЦЫ (РЕШ-119, инвариант №7 для агента)
    Скрипт живёт ВНЕ поставки продукта: `backend/app` его не импортирует,
    подкоманды `scout dedup-judge-probe` нет и не будет. Исполнитель задачи
    запускает пробу ТОЛЬКО с `--dry-run` (без сети и без оплаты) — живой
    прогон делает оркестратор (~0,1–0,3 ₽, см. `.planning/PLAN.md`).
    Вызов модели — исключительно через `app.llm.LLMRouter` и стадию
    `app.pipeline.dedup_judge.judge_duplicate_groups`, тот же путь, которым
    идёт продукт (инвариант №6). Ключи берутся из окружения тем же способом,
    что и в продукте (`app.config.get_settings`), и нигде не печатаются.

ЗАПУСК
    .venv/bin/python scripts/dedup_judge_probe.py --help
    .venv/bin/python scripts/dedup_judge_probe.py --dry-run
    .venv/bin/python scripts/dedup_judge_probe.py
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import sys
from dataclasses import dataclass

#: Фарма — дословно из `evidence/E2G-З6-similarity.md`, раздел «Фарма»,
#: пронумеровано так же (1–5), как в исходной таблице сходства.
PHARMA_TITLES: list[str] = [
    "ИИ-система определения беременности свиноматок по УЗИ",
    "ИИ-система анализа ультразвуковых исследований беременности свиноматок",
    "ИИ-система диагностики супоросности свиноматок по ультразвуковым изображениям",
    "ИИ-диагностика супоросности свиноматок по УЗИ-изображениям (ViT-H, ConvNeXt, Xception)",
    "ИИ-система для анализа УЗИ свиноматок и определения супоросности",
]
#: Единственная пара, названная постановкой дублем: карточки 3–4 отчёта Э2-В.
PHARMA_TRUE_DUP_PAIRS: frozenset[frozenset[int]] = frozenset({frozenset({3, 4})})

#: Нефтегаз — дословно из `evidence/E2G-З6-similarity.md`, раздел «Нефтегаз».
OILGAS_TITLES: list[str] = [
    "Внутрискважинный сепаратор нефти и воды на водоселективной мембране (US7823635B2)",
    "Мембранная внутрискважинная система разделения нефти и воды (DOWS) на основе PTFE-мембран",
    "Мембранная сепарация нефти и воды в скважине (Downhole Separation of Petroleum Fluids)",
    "Мембранная внутрискважинная сепарация нефти и воды (Membrane-based downhole oil–water separation, DOWS)",
    "Мембранная внутрискважинная сепарация нефти и воды (DOWS)",
    "Мембранный модуль для разделения нефти и воды на основе керамической мембраны (CFD-моделирование)",
]
#: Все пары внутри группы 2-3-4-5 — постановка называет всю группу дублями
#: одной технологии; 1 и 6 контрольные недубли ни с чем не образуют пары.
OILGAS_TRUE_DUP_PAIRS: frozenset[frozenset[int]] = frozenset(
    frozenset(pair) for pair in itertools.combinations((2, 3, 4, 5), 2)
)


@dataclass(frozen=True)
class DatasetResult:
    """Итог одного набора: точность против разметки плюс сырые счётчики."""

    name: str
    n: int
    groups: list[list[int]] | None
    true_positive: set[frozenset[int]]
    false_positive: set[frozenset[int]]
    false_negative: set[frozenset[int]]
    true_negative: set[frozenset[int]]


def all_pairs(n: int) -> list[frozenset[int]]:
    """Все пары номеров 1..n — знаменатель точности."""
    return [frozenset(pair) for pair in itertools.combinations(range(1, n + 1), 2)]


def judged_pairs(groups: list[list[int]]) -> set[frozenset[int]]:
    """Развернуть группы судьи в пары: пара «дубль», если оба номера попали
    в одну и ту же группу вердикта."""
    pairs: set[frozenset[int]] = set()
    for group in groups:
        pairs |= {frozenset(pair) for pair in itertools.combinations(sorted(group), 2)}
    return pairs


def score(name: str, n: int, true_dup: frozenset[frozenset[int]], groups: list[list[int]] | None) -> DatasetResult:
    """Сверить вердикт судьи с разметкой по всем парам набора.

    `groups is None` (судья отказал) считается «дублей не найдено» —
    честный худший случай для точности, а не исключение из подсчёта: проба
    обязана показать цену отказа, а не спрятать её.
    """
    predicted = judged_pairs(groups) if groups else set()
    every_pair = set(all_pairs(n))
    tp = predicted & true_dup
    fp = predicted - true_dup
    fn = true_dup - predicted
    tn = every_pair - predicted - true_dup
    return DatasetResult(
        name=name, n=n, groups=groups, true_positive=tp, false_positive=fp, false_negative=fn, true_negative=tn
    )


def _fmt_pairs(pairs: set[frozenset[int]]) -> str:
    if not pairs:
        return "нет"
    return ", ".join("-".join(map(str, sorted(pair))) for pair in sorted(pairs, key=lambda p: sorted(p)))


def print_dataset_report(titles: list[str], result: DatasetResult) -> None:
    total = (
        len(result.true_positive) + len(result.false_positive) + len(result.false_negative) + len(result.true_negative)
    )
    correct = len(result.true_positive) + len(result.true_negative)
    print(f"=== {result.name} ({result.n} заголовков, {total} пар) ===")
    for i, title in enumerate(titles, start=1):
        print(f"  {i}. {title}")
    if result.groups is None:
        print("  судья отказал (деградация) — вердикт считается пустым списком групп")
    else:
        print(f"  группы судьи: {result.groups or 'дублей не найдено'}")
    print(f"  верно найдено дублей (TP): {_fmt_pairs(result.true_positive)}")
    print(f"  ложно найдено дублей (FP): {_fmt_pairs(result.false_positive)}")
    print(f"  пропущено дублей (FN): {_fmt_pairs(result.false_negative)}")
    print(f"  верно НЕ дубли (TN): {len(result.true_negative)} пар")
    print(f"  точность по парам набора: {correct}/{total} = {correct / total:.2%}")
    print()


def print_dry_run() -> None:
    """Сухой прогон: печатает оба набора и разметку, модель не зовёт."""
    print("=== СУХОЙ ПРОГОН (ничего не оплачено) ===")
    for name, titles, true_dup in (
        ("Фарма", PHARMA_TITLES, PHARMA_TRUE_DUP_PAIRS),
        ("Нефтегаз", OILGAS_TITLES, OILGAS_TRUE_DUP_PAIRS),
    ):
        print(
            f"{name}: {len(titles)} заголовков, {len(all_pairs(len(titles)))} пар, "
            f"{len(true_dup)} размечены дублями: {_fmt_pairs(set(true_dup))}"
        )
        for i, title in enumerate(titles, start=1):
            print(f"  {i}. {title}")


def to_candidates(titles: list[str]):
    """Заголовки → `ScoredCandidate` без сниппета и организации (см. докстроку
    модуля: в evidence их нет — судья видит более трудный случай, чем в бою)."""
    from app.schemas import Candidate, ScoredCandidate, SourceType

    return [
        ScoredCandidate(
            candidate=Candidate(
                source_type=SourceType.PUBLICATION,
                title=title,
                url=f"https://example.org/probe/{i}",
                provider="probe",
            ),
            relevance=8,
            maturity=5,
            accessibility=5,
            freshness=5,
            completeness=5,
        )
        for i, title in enumerate(titles, start=1)
    ]


async def run_live(router, titles: list[str]) -> list[list[int]] | None:
    """Один вызов судьи на набор заголовков — тот же путь, которым идёт бой
    (`app.pipeline.dedup_judge.judge_duplicate_groups`), без обхода роутера."""
    from app.pipeline.dedup_judge import judge_duplicate_groups

    return await judge_duplicate_groups(router, to_candidates(titles))


async def _main_async(args: argparse.Namespace) -> int:
    if args.dry_run:
        print_dry_run()
        return 0

    from app.config import get_settings
    from app.llm import LLMRouter

    router = LLMRouter(get_settings())
    results = []
    for name, titles, true_dup in (
        ("Фарма", PHARMA_TITLES, PHARMA_TRUE_DUP_PAIRS),
        ("Нефтегаз", OILGAS_TITLES, OILGAS_TRUE_DUP_PAIRS),
    ):
        print(f"судья дублей: {name}...", file=sys.stderr)
        groups = await run_live(router, titles)
        result = score(name, len(titles), true_dup, groups)
        print_dataset_report(titles, result)
        results.append(result)

    total_correct = sum(len(r.true_positive) + len(r.true_negative) for r in results)
    total_pairs = sum(
        len(r.true_positive) + len(r.false_positive) + len(r.false_negative) + len(r.true_negative) for r in results
    )
    print("=== ИТОГ ===")
    print(f"точность по всем парам обоих наборов: {total_correct}/{total_pairs} = {total_correct / total_pairs:.2%}")
    print(f"стоимость пробы: {router.cost.spent_rub:.4f} ₽ (вызовов: {router.cost.calls})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dedup_judge_probe.py",
        description=(
            "Проба судьи дублей на дословных заголовках Э2-Г с готовой ручной разметкой "
            "(РЕШ-133, РЕШ-152). Скрипт вне поставки продукта."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Ничего не оплачивать: напечатать оба набора заголовков и разметку без вызова модели.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
