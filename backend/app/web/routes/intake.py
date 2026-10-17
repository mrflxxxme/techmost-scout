"""Интейк и подтверждение брифа (задача И-10, SPEC Э3-2).

Живой поток вместо прежней заглушки нереализованной стадии. Весь сценарий интейка
(разбор текста, уточнение ответами, дозаполнение ограничений, стоп-кран)
идёт через общий модуль `app.pipeline.intake_flow` (РЕШ-184) — здесь только
веб-обёртка: форма вместо `typer.prompt`, HTML-фрагменты вместо консоли.

Черновик брифа сохраняется в `briefs` сразу после успешного разбора текста
(ещё до уточняющих вопросов) — иначе `POST /requests/{id}/brief` не из чего
было бы собрать бриф на форме без ответов (пустой список вопросов) или
восстановить контекст для повторного вызова `refine_brief` между двумя
независимыми HTTP-запросами, между которыми ничего, кроме БД, не переживает.

`POST /requests/{id}/brief` работает в двух состояниях по полю формы
`confirm` (РЕШ-190 п. 3):
- без `confirm` — форма пришла с ответами на уточняющие вопросы (или пустая,
  если вопросов не было): бриф собирается через `refine_brief_stage`,
  сохраняется черновиком (`confirmed=False`), статус — `briefing`;
- с `confirm` — форма пришла с отредактированными полями схемы брифа с
  экрана подтверждения: бриф собирается через `brief_from_form`,
  сохраняется подтверждённым (`confirmed=True`), ответ несёт кнопку
  «Начать поиск».

Важное поведение (ревью волны 1, задача И-9): `brief_store.save_brief`
намеренно не сбрасывает уже стоящий `confirmed_at`, когда `confirmed=False`.
Если оператор вернётся к форме ответов **после** того, как бриф уже был
подтверждён (например, кнопкой «Назад» в браузере), старая отметка
подтверждения в БД никуда не денется. Кнопка «Начать поиск» от этого не
протекает: она достаётся ответу ТОЛЬКО из ветки `confirm` этого маршрута
(`_confirm_context(confirmed=True, ...)`), а не из чтения `confirmed_at` из
базы, — поэтому застрявшая старая отметка не может незаметно предложить
старт по неподтверждённой на самом деле версии брифа. Проверено тестом
`test_stale_confirmation_does_not_leak_start_button`.

TRL из формы (РЕШ-185: своей колонки у `Brief` нет и не будет) валидируется
здесь, а не в `brief_store`: число 1–9 уходит в `brief_from_form`, любой
мусор («abc», «999») — не подставляется в `constraints`, оператор видит
предупреждение. `brief_store.brief_from_form` содержательно ничего не
проверяет — так и задумано в И-9, поэтому проверка встаёт сюда.

Обезличивание проверяется ДО первого платного вызова, а не после (РЕШ-193
п. 2, П-2 аудита Э3): регулярка `find_deidentification_risks` бесплатна и
идёт первой строкой `create_request`. Если она находит риск и форма не несёт
`accept_risks`, модель не вызывается вовсе — строка `requests` не заводится,
оператор видит предупреждение и кнопку «Отправить как есть»
(`_deidentification_warning.html`), которая повторяет тот же POST с
`accept_risks=1` и тем же текстом. Так держится обещание §2а «внешним API —
только обезличенный бриф»: сырой текст с похожим на название компании
фрагментом уходит в DeepSeek только после явного подтверждения оператора, а
не молча вместе с первым платным вызовом, как было раньше (предупреждение
показывалось только ПОСЛЕ разбора).
"""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING, Any, get_args

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.anonymize import find_deidentification_risks
from app.config import get_settings
from app.db import get_session
from app.export.markdown import CONSTRAINTS_UNRESOLVED_NOTICE, MATURITY_RU
from app.llm import CostLimitExceededError, LLMRouter
from app.models import Request as RequestRow
from app.models import RequestStatus
from app.pipeline.intake import SOLUTION_KIND_UNKNOWN
from app.pipeline.intake_flow import (
    refine_brief_stage,
    resolve_constraints_stage,
    run_intake_stage,
)
from app.schemas import Brief, IntakeResult, MaturityStage, SolutionKind
from app.web.brief_store import brief_from_form, load_brief, save_brief

if TYPE_CHECKING:  # только для аннотаций, см. докстроку app.pipeline.intake_flow
    from app.pipeline.persistence import StageRecord

log = logging.getLogger(__name__)
router = APIRouter()

#: Подсказка отрасли для интейка, когда её негде взять с формы (веб-форма
#: собирает только текст задачи) — тот же текст, что умолчание CLI
#: (`cli_run._run`), чтобы интейк вёл себя одинаково на обоих каналах.
_INDUSTRY_HINT_DEFAULT = "не указана"

#: Человеческий текст отказа при исчерпании стоп-крана (РЕШ-45, РЕШ-143 п. 2).
#: Новая клиентская фраза — см. список в handoff задачи И-10. Не начинается
#: со слова «Запрос»: `_failure_fragment` уже ставит перед сообщением
#: «Запрос №N: », и повтор слова читался бы неряшливо (ревью цикла 1).
COST_LIMIT_NOTICE = (
    "Достигнут лимит расходов на этот запрос — подбор остановлен. "
    "Попробуйте сократить текст задачи и отправить его снова."
)

#: Предупреждение обезличивания (РЕШ-189 п. 1, FR-4; порядок — РЕШ-193 п. 2,
#: П-2 аудита Э3): показывается ДО первого платного вызова и блокирует
#: отправку в модель, пока оператор не подтвердит кнопкой «Отправить как
#: есть» (`accept_risks`). Новая клиентская фраза — см. handoff.
DEIDENTIFICATION_WARNING_TEMPLATE = (
    "В тексте похоже на название компании или бренда: {fragments}. "
    "Рекомендуем заменить обезличенной формулировкой перед отправкой во внешнюю модель."
)

#: Предупреждение о некорректном TRL (число вне 1–9 или не число) — требование
#: не добавляется в бриф. Новая клиентская фраза — см. handoff.
TRL_INVALID_NOTICE = "Целевой TRL указан некорректно (ожидается число от 1 до 9) — требование не добавлено."

#: Опции выбора желаемой стадии зрелости на форме подтверждения — те же
#: подписи, что уже видит клиент в markdown-экспорте (`export/markdown.MATURITY_RU`,
#: не новая формулировка). `UNKNOWN` не предлагается: просить «неизвестную»
#: зрелость как целевую бессмысленно — это машинный признак отсутствия данных.
_MATURITY_OPTIONS = [(stage.value, MATURITY_RU[stage]) for stage in MaturityStage if stage != MaturityStage.UNKNOWN]


#: Допустимые значения поля «Вид решения» на форме новой задачи (Э6-1); `unknown` —
#: «определить по описанию». Любое иное значение читается как `research`.
_SOLUTION_KIND_CHOICES = (*get_args(SolutionKind), SOLUTION_KIND_UNKNOWN)


def _templates(request: Request):
    """Шаблоны берём из приложения, чтобы не плодить второй экземпляр Jinja."""
    from app.main import templates

    return templates


def _failure_fragment(message: str, *, request_id: int | None = None) -> HTMLResponse:
    """Красная плашка без Jinja: шаблона ошибки в списке файлов задачи нет,
    а сообщение короткое и не содержит пользовательской разметки. Номер
    запроса печатается тем же словом «Запрос №N», что и остальные фрагменты
    этого экрана — по нему оператор узнаёт, какая строка в истории отказала."""
    prefix = f"Запрос №{request_id}: " if request_id is not None else ""
    return HTMLResponse(f'<div class="notice danger appear">{html.escape(prefix + message)}</div>')


def _confirm_context(
    row: RequestRow,
    brief: Brief,
    *,
    confirmed: bool,
    caveats: list[str],
    trl_warning: str | None = None,
    target_trl_display: str = "",
) -> dict[str, Any]:
    return {
        "request_id": row.id,
        "brief": brief,
        "confirmed": confirmed,
        "caveats": caveats,
        "trl_warning": trl_warning,
        # Введённое значение TRL для повторного показа формы после
        # отклонённого ввода — как у остальных полей, которые берутся из
        # `brief` (ревью цикла 1): у TRL своего поля в `Brief` нет (РЕШ-185),
        # поэтому значение приходится нести отдельно.
        "target_trl_display": target_trl_display,
        "maturity_options": _MATURITY_OPTIONS,
    }


async def _fail_with_cost_limit(
    session: AsyncSession, row: RequestRow, llm_router: LLMRouter, exc: CostLimitExceededError
) -> HTMLResponse:
    """Стоп-кран (РЕШ-45): деньги на неудавшийся вызов уже потрачены, запрос
    останавливается статусом `failed` и понятным текстом — исключение здесь
    не пробрасывается наружу дальше (в отличие от CLI, веб обязан ответить
    HTML-фрагментом, а не уронить процесс).

    `CostTracker.add` прибавляет расход к счётчику ДО того, как поднимает
    исключение (`llm/router.py`), — деньги на упавший вызов уже потрачены.
    `llm_router.cost.spent_rub` на выходе уже включает всё, что стояло в
    строке до этой фазы (роутер создан с `initial_spent_rub=row.cost_rub`,
    РЕШ-193 п. 1), поэтому в `row.cost_rub` идёт присваивание, а не
    повторное сложение (ревью цикла 1, П-5 «деньги неудавшегося интейка не
    оставляют след» — докстрока `app.pipeline.intake_flow` требует этого
    прямо)."""
    row.status = RequestStatus.FAILED
    row.error = str(exc)
    row.cost_rub = llm_router.cost.spent_rub
    return _failure_fragment(COST_LIMIT_NOTICE, request_id=row.id)


@router.post("/requests", response_class=HTMLResponse)
async def create_request(
    request: Request,
    raw_text: str = Form(...),
    accept_risks: str | None = Form(None),
    solution_kind: str = Form("research"),
    session: AsyncSession = Depends(get_session),
):
    """Создать запрос и провести разбор текста интейком.

    Обезличивание — первая проверка, до создания строки `requests` и до
    любого платного вызова (см. докстроку модуля, РЕШ-193 п. 2): найденный
    риск без `accept_risks` в форме сразу отдаёт `_deidentification_warning.html`
    с кнопкой «Отправить как есть», строка `requests` не заводится.

    Дальше — успешный разбор — фрагмент с уточняющими вопросами
    (`_intake_questions.html`). Отказ `run_intake` — сразу фрагмент
    подтверждения брифа (`_brief_confirm.html`) с запасным брифом и оговоркой
    `INTAKE_FALLBACK_CAVEAT` (диалог провести не из чего, как в CLI).
    Исчерпание стоп-крана — красная плашка, запрос помечен `failed`.
    """
    text = raw_text.strip()
    kind = solution_kind if solution_kind in _SOLUTION_KIND_CHOICES else "research"
    risks = find_deidentification_risks(text)
    if risks and not accept_risks:
        # Регулярка бесплатна — риск проверяется раньше первого платного
        # вызова, и строки `requests` для отклонённой попытки не заводим
        # (проще, чем создавать и переиспользовать: истории не мусорим).
        warning = DEIDENTIFICATION_WARNING_TEMPLATE.format(fragments="; ".join(risks))
        return _templates(request).TemplateResponse(
            request=request,
            name="_deidentification_warning.html",
            context={"raw_text": text, "warning": warning, "solution_kind": kind},
        )

    row = RequestRow(raw_text=text, status=RequestStatus.INTAKE)
    session.add(row)
    await session.flush()

    llm_router = LLMRouter(get_settings(), initial_spent_rub=row.cost_rub or 0.0)
    stage_log: list[StageRecord] = []

    try:
        outcome = await run_intake_stage(
            llm_router,
            text,
            industry_hint=_INDUSTRY_HINT_DEFAULT,
            session_factory=None,
            stage_log=stage_log,
            solution_kind=kind,
        )
    except CostLimitExceededError as exc:
        return await _fail_with_cost_limit(session, row, llm_router, exc)

    if outcome.intake is None:
        # Отказ разбора: черновика для диалога нет, бриф уже собран запасным
        # путём — идём сразу к подтверждению, как CLI в немом режиме.
        try:
            brief, unresolved = await resolve_constraints_stage(
                llm_router,
                outcome.brief,
                text,
                industry_hint=_INDUSTRY_HINT_DEFAULT,
                session_factory=None,
                stage_log=stage_log,
            )
        except CostLimitExceededError as exc:
            return await _fail_with_cost_limit(session, row, llm_router, exc)

        caveats = list(outcome.caveats)
        if unresolved:
            caveats.append(CONSTRAINTS_UNRESOLVED_NOTICE)

        row.status = RequestStatus.BRIEFING
        row.cost_rub = llm_router.cost.spent_rub
        await save_brief(session, row.id, brief, confirmed=False)
        return _templates(request).TemplateResponse(
            request=request,
            name="_brief_confirm.html",
            context=_confirm_context(row, brief, confirmed=False, caveats=caveats),
        )

    row.cost_rub = llm_router.cost.spent_rub
    # Черновик сохраняется сразу — POST /brief читает его обратно и не должен
    # заново звать платный run_intake, чтобы восстановить контекст диалога.
    await save_brief(session, row.id, outcome.intake.draft_brief, confirmed=False)
    return _templates(request).TemplateResponse(
        request=request,
        name="_intake_questions.html",
        context={
            "request_id": row.id,
            "questions": outcome.intake.questions,
        },
    )


async def _draft_brief_from_answers(request: Request, session: AsyncSession, row: RequestRow, form) -> HTMLResponse:
    """Ветка `POST /brief` без `confirm`: собрать бриф из ответов на вопросы."""
    draft = await load_brief(session, row.id)
    if draft is None:
        return _failure_fragment("черновик брифа не найден — опишите задачу заново.", request_id=row.id)

    questions = form.getlist("question")
    answers_raw = form.getlist("answer")
    # Пустой ответ читается как «не знаю» — тот же принцип, что у умолчания
    # `typer.prompt` в CLI (FR-2: ответ «не знаю» допустим на любой вопрос).
    answers = {q: (a.strip() or "не знаю") for q, a in zip(questions, answers_raw, strict=False) if q.strip()}

    llm_router = LLMRouter(get_settings(), initial_spent_rub=row.cost_rub or 0.0)
    stage_log: list[StageRecord] = []
    caveats: list[str] = []

    try:
        if answers:
            intake_stub = IntakeResult(questions=[], draft_brief=draft)
            outcome = await refine_brief_stage(
                llm_router,
                row.raw_text,
                intake_stub,
                answers,
                industry_hint=_INDUSTRY_HINT_DEFAULT,
                session_factory=None,
                stage_log=stage_log,
                solution_kind=draft.solution_kind,
            )
            brief = outcome.brief
            caveats.extend(outcome.caveats)
        else:
            # Вопросов не было или все оставлены пустыми — тот же случай, что
            # у CLI без интерактивного диалога: бриф остаётся черновиком разбора.
            brief = draft

        brief, unresolved = await resolve_constraints_stage(
            llm_router,
            brief,
            row.raw_text,
            industry_hint=_INDUSTRY_HINT_DEFAULT,
            session_factory=None,
            stage_log=stage_log,
        )
    except CostLimitExceededError as exc:
        return await _fail_with_cost_limit(session, row, llm_router, exc)

    if unresolved:
        caveats.append(CONSTRAINTS_UNRESOLVED_NOTICE)

    row.status = RequestStatus.BRIEFING
    row.cost_rub = llm_router.cost.spent_rub
    await save_brief(session, row.id, brief, confirmed=False)

    return _templates(request).TemplateResponse(
        request=request,
        name="_brief_confirm.html",
        context=_confirm_context(row, brief, confirmed=False, caveats=caveats),
    )


def _validate_target_trl(raw: str | None) -> tuple[str | None, str | None]:
    """Проверить TRL из формы: число 1–9 или предупреждение, без исключения.

    Возвращает `(значение_для_fields, предупреждение)` — ровно одно из двух
    непусто. Мусорный ввод («abc», «999») не должен уехать строкой в
    `constraints` (ревью волны 1) — проверка стоит здесь, а не в
    `brief_store.brief_from_form`, которая ничего не валидирует по контракту
    задачи И-9.
    """
    trl_raw = (raw or "").strip()
    if not trl_raw:
        return None, None
    if trl_raw.isdigit() and 1 <= int(trl_raw) <= 9:
        return trl_raw, None
    return None, TRL_INVALID_NOTICE


async def _confirm_brief(request: Request, session: AsyncSession, row: RequestRow, form) -> HTMLResponse:
    """Ветка `POST /brief` с `confirm`: сохранить правки и подтвердить бриф.

    Мусорный TRL не топит подтверждение целиком, но и не проходит молча:
    оператор остаётся на редактируемой форме (`confirmed=False`) с
    сохранёнными остальными полями и предупреждением, чтобы поправить
    именно TRL, не перепечатывая всё заново (ревью цикла 1) — введённое
    значение возвращается в поле тем же способом, что и у остальных полей.
    """
    trl_raw = (form.get("target_trl") or "").strip()
    target_trl, trl_warning = _validate_target_trl(trl_raw)

    fields: dict[str, Any] = {
        "industry": form.get("industry", ""),
        "task_summary": form.get("task_summary", ""),
        "constraints": form.get("constraints", ""),
        "geography": form.get("geography") or "РФ",
        "budget_hint": form.get("budget_hint") or None,
        "timeline_hint": form.get("timeline_hint") or None,
        "keywords_ru": form.get("keywords_ru", ""),
        "keywords_en": form.get("keywords_en", ""),
        "target_maturity": form.get("target_maturity") or None,
        "owner_answers": [
            {"question": q, "answer": a}
            for q, a in zip(form.getlist("owner_answer_question"), form.getlist("owner_answer_answer"), strict=False)
        ],
    }
    if target_trl is not None:
        fields["target_trl"] = target_trl

    # Вид решения на форме подтверждения не правится: берётся из сохранённого черновика.
    saved = await load_brief(session, row.id)
    fields["solution_kind"] = saved.solution_kind if saved is not None else "research"

    brief = brief_from_form(fields)
    row.status = RequestStatus.BRIEFING

    if trl_warning:
        await save_brief(session, row.id, brief, confirmed=False)
        return _templates(request).TemplateResponse(
            request=request,
            name="_brief_confirm.html",
            context=_confirm_context(
                row, brief, confirmed=False, caveats=[], trl_warning=trl_warning, target_trl_display=trl_raw
            ),
        )

    await save_brief(session, row.id, brief, confirmed=True)
    return _templates(request).TemplateResponse(
        request=request,
        name="_brief_confirm.html",
        context=_confirm_context(row, brief, confirmed=True, caveats=[]),
    )


@router.post("/requests/{request_id}/brief", response_class=HTMLResponse)
async def build_brief(
    request: Request,
    request_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Собрать (без `confirm`) или подтвердить (с `confirm`) бриф запроса.

    Два состояния одного маршрута различаются присутствием поля формы
    `confirm` (РЕШ-190 п. 3) — см. докстроку модуля про то, откуда берётся
    каждое из двух тел формы.
    """
    row = await session.get(RequestRow, request_id)
    if row is None:
        return _failure_fragment("не найден.", request_id=request_id)

    form = await request.form()
    if form.get("confirm"):
        return await _confirm_brief(request, session, row, form)
    return await _draft_brief_from_answers(request, session, row, form)
