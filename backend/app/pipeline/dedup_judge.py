"""Стадия — семантический судья дублей (Д-6 волны Э2-З, РЕШ-152).

ЗАЧЕМ
    Лексическое сходство заголовков не разделяет дубли: на настоящих данных
    обеих отраслей (`.planning/evidence/E2G-З6-similarity.md`) пара, которую
    отчёт сам признал одной разработкой (фарма, карточки 3–4), даёт сходство
    0.182, а пара разных разработок на одну тему (карточки 1–5) — 0.571,
    втрое больше. Никакой порог не разделяет эти диапазоны без ложных склеек
    (нефтегаз: диапазоны настоящих дублей и контрольных недублей
    перекрываются). Прагматичный ответ — не подбор порога, а вопрос модели:
    судья видит короткий (≤ 12) набор уже отобранных кандидатов и решает, кто
    из них — одна и та же разработка, а не считает совпавшие слова.

    Машинерия склейки (`app.pipeline.diversity.merge_duplicate_groups`)
    остаётся нетронутой (граница задачи Д-6, порог 0.65 не трогать): она
    работает как страховка ПОСЛЕ вердикта судьи, на тех же группах.

СТОИМОСТЬ
    Один вызов модели на прогон, не больше `JUDGE_CANDIDATE_LIMIT` кандидатов
    (66 пар при переборе — обозримо для одного батча). Судья спрашивается
    только по ОСНОВНОЙ очереди (`selected` перед `group_for_cards`), не по
    конкурсной: «+1 вызов модели на прогон», а не «+2» (граница задачи).

ДЕГРАДАЦИЯ
    Отказ модели (ошибка провайдера, кривой JSON после починки роутером) —
    это не отказ источника (инвариант №5 сформулирован про поиск), но тот же
    принцип: молчаливый отказ хуже честного. Отказ судьи не роняет прогон —
    карточки собираются, как собирались бы без семантической склейки, а факт
    отказа уходит в лог. Стоп-кран по стоимости (`CostLimitExceededError`) —
    исключение из этого правила: он не деградация, а команда «стоп тратить»,
    и пробрасывается наружу нетронутым, тем же приёмом, что у скоринга и
    сборки карточек (`scoring.py`, `cards.py`).
"""

from __future__ import annotations

import logging
from itertools import pairwise

from pydantic import BaseModel, Field

from app.llm import CostLimitExceededError, LLMRouter, Message
from app.pipeline.prompting import _GO, render_prompt
from app.schemas import ScoredCandidate

#: Имя логгера намеренно не `__name__`: остальные стадии пайплайна пишут в
#: `app.pipeline.stages` (см. `scoring.py`), и разбор прогона фильтрует
#: `caplog` по этой единой строке — своё имя здесь сделало бы строку судьи
#: невидимой для того же фильтра.
log = logging.getLogger("app.pipeline.stages")

#: Предел кандидатов, которых видит судья за один вызов (РЕШ-152, граница
#: задачи Д-6): 12 кандидатов дают 66 пар при полном переборе — ещё
#: обозримый батч для одного вызова. Кандидаты сверх предела в вызов не
#: попадают и остаются несклеенными этим признаком (страховка
#: `merge_duplicate_groups` по-прежнему их видит).
JUDGE_CANDIDATE_LIMIT = 12

#: Верхний предел размера ОДНОЙ группы дублей от судьи — без него мутация
#: «судья признал всё одной разработкой» проходит незамеченной (граница
#: задачи Д-6, мутация «б»). Самая большая ПОДТВЕРЖДЁННАЯ ручной разметкой
#: группа дублей во всей evidence — нефтегаз, четыре карточки из шести
#: (`E2G-З6-similarity.md`). Половина лимита кандидатов (12 // 2 = 6) даёт
#: заметный запас над этим прецедентом и одновременно гарантирует, что группа
#: размером во ВЕСЬ вход — явный признак переклейки, а не измеренного случая
#: — отбрасывается всегда.
JUDGE_MAX_GROUP_SIZE = JUDGE_CANDIDATE_LIMIT // 2

#: Длина фрагмента кандидата в промпте судьи. Короче, чем в карточке или
#: скоринге (§0.1 SPEC — щедрость там, где она окупается): судья сравнивает
#: короткий батч, а не дочитывает источник, и полной аннотации не требуется,
#: чтобы отличить одну разработку от другой.
_SNIPPET_LENGTH = 400


class _JudgeVerdict(BaseModel):
    """Ответ судьи: списки НОМЕРОВ кандидатов (не сами кандидаты), сведённых
    в одну разработку. Кандидат без пары в ответ не попадает вовсе — модель
    перечисляет только найденные группы, а не решение по каждому номеру."""

    duplicate_groups: list[list[int]] = Field(default_factory=list)


def _listing(candidates: list[ScoredCandidate]) -> str:
    """Пронумерованный список кандидатов для промпта судьи: номер, заголовок,
    организация, фрагмент. Модель ссылается на кандидата номером — то же
    правило, что у скоринга (§0.1 SPEC, ловушка 2): переписывать входные
    данные в ответ не просим, значит и раздувать вход целиком незачем."""
    blocks = []
    for position, item in enumerate(candidates, start=1):
        c = item.candidate
        blocks.append(
            f"{position}. {c.title}\n"
            f"   Организация: {c.org or 'не указана'}\n"
            f"   Фрагмент: {c.snippet[:_SNIPPET_LENGTH] or 'нет фрагмента'}"
        )
    return "\n\n".join(blocks)


def _sanitize(raw_groups: list[list[int]], *, size: int) -> list[list[int]]:
    """Отфильтровать ответ судьи до безопасных групп.

    Три проверки, каждая — против конкретного отказа модели: (1) номера вне
    диапазона входа отбрасываются, а не роняют вызов; (2) группа размером
    один — не дубль, пропускается; (3) группа больше `JUDGE_MAX_GROUP_SIZE`
    или пересекающаяся с уже принятой — явный признак того, что судья
    объединяет по теме, а не по сути (мутация «б»), такая группа отбрасывается
    целиком, а не обрезается: обрезка спрятала бы отказ вместо честного лога.
    """
    sanitized: list[list[int]] = []
    claimed: set[int] = set()
    for group in raw_groups:
        numbers = sorted({n for n in group if isinstance(n, int) and 1 <= n <= size})
        if len(numbers) < 2:
            continue
        if len(numbers) > JUDGE_MAX_GROUP_SIZE:
            log.warning(
                "судья дублей вернул группу из %s кандидатов (предел %s) — группа отброшена, похоже на переклейку "
                "по теме, а не по сути",
                len(numbers),
                JUDGE_MAX_GROUP_SIZE,
            )
            continue
        if claimed & set(numbers):
            log.warning("судья дублей вернул пересекающиеся группы — вторая с пересечением отброшена")
            continue
        claimed |= set(numbers)
        sanitized.append(numbers)
    return sanitized


async def judge_duplicate_groups(router: LLMRouter, candidates: list[ScoredCandidate]) -> list[list[int]] | None:
    """Спросить модель, какие из отобранных кандидатов — одна и та же
    разработка. Возвращает список групп НОМЕРОВ (1-индексация — как в
    промпте, не 0, чтобы номер совпадал с тем, что видит человек в логе)
    размером ≥ 2 каждая; кандидат без пары в ответе не упоминается.

    `None` — либо кандидатов меньше двух (дублировать нечего, вызов модели
    того не стоит), либо модель отказала после починки роутером: это
    деградация (см. докстроку модуля), а не отказ прогона. Кандидаты сверх
    `JUDGE_CANDIDATE_LIMIT` в вызов не попадают — номера в ответе относятся
    только к переданному подмножеству.
    """
    if len(candidates) < 2:
        return None
    limited = candidates[:JUDGE_CANDIDATE_LIMIT]
    prompt = render_prompt("dedup_judge", CANDIDATES_LISTING=_listing(limited))
    try:
        verdict = await router.complete_json(
            "dedup_judge",
            [
                # Содержательное — в системном сообщении: так DeepSeek
                # кэширует префикс (общая часть — правила, а не сами
                # кандидаты, что тоже дешевле повторного счёта правил).
                Message(role="system", content=prompt),
                Message(role="user", content=_GO),
            ],
            _JudgeVerdict,
            # Батч короткий (≤ 12 кандидатов), но модель рассуждающая:
            # щедрый лимит из §0.1 SPEC (не ниже 6000) обязателен, иначе
            # рассуждение съедает бюджет и content приходит пустым.
            max_tokens=6000,
        )
    except CostLimitExceededError:
        # Стоп-кран — не отказ модели, а команда «стоп тратить» (РЕШ-45):
        # деградация здесь замаскировала бы исчерпанный лимит под обычную
        # неудачу судьи и позволила бы прогону продолжать тратить дальше.
        raise
    except Exception as exc:
        # Намеренно широкий перехват: деградация, инвариант №5-стиль — модель
        # недоступна, а не источник, но принцип тот же: карточки собираются
        # без судьи, а не роняют прогон.
        log.warning("судья дублей не ответил (%s), группировка идёт без семантической склейки", exc)
        return None
    return _sanitize(verdict.duplicate_groups, size=len(limited))


def apply_judge_groups(
    groups: list[list[ScoredCandidate]],
    judge_groups: list[list[int]],
    judge_input: list[ScoredCandidate],
) -> list[list[ScoredCandidate]]:
    """Слить заготовки `group_for_cards` по вердикту судьи.

    Наивная склейка (просто дописать группы судьи в общий список и прогнать
    через `merge_duplicate_groups`) заводит кандидата в ДВЕ группы сразу: она
    сливает лишь ту заготовку, что стоит в списке раньше, а вторую заготовку
    того же кандидата оставляет отдельно — на выходе один источник печатался
    бы в двух карточках. Поэтому склейка идёт через union-find по ЛИЧНОСТИ
    объекта кандидата: каждая заготовка `group_for_cards` — узел, пара
    заготовок сливается, если хотя бы один их кандидат состоит в одной группе
    судьи, — и на выходе каждый кандидат остаётся ровно в одной заготовке.

    `judge_input` — тот самый (усечённый до `JUDGE_CANDIDATE_LIMIT`) список,
    который передавался в `judge_duplicate_groups`: номера из `judge_groups`
    — это позиции в НЁМ, не в исходном `groups`.
    """
    if not judge_groups:
        return groups

    owner: dict[int, int] = {}
    for group_index, group in enumerate(groups):
        for item in group:
            owner[id(item)] = group_index

    parent = list(range(len(groups)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: int, b: int) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    for cluster in judge_groups:
        owners_in_cluster = sorted(
            {owner[id(judge_input[number - 1])] for number in cluster if id(judge_input[number - 1]) in owner}
        )
        for a, b in pairwise(owners_in_cluster):
            union(a, b)

    buckets: dict[int, list[ScoredCandidate]] = {}
    for index, group in enumerate(groups):
        buckets.setdefault(find(index), []).extend(group)
    return list(buckets.values())
