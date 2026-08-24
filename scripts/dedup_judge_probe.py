"""Проба: семантический судья дублей на карточках воротных отчётов Э2-Г (Д-6 Э2-З, РЕШ-152).

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
    же промпт `prompts/dedup_judge.md` на тех же кандидатах и печатает
    точность против готовой ручной разметки обоих отчётов.

    ЦИКЛ 1 РЕВЬЮ (`E2Z-Д6-review.md`): первая редакция подавала судье ГОЛЫЕ
    заголовки (`org=None`, `snippet=""`) — вход беднее боевого: боевая стадия
    (`dedup_judge._listing`) всегда показывает судье организацию и фрагмент
    источника, если они есть у кандидата. Живой прогон на голых заголовках
    (фарма 10%, вся выдача склеена в одну группу) не отличал «промпт слаб на
    трудном случае» от «судье не хватило единственного доступного
    дифференцирующего признака». Эта редакция берёт `org`/`snippet` из
    ВОРОТНЫХ карточек `E2V-gate-фарма.md` и `E2B-gate-нефтегаз.md` (номер
    исходной строки — в комментарии рядом с каждым `ProbeItem` ниже): `org`
    — дословно поле «Разработчик» карточки БЕЗ хвоста «— не найдено» (сам хвост — про
    контактную команду, не про факт наличия организации); где «Разработчик:
    не найдено» целиком — `org=None`, ЧЕСТНО, а не заполнено на глаз (это тот
    же случай, что у реального кандидата без организации в источнике).
    `snippet` — первый содержательный абзац карточки (после «Стадия
    зрелости:»): не настоящий сниппет поисковой выдачи (воротный отчёт его не
    хранит), а его текстовое приближение, что и названо явно, а не выдаётся
    за настоящий сниппет.

РАЗМЕТКА (дословно из постановки задачи Д-6, PLAN.md)
    Фарма (5 карточек, `E2V-gate-фарма.md`): пара 3–4 — дубль (отчёт сам
    признал их одной разработкой), пара 1–5 — НЕ дубль (общая тема, разные
    работы, самый трудный контрольный случай признака).
    Нефтегаз (6 карточек, `E2B-gate-нефтегаз.md`): группа 2-3-4-5 — дубли
    (одна технология, четыре формулировки), карточки 1 и 6 — контрольные
    недубли (патент на другой мембране и керамический модуль с CFD).
    Остальные пары (не названные явно в постановке) размечены как недубли —
    только пара 3–4 фармы и группа 2-3-4-5 нефтегаза признаны отчётами
    одной разработкой.

ГРАНИЦЫ (РЕШ-119, инвариант №7 для агента)
    Скрипт живёт ВНЕ поставки продукта: `backend/app` его не импортирует,
    подкоманды `scout dedup-judge-probe` нет и не будет. Исполнитель задачи
    запускает пробу ТОЛЬКО с `--dry-run` (без сети и без оплаты) — живой
    прогон делает оркестратор (~0,15 ₽, см. `.planning/PLAN.md`).
    Вызов модели — исключительно через `app.llm.LLMRouter` и стадию
    `app.pipeline.dedup_judge.judge_duplicate_groups`, тот же путь, которым
    идёт продукт (инвариант №6). Ключи берутся из окружения тем же способом,
    что и в продукте (`app.config.get_settings`), и нигде не печатаются.

    ПРОМПТ `prompts/dedup_judge.md` ЭТА ПРАВКА НЕ ТРОГАЕТ (по решению
    ревью, цикл 1): правило про организацию и калибровка правила 6 ждут
    результата повторного живого прогона на обогащённом составе полей —
    иначе промпт и проба правились бы одновременно, и по числам ретеста было
    бы невозможно понять, что именно изменило результат.

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


@dataclass(frozen=True)
class ProbeItem:
    """Один кандидат пробы: дословный заголовок карточки воротного отчёта
    плюс `org`/`snippet` оттуда же (см. ЦИКЛ 1 РЕВЬЮ в докстроке модуля)."""

    number: int
    title: str
    org: str | None
    snippet: str


#: Фарма — карточки `E2V-gate-фарма.md`, пронумеровано так же (1–5), как в
#: исходной таблице сходства `E2G-З6-similarity.md`. `org`/`snippet` — поля
#: «Разработчик» и первый абзац каждой карточки (строки указаны в комментарии).
PHARMA_ITEMS: list[ProbeItem] = [
    ProbeItem(  # :33-39
        1,
        "ИИ-система определения беременности свиноматок по УЗИ",
        "RDA",
        "Исследователи RDA разработали программу для определения супоросности свиноматок на основе "
        "ультразвуковых изображений. Для обучения алгоритма ИИ использованы ультразвуковые данные матки "
        "высокого разрешения с частотой 5 МГц. Система определяет статус беременности после захвата "
        "ультразвуковых изображений живота свиноматки в течение не менее 10 секунд. Программа "
        "позиционируется как удобная для использования на ферме.",
    ),
    ProbeItem(  # :72-78
        2,
        "ИИ-система анализа ультразвуковых исследований беременности свиноматок",
        None,
        "В источнике описан подход, основанный на интеграции искусственного интеллекта с ультразвуковым "
        "биосенсингом. Разрабатывается управляемая данными модель глубокого обучения для устранения "
        "акустических артефактов в B-режиме ультразвуковой визуализации, специально для диагностики "
        "беременности свиноматок. По мнению авторов, такой подход может повысить точность диагностики в "
        "сельскохозяйственных и биомедицинских приложениях.",
    ),
    ProbeItem(  # :103-109
        3,
        "ИИ-система диагностики супоросности свиноматок по ультразвуковым изображениям",
        "Department of Data Science, Kangwon National University, Chuncheon 24341, Republic of Korea",
        "Разработка представляет собой метод диагностики супоросности свиноматок с использованием "
        "глубокого обучения для анализа изображений низкочастотного ультразвука. В исследовании "
        "сравниваются три модели классификации: ViT-H, ConvNeXt-xlarge и Xception. Для расширения данных "
        "применяется AutoAugment.",
    ),
    ProbeItem(  # :137-143
        4,
        "ИИ-диагностика супоросности свиноматок по УЗИ-изображениям (ViT-H, ConvNeXt, Xception)",
        None,
        "Разработка представляет подход к диагностике супоросности свиноматок с применением глубокого "
        "обучения и анализа низкочастотных ультразвуковых изображений. Созданы и сравниваются три модели "
        "классификации: ViT-H, ConvNeXt-xlarge и Xception, целью которых является повышение точности "
        "диагностики. Данных о достигнутой точности и стадии готовности в источнике не приводится.",
    ),
    ProbeItem(  # :170-176
        5,
        "ИИ-система для анализа УЗИ свиноматок и определения супоросности",
        None,
        "Предложен подход на основе глубокого обучения для анализа ультразвуковых изображений свиноматок "
        "с целью повышения точности определения супоросности. В исследовании протестированы три модели "
        "глубокого обучения; по описанию источника они дают значительно лучшие результаты по сравнению с "
        "традиционной технологией. Конкретные значения точности и стадия готовности в источнике не "
        "приведены.",
    ),
]
#: Единственная пара, названная постановкой дублем: карточки 3–4 отчёта Э2-В.
PHARMA_TRUE_DUP_PAIRS: frozenset[frozenset[int]] = frozenset({frozenset({3, 4})})

#: Нефтегаз — карточки `E2B-gate-нефтегаз.md`, та же нумерация, что в
#: `E2G-З6-similarity.md`.
OILGAS_ITEMS: list[ProbeItem] = [
    ProbeItem(  # :34-40
        1,
        "Внутрискважинный сепаратор нефти и воды на водоселективной мембране (US7823635B2)",
        None,
        "Технология разделяет нефть и воду непосредственно в скважине с помощью водоселективной мембраны. "
        "В одном из вариантов для подъёма продукции используется погружной насос. При прохождении "
        "продукции через сепаратор вода удаляется через мембрану, образуя отдельные потоки воды и нефти.",
    ),
    ProbeItem(  # :76-82
        2,
        "Мембранная внутрискважинная система разделения нефти и воды (DOWS) на основе PTFE-мембран",
        None,
        "Технология внутрискважинного мембранного разделения нефти и воды (DOWS) на основе PTFE-мембран "
        "как альтернатива гидроциклонным системам. По данным источника, PTFE-мембраны достигают почти 99% "
        "эффективности разделения по сравнению с 75-85% для гидроциклонных систем. Стоимость установки "
        "погружного электронасоса DOWS оценивается от $90 000 до $250 000, тогда как гравитационные "
        "сепараторы стоят от $15 000 до $25 000.",
    ),
    ProbeItem(  # :114-120
        3,
        "Мембранная сепарация нефти и воды в скважине (Downhole Separation of Petroleum Fluids)",
        None,
        "Статья описывает мембранную технологию для внутрискважинного разделения нефти и воды, отмечая "
        "недостатки существующих технологий. Полимерные мембраны, применявшиеся ранее, создают высокий "
        "перепад давления и дороги. Авторы идентифицируют новый материал, который пропускает нефть, но не "
        "пропускает воду. Данные о конструкции, размерах и технологической зрелости в источнике не "
        "приводятся.",
    ),
    ProbeItem(  # :161-167
        4,
        "Мембранная внутрискважинная сепарация нефти и воды (Membrane-based downhole oil–water separation, DOWS)",
        None,
        "Рассматривается мембранная технология внутрискважинной сепарации нефти и воды (DOWS) как "
        "альтернатива гидроциклонным системам. В источнике упоминается, что разделение происходит "
        "непосредственно в скважине. Конкретные технические параметры установки в источнике не приведены.",
    ),
    ProbeItem(  # :204-210
        5,
        "Мембранная внутрискважинная сепарация нефти и воды (DOWS)",
        None,
        "Технология мембранной внутрискважинной сепарации нефти и воды (DOWS) рассматривается как "
        "альтернатива гидроциклонной DOWS. Направлена на снижение затрат на подъём и обработку пластовой "
        "воды за счёт отделения воды в скважине. Подробные характеристики установки в источнике не "
        "приведены.",
    ),
    ProbeItem(  # :249-255
        6,
        "Мембранный модуль для разделения нефти и воды на основе керамической мембраны (CFD-моделирование)",
        "Instituto Federal do Piauí",
        "Исследован процесс разделения обводнённой воды и нефти в модуле с пористой керамической "
        "мембраной. С помощью CFD-моделирования (подход Эйлера–Эйлера, модель турбулентности SST k-ω в "
        "Ansys Fluent 15.0) оценено гидродинамическое поведение смеси при различных массовых расходах и "
        "концентрациях нефти. Работа выполнена методами компьютерного моделирования, экспериментального "
        "подтверждения разделения в источнике нет.",
    ),
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


def print_dataset_report(items: list[ProbeItem], result: DatasetResult) -> None:
    total = (
        len(result.true_positive) + len(result.false_positive) + len(result.false_negative) + len(result.true_negative)
    )
    correct = len(result.true_positive) + len(result.true_negative)
    print(f"=== {result.name} ({result.n} карточек, {total} пар) ===")
    for item in items:
        print(f"  {item.number}. {item.title}")
        print(f"     org: {item.org or 'не найдено'}")
        print(f"     snippet: {item.snippet}")
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
    """Сухой прогон: печатает оба набора (заголовок, org, snippet) и разметку, модель не зовёт."""
    print("=== СУХОЙ ПРОГОН (ничего не оплачено) ===")
    for name, items, true_dup in (
        ("Фарма", PHARMA_ITEMS, PHARMA_TRUE_DUP_PAIRS),
        ("Нефтегаз", OILGAS_ITEMS, OILGAS_TRUE_DUP_PAIRS),
    ):
        print(
            f"{name}: {len(items)} карточек, {len(all_pairs(len(items)))} пар, "
            f"{len(true_dup)} размечены дублями: {_fmt_pairs(set(true_dup))}"
        )
        for item in items:
            print(f"  {item.number}. {item.title}")
            print(f"     org: {item.org or 'не найдено'}")
            print(f"     snippet: {item.snippet}")


def to_candidates(items: list[ProbeItem]):
    """Кандидаты пробы → `ScoredCandidate` с `org`/`snippet` из воротных
    карточек (см. докстроку модуля, ЦИКЛ 1 РЕВЬЮ) — тот же состав полей,
    что видит боевая стадия через `dedup_judge._listing`."""
    from app.schemas import Candidate, ScoredCandidate, SourceType

    return [
        ScoredCandidate(
            candidate=Candidate(
                source_type=SourceType.PUBLICATION,
                title=item.title,
                url=f"https://example.org/probe/{item.number}",
                org=item.org,
                snippet=item.snippet,
                provider="probe",
            ),
            relevance=8,
            maturity=5,
            accessibility=5,
            freshness=5,
            completeness=5,
        )
        for item in items
    ]


async def run_live(router, items: list[ProbeItem]) -> list[list[int]] | None:
    """Один вызов судьи на набор кандидатов — тот же путь, которым идёт бой
    (`app.pipeline.dedup_judge.judge_duplicate_groups`), без обхода роутера."""
    from app.pipeline.dedup_judge import judge_duplicate_groups

    return await judge_duplicate_groups(router, to_candidates(items))


async def _main_async(args: argparse.Namespace) -> int:
    if args.dry_run:
        print_dry_run()
        return 0

    from app.config import get_settings
    from app.llm import LLMRouter

    router = LLMRouter(get_settings())
    results = []
    for name, items, true_dup in (
        ("Фарма", PHARMA_ITEMS, PHARMA_TRUE_DUP_PAIRS),
        ("Нефтегаз", OILGAS_ITEMS, OILGAS_TRUE_DUP_PAIRS),
    ):
        print(f"судья дублей: {name}...", file=sys.stderr)
        groups = await run_live(router, items)
        result = score(name, len(items), true_dup, groups)
        print_dataset_report(items, result)
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
            "Проба судьи дублей на карточках воротных отчётов Э2-Г с готовой ручной разметкой "
            "(РЕШ-133, РЕШ-152). Скрипт вне поставки продукта."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Ничего не оплачивать: напечатать оба набора (заголовок, org, snippet) и разметку без вызова модели.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
