"""Контракты данных пайплайна.

Единственный источник истины по формам данных между стадиями. Любая стадия
принимает и возвращает только эти модели — так стадии остаются заменяемыми,
а тесты пишутся на фикстурах без обращения к внешним API.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl, field_validator


class MaturityStage(StrEnum):
    """Стадия зрелости разработки на языке, понятном промышленному заказчику."""

    LAB = "lab"  # работает в лаборатории
    PROTOTYPE = "prototype"  # есть опытный образец
    PILOT = "pilot"  # прошла пилот на реальном объекте
    SERIES = "series"  # серийно применяется


class SourceType(StrEnum):
    PUBLICATION = "publication"
    PATENT = "patent"
    GRANT = "grant"
    ORG = "org"
    SHOWCASE = "showcase"  # витрина партнёров — приоритетный слой
    WEB = "web"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Verdict(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REWORK = "rework"


class Brief(BaseModel):
    """Структурированная постановка задачи после уточняющих вопросов.

    Обезличена: названия компаний-заказчиков сюда не попадают (см. anonymize).
    """

    industry: str = Field(description="Отрасль заказчика, например «нефтепереработка»")
    task_summary: str = Field(description="Суть технологической задачи, 1-3 предложения")
    constraints: list[str] = Field(default_factory=list, description="Ограничения: среда, ГОСТ, габариты, температура")
    target_maturity: MaturityStage | None = Field(default=None, description="Желаемая готовность решения")
    geography: str = Field(default="РФ", description="Где предполагается внедрение")
    budget_hint: str | None = None
    timeline_hint: str | None = None
    keywords_ru: list[str] = Field(default_factory=list)
    keywords_en: list[str] = Field(default_factory=list)


class IntakeQuestion(BaseModel):
    question: str
    why: str = Field(description="Зачем это нужно — показывается оператору")


class IntakeResult(BaseModel):
    questions: list[IntakeQuestion] = Field(default_factory=list, max_length=5)
    draft_brief: Brief


class SearchQuery(BaseModel):
    """Одна поисковая формулировка под конкретный тип источника."""

    text: str
    source_type: SourceType
    lang: str = Field(default="ru", pattern="^(ru|en)$")
    rationale: str = Field(description="Почему именно такая формулировка")


class SearchPlan(BaseModel):
    queries: list[SearchQuery] = Field(min_length=1, max_length=24)


class Candidate(BaseModel):
    """Сырой кандидат от поискового адаптера. Без URL кандидат отбрасывается."""

    source_type: SourceType
    title: str
    url: HttpUrl
    org: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    snippet: str = ""
    raw_id: str | None = Field(default=None, description="DOI, номер патента, id гранта")
    provider: str = Field(description="Имя адаптера: crossref, cyberleninka, showcase, …")

    @field_validator("year")
    @classmethod
    def _sane_year(cls, v: int | None) -> int | None:
        if v is not None and not (1900 <= v <= 2100):
            return None
        return v

    def dedup_key(self) -> str:
        """Ключ склейки дублей: DOI/номер, иначе нормализованное название."""
        if self.raw_id:
            return self.raw_id.strip().lower()
        normalized = "".join(ch for ch in self.title.lower() if ch.isalnum() or ch.isspace())
        return " ".join(normalized.split())


class ScoredCandidate(BaseModel):
    candidate: Candidate
    relevance: int = Field(ge=0, le=10)
    maturity: int = Field(ge=0, le=10)
    accessibility: int = Field(ge=0, le=10, description="Насколько реально выйти на разработчика")
    freshness: int = Field(ge=0, le=10)
    completeness: int = Field(ge=0, le=10)
    notes: str = ""

    @property
    def total(self) -> float:
        """Взвешенная сумма. Релевантность и доступность весят больше всего:
        карточка бесполезна, если решение не по задаче или разработчика не найти."""
        return (
            self.relevance * 0.35
            + self.maturity * 0.20
            + self.accessibility * 0.20
            + self.freshness * 0.10
            + self.completeness * 0.15
        )


class Evidence(BaseModel):
    """Утверждение и ссылка, которая его подтверждает.

    Правило системы: в карточке не бывает фактического утверждения без Evidence.
    """

    claim: str
    url: HttpUrl


class GovSupportMatch(BaseModel):
    instrument: str = Field(description="Название инструмента из data/gov_support.yaml")
    why: str = Field(description="Почему подходит этой разработке и задаче")
    limit: str | None = Field(default=None, description="Ограничение по сумме или доле")
    url: HttpUrl | None = None


class SolutionCard(BaseModel):
    """Итоговая карточка варианта решения — то, что видит клиент после валидации."""

    title: str
    essence: str = Field(description="Суть решения деловым языком, без академизма")
    maturity_stage: MaturityStage
    trl_estimate: int | None = Field(default=None, ge=1, le=9)
    developer_org: str | None = None
    developer_team: str | None = None
    contact_leads: list[str] = Field(default_factory=list, description="Зацепки для выхода на связь")
    evidence: list[Evidence] = Field(default_factory=list)
    applicability: str = Field(description="Как закрывает задачу из брифа")
    risks: list[str] = Field(default_factory=list)
    gov_support: list[GovSupportMatch] = Field(default_factory=list)
    next_step: str = Field(description="Конкретный следующий шаг для клиента")
    confidence: Confidence = Confidence.MEDIUM
    confidence_note: str = ""
    candidate_urls: list[HttpUrl] = Field(default_factory=list)

    def has_unsourced_claims(self) -> bool:
        """Грубая проверка: карточка обязана нести хотя бы одно подтверждение."""
        return len(self.evidence) == 0


class ReportSummary(BaseModel):
    comparison_rows: list[dict[str, str]] = Field(default_factory=list)
    recommendation: str
    caveats: list[str] = Field(default_factory=list)


class Report(BaseModel):
    request_id: int
    brief: Brief
    cards: list[SolutionCard]
    summary: ReportSummary
    sources_unavailable: list[str] = Field(
        default_factory=list, description="Источники, которые не ответили — честно пишем в отчёт"
    )
    generated_at: datetime
    cost_rub: float = 0.0
