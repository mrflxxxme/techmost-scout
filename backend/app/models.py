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
    search_plans: Mapped[list[SearchPlan]] = relationship(back_populates="request", cascade="all, delete-orphan")
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


class SearchPlan(Base):
    """План поиска одного прохода — из чего собрана вся выдача прогона (РЕШ-83).

    Без сохранённого плана эффект правок поиска неотличим от разброса между
    прогонами: модель строит план заново каждый раз, и один и тот же запрос
    агропрома дал 92 и 72 кандидата в двух прогонах Э2-А. Сохранённый план
    позволяет повторить прогон по тем же формулировкам и сравнивать выдачу, а
    не удачу планировщика.
    """

    __tablename__ = "search_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), index=True)
    #: 1 — первый проход, 2 — второй (условный второй проход, РЕШ-81). Колонка
    #: заведена сразу, хотя пишется пока только единица: `init_db` — это
    #: `create_all`, а он не добавляет колонки в уже существующую таблицу, и
    #: вторая правка схемы обошлась бы ручным переносом базы (грабля Э2-А).
    pass_no: Mapped[int] = mapped_column(Integer, default=1, index=True)
    #: Дамп `schemas.SearchPlan.model_dump(mode="json")` целиком, вместе с
    #: ключом `queries`: план читается обратно той же схемой одним вызовом, без
    #: собственного разбора полей — иначе расхождение схемы и разбора всплыло бы
    #: только на платном прогоне.
    queries: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    request: Mapped[Request] = relationship(back_populates="search_plans")


class Candidate(Base):
    """Найденный кандидат до превращения в карточку.

    Строки пишутся с Т-20 (РЕШ-84): до неё оценки скоринга жили только в строке
    журнала калибровки, и числовой критерий приёмки подэтапа (РЕШ-86 — «максимум
    `relevance` вырос с 4 до ≥6, число кандидатов не ниже порога выросло с 3»)
    подтвердить было нечем.
    """

    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), index=True)
    #: 1 — первый проход поиска, 2 — второй (условный второй проход, РЕШ-81).
    #: Колонка заведена сразу, хотя пишется пока только единица: `init_db` — это
    #: `create_all`, а он не добавляет колонки в уже существующую таблицу, и
    #: вторая правка схемы обошлась бы ручным переносом базы (грабля Э2-А). Тем
    #: же способом и по той же причине Т-19 завела `search_plans.pass_no`.
    pass_no: Mapped[int] = mapped_column(Integer, default=1, index=True)
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
    #: Все пять оценок, взвешенная сумма `total` и признак `neutral` — оценку
    #: поставил код после отказа батча, а не модель (`app.metrics.score_payload`).
    #: Без признака измеритель «доля `relevance` ≥ 4» считал бы следы отказов за
    #: результат: нейтральная оценка — ровно пятёрка по всем шкалам.
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
    #: Секция отчёта, в которую попала карточка (РЕШ-85 п. 1, РЕШ-92): `False` —
    #: «Варианты решений» (лимит 5–7, FR-9), `True` — «Ранние разработки и
    #: команды», вне лимита. Отдельная колонка, а не догадка по `position` или
    #: по типу источника связанных кандидатов: карточки обеих секций лежат в
    #: одной таблице, и без признака в базе не отличить решение, прошедшее общий
    #: отбор, от конкурсного, который в общем отборе не участвовал вовсе.
    is_contest: Mapped[bool] = mapped_column(default=False, index=True)
    title: Mapped[str] = mapped_column(Text)
    essence: Mapped[str] = mapped_column(Text)
    maturity_stage: Mapped[str] = mapped_column(String(32))
    trl_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Статус прав на решение (РЕШ-79 п. 1): `у команды`,
    #: `передано организатору или заказчику` либо `не установлено`. По умолчанию
    #: — «не установлено»: автоматически статус не определяется, и пустая строка
    #: здесь читалась бы как «прав нет вопросов», чего разведка не подтверждает.
    ip_status: Mapped[str] = mapped_column(String(64), default="не установлено")
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
    """Журнал стадий: время, источники, стоимость. Основа демо-режима."""

    __tablename__ = "pipeline_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), index=True)
    #: 1 — первый проход поиска, 2 — второй (условный второй проход, РЕШ-81).
    #: Заведена по тому же доводу, что `search_plans.pass_no` (Т-19) и
    #: `candidates.pass_no` (Т-20): второй проход даёт вторую строку `search` и
    #: вторую `scorer` с теми же именами стадий, и без этого поля расход второго
    #: прохода (доклад по РЕШ-82) отделялся бы упорядочиванием строк по времени
    #: с отсечкой по `replan` — выводом из порядка записей, а не из данных.
    #: Индекс заводится сразу: `init_db` — это `create_all`, а он не добавляет
    #: ни колонок, ни индексов в уже существующую таблицу (грабля Э2-А).
    pass_no: Mapped[int] = mapped_column(Integer, default=1, index=True)
    stage: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # `tokens_in`/`tokens_out` удалены из модели (РЕШ-174): счётчик стоимости
    # копит только рубли, токены нигде не читались. На уже существующем файле
    # базы старые колонки остаются мёртвым грузом до пересоздания — SQLite не
    # мигрирует схему при `create_all`, и это не задача этой правки.
    cost_rub: Mapped[float] = mapped_column(Float, default=0.0)
    sources_hit: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    request: Mapped[Request] = relationship(back_populates="logs")
