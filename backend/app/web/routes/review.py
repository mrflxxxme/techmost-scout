"""Экран ревью эксперта (SPEC Э4-1, «Контракты» → «Вердикты», РЕШ-196–197).

`GET /requests/{id}/review` показывает **все** карточки запроса обеих секций
(включая уже отклонённые — эксперт видит их для контроля, в отличие от
клиентского вида, который их прячет). Поэтому здесь сознательно не
используется `export.view.build_export_view` целиком (фильтр reject и
переиндексация позиций той сборки ревью не нужны) — но правки оператора
(`report_overrides.apply_overrides`) на отчёт всё же накладываются (РЕШ-205):
эксперт должен валидировать ровно тот текст, что уедет клиенту, а не
исходный вывод модели. Наложение правок делается по исходным id строк
`solution_cards`, фильтр reject поверх не применяется.

`POST /cards/{card_id}/verdict` пишет **новую** строку `verdicts` — ничего не
обновляется и не удаляется (РЕШ-196 п. 3). Когда после записи у каждой
строки `solution_cards` запроса есть хотя бы один вердикт
(`app.verdicts.all_cards_reviewed`), статус запроса становится `validated`;
`exported` этим не понижается.

`POST /requests/{id}/summary` — правка рекомендации и оговорок сводки
(РЕШ-206): в отличие от вердиктов, здесь **одна** строка `summary_overrides`
на запрос (`app.models.SummaryOverride`), повторный вызов перезаписывает её
же, а не добавляет новую — текст сводки не обучающая выборка, ценность
только в последней версии, которая уедет клиенту.

`POST /requests/{id}/analogs/{analog_key}/verdict` — поштучный вердикт на
зарубежный аналог (Э6-7): тоже **новая** строка (`analog_verdicts`), без оценки
1–5. Статус запроса, `all_cards_reviewed` и подпись вердикты аналогов не
меняют — замок отчёта целиком прежний.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from html import escape
from re import fullmatch

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.export.analogs import ANALOG_SECTION_TITLE
from app.export.markdown import CONTEST_SECTION_HEADING
from app.models import AnalogVerdict as AnalogVerdictRow
from app.models import Request as RequestRow
from app.models import RequestStatus
from app.models import SolutionCard as SolutionCardRow
from app.models import SummaryOverride as SummaryOverrideRow
from app.models import Verdict as VerdictRow
from app.pipeline.persistence import ReportNotFoundError, load_report
from app.report_overrides import OverrideMap, apply_overrides
from app.schemas import AnalogCard, SolutionCard
from app.schemas import Verdict as VerdictDecision
from app.summary_overrides import SummaryOverrideData, apply_summary_override, resolve_summary_edit
from app.verdicts import (
    CardKey,
    all_cards_reviewed,
    analog_key,
    effective_analog_verdicts,
    effective_verdicts,
    hidden_reason,
)

log = logging.getLogger(__name__)
router = APIRouter()

#: Новые клиентские фразы задачи — см. handoff для визы владельца.
REQUEST_NOT_FOUND = "Запрос №{request_id} не найден."
CARD_NOT_FOUND = "Карточка №{card_id} не найдена."
REFUSAL_EMPTY_NAME = "Укажите имя проверяющего — без имени вердикт не засчитывается."
REFUSAL_RATING_RANGE = "Оценка должна быть от 1 до 5."
REFUSAL_UNKNOWN_DECISION = "Неизвестное решение — выберите «Одобрить», «На доработку» или «Отклонить»."
REFUSAL_EMPTY_RECOMMENDATION = "Рекомендация не может быть пустой — опишите, с чего начать."
ANALOG_NOT_FOUND = "Аналог не найден в отчёте запроса №{request_id}."
#: Ключ аналога в адресе — 16 hex `verdicts.analog_key`; иной вид — 404 без чтения базы.
_ANALOG_KEY_PATTERN = r"[0-9a-f]{16}"

#: Русские подписи решения — общие для формы и фрагмента с записанным вердиктом.
DECISION_LABELS: dict[VerdictDecision, str] = {
    VerdictDecision.APPROVE: "Одобрено",
    VerdictDecision.REWORK: "На доработку",
    VerdictDecision.REJECT: "Отклонено",
}

#: Причины, по которым карточка не уедет клиенту, — эксперт видит их на
#: экране ревью рядом с формой правки сводки (аудит Э4, правка П-6): без
#: этого блока номера карточек в рекомендации и оговорках, которые эксперт
#: правит вручную, было не с чем сверить.
HIDDEN_REJECTED = "отклонена экспертом"
HIDDEN_NO_EVIDENCE = "нет ни одного подтверждения"


def _templates(request: Request):
    """Шаблоны берём из приложения, чтобы не плодить второй экземпляр Jinja."""
    from app.main import templates

    return templates


@asynccontextmanager
async def _session_scope(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Открытая сессия под форму `SessionFactory`, которую ждёт `load_report`."""
    yield session


def _notice(text: str, *, status_code: int) -> HTMLResponse:
    """Фрагмент-отказ: `.notice.danger` из `theme.css` (та же форма, что в `run.py`)."""
    return HTMLResponse(content=f'<div class="notice danger">{text}</div>', status_code=status_code)


async def _load_card_ids(session: AsyncSession, request_id: int) -> dict[CardKey, int]:
    """Id строк `solution_cards` запроса по ключу (секция, позиция) — без фильтра reject."""
    rows = (
        (await session.execute(select(SolutionCardRow).where(SolutionCardRow.request_id == request_id))).scalars().all()
    )
    return {(row.is_contest, row.position): row.id for row in rows}


async def _load_overrides(session: AsyncSession, request_id: int) -> OverrideMap:
    """Правки оператора по ключу (секция, позиция) — тем же ключом, что и `_load_card_ids`."""
    rows = (
        (await session.execute(select(SolutionCardRow).where(SolutionCardRow.request_id == request_id))).scalars().all()
    )
    return {(row.is_contest, row.position): dict(row.editor_overrides or {}) for row in rows}


async def _load_effective(session: AsyncSession, card_ids: dict[CardKey, int]) -> dict[int, VerdictRow]:
    """Действующие вердикты (последний по `created_at`, тай-брейк по `id`) по карточкам запроса."""
    if not card_ids:
        return {}
    found = await session.execute(select(VerdictRow).where(VerdictRow.card_id.in_(card_ids.values())))
    return effective_verdicts(found.scalars().all())


async def _load_summary_override_row(session: AsyncSession, request_id: int) -> SummaryOverrideRow | None:
    """Строка `summary_overrides` запроса — не более одной по построению (unique)."""
    return (
        (await session.execute(select(SummaryOverrideRow).where(SummaryOverrideRow.request_id == request_id)))
        .scalars()
        .one_or_none()
    )


async def _load_original_summary(session: AsyncSession, request_id: int) -> tuple[str, list[str]]:
    """Рекомендация и оговорки сводки ДО правки эксперта — после наложения правок карточек.

    Точка сравнения для `resolve_summary_edit` (П-6): текст модели, прошедший
    те же правки оператора, что видит эксперт в предзаполненной форме
    (`show_review`), но без наложения правки самой сводки — иначе сравнение с
    уже сохранённой прежде правкой скрывало бы повторную правку эксперта.
    """
    report = await load_report(lambda: _session_scope(session), request_id)
    overrides = await _load_overrides(session, request_id)
    report = apply_overrides(report, overrides)
    return report.summary.recommendation, list(report.summary.caveats)


def _reason_text(reason: str | None) -> str | None:
    """Завизированный русский текст причины скрытия (РЕШ-205) либо `None`, если скрытия нет."""
    if reason is None:
        return None
    return HIDDEN_REJECTED if reason == "rejected" else HIDDEN_NO_EVIDENCE


def _hidden_reason(card: SolutionCard, card_id: int | None, effective: dict[int, VerdictRow]) -> str | None:
    """Причина, по которой карточка не уедет клиенту, либо `None`, если уедет.

    Признак берётся напрямую у единственного источника причины скрытия —
    `app.verdicts.hidden_reason` (РЕШ-215), тем же условием и в том же
    порядке, что решает видимость карточки в клиентском экспорте
    (`require_evidence=True`, режим клиентского вида, РЕШ-213). Здесь
    остаётся только перевод машинного признака в завизированный русский
    текст (РЕШ-205) — саму видимость эта функция не пересчитывает.
    """
    verdict = effective.get(card_id) if card_id is not None else None
    return _reason_text(hidden_reason(card, verdict, require_evidence=True))


def _annotate_analogs(
    cards: list[AnalogCard], effective: dict[str, AnalogVerdictRow]
) -> list[tuple[AnalogCard, str, str | None]]:
    """Аналоги отчёта для экрана: сама карточка, ключ формы и причина скрытия (или `None`).

    Причину считает та же `verdicts.hidden_reason`, что и клиентский вид
    (`require_evidence=True`); судьба аналога по стране здесь не показывается.
    """
    result: list[tuple[AnalogCard, str, str | None]] = []
    for card in cards:
        key = analog_key(card)
        result.append((card, key, _reason_text(hidden_reason(card, effective.get(key), require_evidence=True))))
    return result


async def _load_effective_analogs(session: AsyncSession, request_id: int) -> dict[str, AnalogVerdictRow]:
    """Действующие вердикты аналогов запроса по ключу аналога."""
    found = await session.execute(select(AnalogVerdictRow).where(AnalogVerdictRow.request_id == request_id))
    return effective_analog_verdicts(list(found.scalars().all()))


def _annotate_client_view(
    cards: list[SolutionCard],
    card_ids: dict[CardKey, int],
    effective: dict[int, VerdictRow],
    *,
    is_contest: bool,
) -> list[tuple[SolutionCard, int | None, int | None, str | None]]:
    """Разметить карточки секции: id строки, номер у клиента (или `None`) и причина скрытия (или `None`).

    Номер у клиента считается заново с единицы в пределах секции — по тому же
    признаку, что решает видимость в клиентском экспорте (РЕШ-213): вызванная
    здесь `_hidden_reason` зовёт `verdicts.hidden_reason` с
    `require_evidence=True`, тем же условием, каким `export.view.
    build_export_view` строит `client_report`, а `export.view.
    _comparison_table_hidden` решает, прятать ли таблицу сравнения. Пустой
    `evidence` учитывается здесь же, одним проходом — не позже, в
    `pdf.py`/`docx.py` (так было до РЕШ-213): с четвёртой волны экспортёры
    своего фильтра по `evidence` не ведут вовсе, только печатают уже
    отфильтрованный `ExportView.client_report`.
    """
    result: list[tuple[SolutionCard, int | None, int | None, str | None]] = []
    client_num = 0
    for position, card in enumerate(cards):
        card_id = card_ids.get((is_contest, position))
        reason = _hidden_reason(card, card_id, effective)
        if reason is None:
            client_num += 1
            result.append((card, card_id, client_num, None))
        else:
            result.append((card, card_id, None, reason))
    return result


@router.get("/requests/{request_id}/review", response_class=HTMLResponse)
async def show_review(request_id: int, request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Экран ревью: карточки обеих секций в одну колонку, форма вердикта на каждую.

    В отличие от страницы отчёта, здесь показываются все карточки без фильтра
    reject — эксперт должен видеть отклонённые варианты, чтобы контролировать
    свои же прежние решения, а не только то, что уйдёт клиенту.
    """
    row = await session.get(RequestRow, request_id)
    if row is None:
        raise HTTPException(status_code=404, detail=REQUEST_NOT_FOUND.format(request_id=request_id))

    try:
        report = await load_report(lambda: _session_scope(session), request_id)
    except ReportNotFoundError as exc:
        # Текст исключения — только в лог; на экране остаётся человеческая фраза.
        log.info("отчёт запроса №%s недоступен для ревью: %s", request_id, exc)
        return _templates(request).TemplateResponse(
            request=request,
            name="review.html",
            context={"row": row, "report": None},
            status_code=404,
        )

    card_ids = await _load_card_ids(session, request_id)
    effective = await _load_effective(session, card_ids)

    # РЕШ-205: эксперт видит правленый текст, а не исходный вывод модели —
    # ключи (is_contest, position) остаются позициями из БД, наложение их не
    # сдвигает (в отличие от `build_export_view`, здесь нет фильтра reject).
    overrides = await _load_overrides(session, request_id)
    report = apply_overrides(report, overrides)

    # РЕШ-206: то же правило для сводки — форма правки показывает текущий
    # текст (правленый, если правка есть, иначе исходный вывод модели).
    summary_override_row = await _load_summary_override_row(session, request_id)
    summary_override = (
        SummaryOverrideData(recommendation=summary_override_row.recommendation, caveats=summary_override_row.caveats)
        if summary_override_row is not None
        else None
    )
    report = apply_summary_override(report, summary_override)

    # П-6 (аудит Э4): эксперт правит рекомендацию и оговорки по номерам карточек
    # с этого же экрана — рядом с формой правки нужно честно показать, какие
    # карточки клиент вообще не увидит и под какими номерами уедут остальные.
    main_visibility = _annotate_client_view(report.cards, card_ids, effective, is_contest=False)
    contest_visibility = _annotate_client_view(report.contest_cards, card_ids, effective, is_contest=True)

    cards_with_ids = [
        (index + 1, card, card_id, client_num, reason)
        for index, (card, card_id, client_num, reason) in enumerate(main_visibility)
    ]
    contest_with_ids = [
        (index + 1, card, card_id, client_num, reason)
        for index, (card, card_id, client_num, reason) in enumerate(contest_visibility)
    ]

    analogs_with_keys = _annotate_analogs(report.analog_cards, await _load_effective_analogs(session, request_id))

    return _templates(request).TemplateResponse(
        request=request,
        name="review.html",
        context={
            "row": row,
            "report": report,
            "cards_with_ids": cards_with_ids,
            "contest_with_ids": contest_with_ids,
            "analogs_with_keys": analogs_with_keys,
            "analog_heading": ANALOG_SECTION_TITLE,
            "contest_heading": CONTEST_SECTION_HEADING.removeprefix("## "),
            "reviewer_default": get_settings().reviewer_name,
            "reviewed": len(effective),
            "total": len(card_ids),
        },
    )


@router.post("/cards/{card_id}/verdict", response_class=HTMLResponse)
async def submit_verdict(
    card_id: int,
    decision: str = Form(...),
    rating: int = Form(...),
    comment: str = Form(default=""),
    reviewer_name: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Записать новую строку `verdicts` и вернуть фрагмент со счётчиком «N из M».

    Отказы — пустое имя, оценка вне 1–5, неизвестное решение — не создают
    строку в `verdicts` (инвариант №2 держится на непустом имени: обходить
    его пустой строкой нельзя).
    """
    card_row = await session.get(SolutionCardRow, card_id)
    if card_row is None:
        return _notice(CARD_NOT_FOUND.format(card_id=card_id), status_code=404)

    name = reviewer_name.strip()
    if not name:
        return _notice(REFUSAL_EMPTY_NAME, status_code=400)

    if not 1 <= rating <= 5:
        return _notice(REFUSAL_RATING_RANGE, status_code=400)

    try:
        decision_value = VerdictDecision(decision)
    except ValueError:
        return _notice(REFUSAL_UNKNOWN_DECISION, status_code=400)

    comment_text = comment.strip()
    session.add(
        VerdictRow(
            card_id=card_id,
            reviewer_name=name,
            decision=decision_value.value,
            rating=rating,
            comment=comment_text,
        )
    )
    await session.flush()

    request_id = card_row.request_id
    card_ids = await _load_card_ids(session, request_id)
    effective = await _load_effective(session, card_ids)

    if all_cards_reviewed(card_ids, effective):
        request_row = await session.get(RequestRow, request_id)
        # `exported` не понижается до `validated` (РЕШ-196 п. 4).
        if request_row is not None and request_row.status != RequestStatus.EXPORTED:
            request_row.status = RequestStatus.VALIDATED

    label = DECISION_LABELS.get(decision_value, decision_value.value)
    comment_suffix = f" — {escape(comment_text)}" if comment_text else ""
    fragment = (
        '<div class="notice mb-sm">'
        f"Вердикт записан: {label}, оценка {rating}/5, проверяющий {escape(name)}{comment_suffix}. "
        f"Проверено {len(effective)} из {len(card_ids)}."
        "</div>"
    )
    return HTMLResponse(content=fragment, status_code=200)


@router.post("/requests/{request_id}/summary", response_class=HTMLResponse)
async def edit_summary(
    request_id: int,
    recommendation: str = Form(...),
    caveats: str = Form(default=""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Записать правку рекомендации и оговорок сводки (РЕШ-206).

    Пишет ОДНУ строку `summary_overrides` на запрос: если строка уже есть,
    её поля перезаписываются, новая строка не создаётся — в отличие от
    `submit_verdict`, здесь нет ценности хранить историю правок текста.
    `caveats` собирается формой построчно (по образцу `risks`/`contact_leads`
    в `report.py::edit_card`) и разбирается в список до записи.

    Поле пишется как правка (`recommendation`/`caveats` строки, не `None`)
    только если оно действительно отличается от исходного текста отчёта —
    `resolve_summary_edit` сравнивает с текстом ДО правки сводки (после
    правок карточек, П-6, аудит Э4). Один клик по «Сохранить» без единого
    изменения формы больше не делает `summary_text_edited` истинным.
    """
    request_row = await session.get(RequestRow, request_id)
    if request_row is None:
        return _notice(REQUEST_NOT_FOUND.format(request_id=request_id), status_code=404)

    recommendation_text = recommendation.strip()
    if not recommendation_text:
        return _notice(REFUSAL_EMPTY_RECOMMENDATION, status_code=400)

    caveats_list = [line.strip() for line in caveats.splitlines() if line.strip()]

    try:
        original_recommendation, original_caveats = await _load_original_summary(session, request_id)
    except ReportNotFoundError:
        # Отчёта ещё нет (запрос не дошёл до готового результата) — сравнивать
        # правку не с чем, безопасный выбор — считать поля правкой как есть.
        original_recommendation, original_caveats = "", []

    resolved = resolve_summary_edit(
        original_recommendation=original_recommendation,
        original_caveats=original_caveats,
        submitted_recommendation=recommendation_text,
        submitted_caveats=caveats_list,
    )

    override_row = await _load_summary_override_row(session, request_id)
    if override_row is None:
        override_row = SummaryOverrideRow(request_id=request_id)
        session.add(override_row)
    override_row.recommendation = resolved.recommendation
    override_row.caveats = resolved.caveats
    await session.flush()

    caveats_suffix = f" Оговорки: {escape('; '.join(caveats_list))}." if caveats_list else " Оговорок нет."
    fragment = (
        '<div class="notice mb-sm">'
        f"Сводка сохранена. Рекомендация: {escape(recommendation_text)}.{caveats_suffix}"
        "</div>"
    )
    return HTMLResponse(content=fragment, status_code=200)


@router.post("/requests/{request_id}/analogs/{analog_key_value}/verdict", response_class=HTMLResponse)
async def submit_analog_verdict(
    request_id: int,
    analog_key_value: str,
    decision: str = Form(default=""),
    comment: str = Form(default=""),
    reviewer_name: str = Form(default=""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Записать новую строку `analog_verdicts` на аналог отчёта и вернуть фрагмент-подтверждение.

    Ключ не вида 16 hex, запрос без отчёта или ключ вне аналогов отчёта — 404;
    пустое имя и неизвестное решение — 400 (поля формы необязательны на уровне
    разбора, иначе FastAPI ответил бы 422); ни один отказ строку не пишет.
    Статус запроса и счётчик «проверено N из M» вердикт аналога не трогает.
    """
    if fullmatch(_ANALOG_KEY_PATTERN, analog_key_value) is None:
        return _notice(ANALOG_NOT_FOUND.format(request_id=request_id), status_code=404)
    try:
        report = await load_report(lambda: _session_scope(session), request_id)
    except ReportNotFoundError:
        return _notice(ANALOG_NOT_FOUND.format(request_id=request_id), status_code=404)
    if analog_key_value not in {analog_key(card) for card in report.analog_cards}:
        return _notice(ANALOG_NOT_FOUND.format(request_id=request_id), status_code=404)

    name = reviewer_name.strip()
    if not name:
        return _notice(REFUSAL_EMPTY_NAME, status_code=400)
    try:
        decision_value = VerdictDecision(decision)
    except ValueError:
        return _notice(REFUSAL_UNKNOWN_DECISION, status_code=400)

    comment_text = comment.strip()
    session.add(
        AnalogVerdictRow(
            request_id=request_id,
            analog_key=analog_key_value,
            reviewer_name=name,
            decision=decision_value.value,
            comment=comment_text,
        )
    )
    await session.flush()

    comment_suffix = f" — {escape(comment_text)}" if comment_text else ""
    fragment = (
        '<div class="notice mb-sm">'
        f"Вердикт по аналогу записан: {DECISION_LABELS[decision_value]}, проверяющий {escape(name)}{comment_suffix}."
        "</div>"
    )
    return HTMLResponse(content=fragment, status_code=200)
