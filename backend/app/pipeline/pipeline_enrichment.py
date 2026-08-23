"""Обогащение кандидатов по DOI (РЕШ-151, дробление `orchestrator.py`).

Отдельный модуль от поиска: обогащение работает на уже собранном списке
кандидатов и вслепую по DOI, не зная, каким источником они найдены, — это
отдельный, самодостаточный кусок прогона (РЕШ-52.3).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from pydantic import ValidationError

from app.pipeline.persistence import StageRecord
from app.schemas import Candidate
from app.search import normalize_doi

#: Логгер намеренно не по `__name__` — см. пояснение в `pipeline_context.py`:
#: код до дробления жил в `app.pipeline.orchestrator`.
log = logging.getLogger("app.pipeline.orchestrator")

#: Поля кандидата, которые обогащение имеет право заполнить. Только пустые:
#: данные источника, нашедшего кандидата, точнее — он видел его в контексте
#: своей выдачи, а обогащение работает вслепую по DOI.
_PATCHABLE_FIELDS = ("org", "snippet", "pdf_url")


def _apply_patch(candidate: Candidate, patch: dict) -> tuple[Candidate, list[str]]:
    """Заполнить пустые поля кандидата патчем обогащения.

    Непустое значение не перезаписывается никогда — это суть правки, а не
    деталь: иначе аффилиация из Crossref молча заменялась бы на организацию
    первого автора из OpenAlex, а фрагмент из витрины партнёров — на
    академическую аннотацию.

    Поля проверяются по одному, и негодное отбрасывается в одиночку (П-2
    аудита). Раньше валидация шла одним куском: патч с годной организацией и
    мусорным `pdf_url` отбрасывался целиком, вместе с ссылкой пропадали
    организация и аннотация, а сообщение уходило на `DEBUG`, которого в прогоне
    не видно. Возвращает пару «кандидат, имена отброшенных полей»: молчаливое
    отбрасывание — это ровно тот отказ, который снаружи неотличим от работы.
    """
    updates = {field: patch[field] for field in _PATCHABLE_FIELDS if patch.get(field) and not getattr(candidate, field)}
    if not updates:
        return candidate, []
    # Через повторную валидацию, а не model_copy: в патче сырые строки, и
    # кривой pdf_url обязан отвалиться здесь, а не в экспорте отчёта.
    base = candidate.model_dump(mode="json")
    result = candidate
    accepted: dict = {}
    dropped: list[str] = []
    for field_name, value in updates.items():
        try:
            result = Candidate.model_validate({**base, **accepted, field_name: value})
        except ValidationError as exc:
            dropped.append(field_name)
            log.info("обогащение: поле %s отброшено для %s: %s", field_name, candidate.url, exc)
            continue
        accepted[field_name] = value
    return result, dropped


class EnrichmentMixin:
    """Обогащение по DOI: одна стадия, вызывается на обоих проходах поиска."""

    async def enrich_candidates(
        self, candidates: list[Candidate], *, stage_log: list[StageRecord] | None = None, pass_no: int = 1
    ) -> tuple[list[Candidate], dict[str, int]]:
        """Добрать метаданные по DOI у источника обогащения (РЕШ-52.3).

        Возвращает пару: кандидаты с заполненными ПУСТЫМИ полями и цитируемость
        по ключу склейки кандидата. Цитируемость не поле `Candidate` — она
        нужна ровно одной стадии, скорингу, и едет отдельным словарём, а не
        правкой общего контракта данных.

        Отказ обогащения прогон не роняет: адаптер честно поднимает
        `SearchError` (инвариант №5 в редакции РЕШ-43), а здесь это становится
        строкой в логе и продолжением работы — обогащение необязательно, без
        него кандидаты просто беднее.

        Но «не роняет» не значит «незаметно»: у стадии три независимых пути
        тихой деградации — отказ целиком, прерванный обход и отброшенные поля
        патча, — и ни один не оставлял следа в `pipeline_logs` (П-5 аудита).
        Обогащение, отказывающее в каждом прогоне месяц подряд, снаружи было
        неотличимо от работающего. Поэтому стадия пишет `StageRecord` с
        непустым `error` — тем же способом, каким Т-2 завела запись об отказе
        интейка.

        `pass_no` — проход, которому принадлежит запись: обогащение зовётся и во
        втором проходе (РЕШ-81), и без явного номера его строка приписалась бы
        первому (Ц-2 пост-аудита Э2-Б).
        """
        adapters = await self._get_adapters()
        # Источник обогащения узнаётся по методу, а не по имени класса:
        # оркестратору незачем знать, что это именно OpenAlex.
        adapter = next((a for a in adapters if hasattr(a, "enrich_by_doi")), None)
        if adapter is None:
            return candidates, {}

        by_doi: dict[str, list[int]] = {}
        for index, candidate in enumerate(candidates):
            doi = normalize_doi(candidate.raw_id)
            if doi:
                by_doi.setdefault(doi, []).append(index)
        if not by_doi:
            return candidates, {}

        started = datetime.now(UTC)

        def write_stage(problem: str | None) -> None:
            """Строка журнала стадии обогащения; `problem` — что именно деградировало."""
            if stage_log is not None:
                stage_log.append(
                    StageRecord(
                        stage="enrich",
                        started_at=started,
                        finished_at=datetime.now(UTC),
                        pass_no=pass_no,
                        error=problem,
                    )
                )

        try:
            patches = await adapter.enrich_by_doi(list(by_doi))
        except Exception as exc:
            log.warning("обогащение по DOI не удалось (%s), прогон продолжается без него", exc)
            write_stage(f"обогащение по DOI не удалось: {exc}")
            return candidates, {}

        enriched = list(candidates)
        citations: dict[str, int] = {}
        dropped_fields: list[str] = []
        filled_org = 0
        for doi, patch in patches.items():
            for index in by_doi.get(doi, ()):
                candidate = enriched[index]
                cited = patch.get("cited_by_count")
                if isinstance(cited, int) and not isinstance(cited, bool):
                    citations[candidate.dedup_key()] = cited
                enriched[index], dropped = _apply_patch(candidate, patch)
                dropped_fields.extend(dropped)
                if not candidate.org and enriched[index].org:
                    filled_org += 1

        # Счётчик заполненных организаций — не косметика (П-1 аудита):
        # `stages.group_for_cards` группирует отобранных кандидатов по ТОЧНОЙ
        # строке `org`, а OpenAlex часто отдаёт зонтичную организацию
        # («Russian Academy of Sciences») вместо лаборатории. Значит обогащение
        # способно схлопнуть две несвязанные работы в одну карточку и сдвинуть
        # число карточек независимо от порога отбора. Без этой строки эффект не
        # измерить ни в одном прогоне.
        log.info(
            "обогащение: заполнен пустой org у %s кандидатов из %s (карточки группируются по точной строке org)",
            filled_org,
            len(candidates),
        )

        problems: list[str] = []
        # Обход, прерванный отказом на середине: патчи собраны не по всем DOI.
        interrupted = getattr(adapter, "last_error", None)
        if interrupted:
            problems.append(str(interrupted))
        if dropped_fields:
            names = ", ".join(sorted(set(dropped_fields)))
            summary = f"отброшено негодных полей патча — {len(dropped_fields)} ({names})"
            log.warning("обогащение: %s", summary)
            problems.append(summary)
        write_stage("; ".join(problems) or None)
        return enriched, citations
