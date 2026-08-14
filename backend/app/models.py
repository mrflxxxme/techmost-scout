"""Таблицы базы данных.

Здесь живёт то, что должно переживать перезапуск: запросы, брифы, кандидаты,
карточки, вердикты эксперта, витрина партнёров и журнал стадий. Накопленные
запросы и вердикты — будущий главный актив компании, поэтому ничего не
удаляем, только помечаем статусами.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Request(Base):
    """Один запрос бизнеса на подбор решений."""

    __tablename__ = "requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    raw_text: Mapped[str] = mapped_column(Text)
    # intake → briefing → searching → drafted → validated → exported | failed
    status: Mapped[str] = mapped_column(String(32), default="intake", index=True)
    cost_rub: Mapped[float] = mapped_column(Float, default=0.0)
    demo_flag: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    brief: Mapped[Brief | None] = relationship(back_populates="request", uselist=False, cascade="all, delete-orphan")
    candidates: Mapped[list[Candidate]] = relationship(back_populates="request", cascade="all, delete-orphan")
    cards: Mapped[list[SolutionCard]] = relationship(back_populates="request", cascade="all, delete-orphan")
    logs: Mapped[list[PipelineLog]] = relationship(back_populates="request", cascade="all, delete-orphan")


class Brief(Base):
    """Структурированная постановка задачи после уточнений."""

    __tablename__ = "briefs"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), unique=True)
    industry: Mapped[str] = mapped_column(String(255))
    task_summary: Mapped[str] = mapped_column(Text)
    constraints: Mapped[list] = mapped_column(JSON, default=list)
    target_maturity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    geography: Mapped[str] = mapped_column(String(128), default="РФ")
    budget_hint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    timeline_hint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    keywords_ru: Mapped[list] = mapped_column(JSON, default=list)
    keywords_en: Mapped[list] = mapped_column(JSON, default=list)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    request: Mapped[Request] = relationship(back_populates="brief")


class Candidate(Base):
    """Найденный кандидат до превращения в карточку."""

    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    org: Mapped[str | None] = mapped_column(String(512), nullable=True)
    authors: Mapped[list] = mapped_column(JSON, default=list)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snippet: Mapped[str] = mapped_column(Text, default="")
    raw_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dedup_key: Mapped[str] = mapped_column(String(512), index=True)
    score: Mapped[dict] = mapped_column(JSON, default=dict)
    # found → deduped → selected → rejected
    status: Mapped[str] = mapped_column(String(32), default="found")

    request: Mapped[Request] = relationship(back_populates="candidates")


class SolutionCard(Base):
    """Карточка варианта решения — единица ценности для клиента."""

    __tablename__ = "solution_cards"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(Text)
    essence: Mapped[str] = mapped_column(Text)
    maturity_stage: Mapped[str] = mapped_column(String(32))
    trl_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    developer_org: Mapped[str | None] = mapped_column(String(512), nullable=True)
    developer_team: Mapped[str | None] = mapped_column(String(512), nullable=True)
    contact_leads: Mapped[list] = mapped_column(JSON, default=list)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    applicability: Mapped[str] = mapped_column(Text)
    risks: Mapped[list] = mapped_column(JSON, default=list)
    gov_support: Mapped[list] = mapped_column(JSON, default=list)
    next_step: Mapped[str] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(16), default="medium")
    confidence_note: Mapped[str] = mapped_column(Text, default="")
    candidate_urls: Mapped[list] = mapped_column(JSON, default=list)
    #: Три поля ниже печатаются в markdown-отчёте: без них карточка в базе
    #: выходит «увереннее» отчёта — не видно ни сверки с требованиями, ни того,
    #: что проверяющая стадия сочла неподтверждённым
    requirement_checks: Mapped[list] = mapped_column(JSON, default=list)
    unsourced_claims: Mapped[list] = mapped_column(JSON, default=list)
    pdf_urls: Mapped[list] = mapped_column(JSON, default=list)
    #: Правки оператора поверх машинного текста — машинный текст сохраняем
    editor_overrides: Mapped[dict] = mapped_column(JSON, default=dict)

    request: Mapped[Request] = relationship(back_populates="cards")
    verdicts: Mapped[list[Verdict]] = relationship(back_populates="card", cascade="all, delete-orphan")


class Verdict(Base):
    """Вердикт научного эксперта. Одновременно контроль качества и обучающая выборка."""

    __tablename__ = "verdicts"

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("solution_cards.id"), index=True)
    reviewer_name: Mapped[str] = mapped_column(String(255))
    decision: Mapped[str] = mapped_column(String(16))  # approve | reject | rework
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="Оценка 1-5 для протокола приёмки")
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    card: Mapped[SolutionCard] = relationship(back_populates="verdicts")


class ShowcaseItem(Base):
    """Витрина разработок партнёрских институтов — приоритетный слой выдачи."""

    __tablename__ = "showcase_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    org: Mapped[str] = mapped_column(String(512))
    contact: Mapped[str] = mapped_column(String(512), default="")
    maturity_stage: Mapped[str] = mapped_column(String(32), default="lab")
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_note: Mapped[str] = mapped_column(Text, default="", comment="Откуда получено, кто подтвердил")
    added_by: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PipelineLog(Base):
    """Журнал стадий: время, источники, токены, стоимость. Основа демо-режима."""

    __tablename__ = "pipeline_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), index=True)
    stage: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_rub: Mapped[float] = mapped_column(Float, default=0.0)
    sources_hit: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    request: Mapped[Request] = relationship(back_populates="logs")
