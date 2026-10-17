# План этапа 6 — «Рынок»: вид решения, аналоги, фильтр стран, экспресс-скан

Закреплён планировщиком 17 октября 2026. Прежние планы: `.planning/plans/PLAN-E5.md`,
`.planning/plans/PLAN-E6-API.md` (PR #15). План не переписывается по ходу этапа:
изменение — решение владельца и запись в `DECISIONS.md` (§6, §9).

**Основание.** `docs/SPEC.md` §7а (Э6-1…Э6-6), §2а «Вне скоупа MVP», РЕШ-238…265
(грилл закрыт — развилки данность). Волна собирает всё, что делается **без живых
ключей моделей**: внешние вызовы в тестах — respx и заглушки роутера (инв. 7);
живой скан — отложенность DLG-Э6-1 с действием владельца.

## Состав и что вынесено

| Задача | Что | Цикл (РЕШ-244) | Волна |
|---|---|---|---|
| Э6-1 | `solution_kind` в брифе + все схемные контракты этапа | полный | 1 |
| Э6-3 | Справочник 430-р и функция трёх состояний страны | полный | 1 |
| Э6-2а | Слой аналогов в пайплайне, `planner.md`, `analog_scorer.md` | полный | 2 |
| Э6-4 | Секция «Что делают в мире» во всех выходах | лёгкий | 2 |
| Э6-6а | Настройки скана, токен, лимиты, роутинг по Host | полный | 2 |
| Э6-2б | Адаптеры Wikidata и каталога YC | полный | 3 |
| Э6-2в | Яндекс-поиск аналогов по белому списку доменов | полный | 3 |
| Э6-6б | Автосверка фактов и вид скана (плашка, «без контактов») | полный | 3 |
| Э6-6в | Публичная форма, фоновый скан, страница, JSON, заявка | полный | 4 |

**Вынесено из волны:**
* **Э6-5, остаток** — `LeadEvent` и подписанная ссылка `LaunchContext`: в репозитории
  нет адреса приёмника, секрета и алгоритма подписи, схемы `LeadEvent` — это
  артефакты бота экосистемы (W2, РЕШ-250). «Следующий шаг — один на экран»
  закрывается в Э6-6в единственной кнопкой «Заказать полный отчёт».
* **РЕШ-261** (Product Radar, реестр ПО Минцифры, ГИСП) — отдельная задача
  следующей волны: способ доступа к реестрам выбирается «по факту проверки», а
  живой проверки в этой волне нет.
* **Поштучный вердикт эксперта на аналог** — долг следующей волны (Э-3).
* Product Hunt API — нет (РЕШ-256); капча — нет (РЕШ-265).

## Общие правила этапа (в задачах не повторяются)

1. **Пределы циклов (§4).** Полный: ревью 2, правки в аудите 1, починка тестов 3.
   Лёгкий (Э6-4): ревью 1, верификация, ADR строкой в `DECISIONS.md`, починка
   тестов 3; аудит — в составе аудита этапа. Дальше — стоп и разбор.
2. **Ветки и PR.** Волна — ветка `claude/e6-wN` от ветки предыдущей волны (W1 — от
   `claude/festive-babbage-ff9874`). Задача — своя ветка от `claude/e6-wN`, вливается
   в неё после ревью и верификации; конфликт разбирает оркестратор (§11). Одна PR
   на волну (`claude/e6-wN` → ветка предыдущей волны), стопкой.
3. **Общие критерии каждой задачи.** Перед волной оркестратор фиксирует базу
   passed (на PR #15 — 1381). `pytest -q` — 0 failed, passed ≥ база + новые;
   `ruff check . && ruff format --check .` (ruff из `.venv`, РЕШ-234) — 0 замечаний;
   прогон в контейнере `python:3.12-slim` (РЕШ-249).
4. **Прежние тесты не правятся**, кроме названных в задаче: неизменность «Науки»
   доказывается тем, что её тесты зелёные без правок. Проверка:
   `git diff --stat claude/e6-wN -- tests/` — только новые файлы и названные.
   Фикстуры — внутри тестовых файлов, как принято в наборе.
5. **500 строк** в `backend/app` — `tests/test_backend_layout.py` (исключений нет).
   У предела: `markdown.py` 440, `run_stages.py` 430, `pipeline_search.py` 423 —
   новое уходит в новые модули. Новые тестовые файлы — до 500 строк.
6. **Инв. 6:** `rg -ln httpx backend/app | rg -v "backend/app/(llm/|search/|cli_doctor\.py$|pipeline/pipeline_context\.py$|pipeline/pipeline_search\.py$)"`
   — пусто (на базе пусто). Секретов нет ни в коде, ни в фикстурах, ни в выводе.
7. **Тексты клиенту — на утверждение владельцу в PR** (§9 п. 4): форма, кнопка,
   сообщения отказа, подписи секций, промпты, белый список доменов, редакция 430-р.
   Исполнитель выносит их списком в описание PR.
8. **Новых зависимостей нет** (pyyaml, httpx, python-multipart есть). Сутки —
   фиксированный UTC+3, не `zoneinfo`: в `python:3.12-slim` нет `tzdata`.
9. **Коммит до handoff** (§13а): handoff — `.planning/evidence/Э6-<задача>-handoff.yaml`
   тем же коммитом; вывод проверок — `.planning/evidence/Э6-<задача>-<что>.txt`.
10. **Крупное не читать целиком** (§13): `SPEC.md`, `DECISIONS.md`, длинные тесты —
    поиском; в «Читать первым» названы места.

## Что есть на базе (проверено 17.10.2026)

* `Brief.region_code/need_id/mode` — `exclude=True` (`schemas.py:122–132`): **в
  `report_json` не попадают**, живут в колонках `briefs`, `export/json.py:130–135`
  накладывает их сам. Потребитель `mode` после чтения отчёта из базы без наложения
  получит `business` — ловушка для фильтра стран.
* Образец секции вне лимита — `contest_cards`: `split_contest_candidates`
  (`pipeline_selection_flow.py:35`) зовётся в `run_stages.py:162` и `:179`; секция
  собирается последней (`run_stages.py:358–394`), стоп-кран не перехватывается.
  `COVERAGE_EXCLUDED_SOURCE_TYPES = {CONTEST}` — `extraction.py:322`.
* `tests/test_plan_types.py` держит соответствие **в обе стороны**: тип правила 1
  `planner.md` без адаптера и адаптер без типа — оба красные. Тип `analog` и первый
  адаптер с `ANALOG` обязаны войти одной задачей.
* Сужение `site:` — `yandex_parse.with_site_filter`, площадки — кортежи в коде;
  предел 6 платных запросов на прогон, пропуски — `limit_problems`. Поиск Яндекса в
  `cost_rub` не входит (там только модели).
* Стадия без маршрута в `LLM_STAGE_ROUTES` идёт по `LLM_FALLBACK_ORDER` (`router.chain_for`).
* **Автосверки фактов с текстом источника в коде нет.** Есть LLM-стадия `verifier`
  (сверяет карточку с текстами группы → `unsourced_claims`) и проверка «`evidence`
  непуст». Сверки ссылки факта со ссылками кандидатов и содержимого с текстом нет.
* `_active_runs` — один прогон на систему (РЕШ-147). `data/` в контейнере — том, новые
  файлы образа на него не доезжают (ловушка Э5). `.env.example` закрыт правами
  среды (DLG-2 Э5) — новые переменные описываются в `docs/deploy.md`.
* Грепом на базе пусто: `"TW"|'TW'|430` в `backend/app`; `captcha|zoneinfo` в
  `backend/`, `tests/`; `analog` во всём `prompts/`; `/scan` в `backend/`;
  `194\.87` в `docs/deploy.md`.

## Контракты, закреплённые планом

**Схемы (`schemas.py`, только Э6-1):**
```python
SolutionKind = Literal["research", "product", "digital", "business_model"]
MARKET_KINDS = ("product", "digital", "business_model")
Brief.solution_kind: SolutionKind = Field(default="research", exclude=True)
SourceType.ANALOG = "analog"
Candidate.country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")  # ISO 3166-1 alpha-2
class AnalogCard(BaseModel):
    title: str; essence: str
    country: str = Field(pattern=r"^[A-Z]{2}$"); country_evidence_url: HttpUrl
    business_model: str; traction: str = ""        # пусто — «не найдено»
    transferability_ru: str; what_to_do_in_ru: str
    evidence: list[Evidence] = []; candidate_urls: list[HttpUrl] = []  # ссылки — кодом
Report.analog_cards: list[AnalogCard] = Field(default_factory=list)
```
**Страна (`backend/app/countries.py`, Э6-3):** `CountryStatus`: `main | footnote |
excluded`; `country_status(code, mode) -> CountryStatus`: `business` → `main` для
всех; `gov` → `excluded` для `gov_excluded` (Тайвань), иначе `footnote` для перечня
430-р, иначе `main`. Исключение сильнее сноски (Тайвань в 430-р есть). Код не
`^[A-Z]{2}$` или неизвестный режим — `ValueError`. Потребителей ровно три: отбор
аналогов (Э6-2а), `export/analogs.analog_sections` (Э6-4), вид скана через неё (Э6-6б).

**Выгрузки (`export/analogs.py`, Э6-4):** `analog_sections(report) ->
AnalogSections(main, footnotes)`, режим — `report.brief.mode`; аналог без `evidence`
и `excluded` не печатается нигде. `build_export_view` накладывает поля `briefs`
(`ECOSYSTEM_BRIEF_FIELDS` + `solution_kind`) на бриф `report` и `client_report` —
одна точка; страница, PDF, DOCX, JSON читают `view.analog_sections`, markdown зовёт
`analog_sections(report)`.

**Пути скана:** `GET /scan` форма · `POST /scan` запуск · `GET /scan/{token}` страница ·
`GET /scan/{token}/status` прогресс · `GET /scan/{token}.json` JSON. Токен —
`secrets.token_urlsafe(32)` в `requests.scan_token` (nullable; не `NULL` — скан).

**Роутинг по Host (Э6-6а):** `Host` без порта, нижний регистр. `PUBLIC_SCAN_HOST`
пуст — `/scan*` 404 везде. Публичный хост: `/` → 302 на `/scan`; разрешены `/scan*`,
`/static/*`, `/api/v1/*` (под ключом, РЕШ-262), `/health`; остальное — 404 **до**
проверки пароля. Рабочий хост: `/scan*` — 404.

**Настройки (`config.py`, только Э6-6а), умолчания РЕШ-258:** `SCAN_LIMIT_PER_IP_PER_DAY=2`,
`SCAN_DAILY_BUDGET_RUB=300`, `SCAN_COST_LIMIT_RUB=10`, `PUBLIC_SCAN_HOST=""`,
`SCAN_ORDER_TELEGRAM_URL=""`, `SCAN_ORDER_EMAIL=""`, `YANDEX_SEARCH_RUB_PER_QUERY`
(по тарифу Yandex Search API, значение и ссылка — в PR; см. Э-1).

**Вид скана (`web/scan_view.py`, Э6-6б):** `SCAN_BANNER = "Автоматический обзор, не
проверен экспертом"`; `ScanView` (frozen) — плашка **свойство** класса, не аргумент;
`scan_payload(view)` всегда несёт `banner`, `kind: "express_scan"`, `schema_version`.
До 3 карточек и до 3 аналогов, только прошедшие автосверку; пусто — «не найдено».

## Волны и параллельность

| Волна | Исполнитель A | Исполнитель B | Исполнитель C |
|---|---|---|---|
| 1 | Э6-1 | Э6-3 | — |
| 2 | Э6-2а | Э6-4 | Э6-6а |
| 3 | Э6-2б | Э6-2в | Э6-6б |
| 4 | Э6-6в | — | — |
| ворота | оркестратор: `.planning/gates/Э6.md`, DLG-Э6-1/2 владельца | | |

Один владелец общего контракта в волне: `schemas.py` — Э6-1; `config.py` — Э6-6а;
`prompts/*` и `tests/test_prompts.py` — Э6-1 (W1), Э6-2а (W2); `models.py`/`db.py` —
Э6-1 (W1), Э6-6а (W2); `main.py` — Э6-6а (W2), Э6-6в (W4). Файлы по волнам — в
сводке в конце; внутри волн пересечений нет.

---

## Волна 1

### Э6-1 · Вид решения в брифе и схемные контракты этапа
- **A · полный · зависимостей нет · sequential** (единственный владелец `schemas.py`)
- **Файлы:** `backend/app/{schemas,models,db}.py`, `backend/app/web/brief_store.py`,
  `backend/app/pipeline/{intake,constraints_guard,intake_flow}.py`,
  `backend/app/web/routes/intake.py`, `backend/app/web/templates/index.html`,
  `prompts/intake.md`, `prompts/CHANGELOG.md`, `tests/test_prompts.py`,
  `tests/test_solution_kind.py`, `tests/test_schemas_e6.py` (новые)
- **Контракт:**
  1. Все типы раздела «Схемы» — разом; поведения аналогов задача не вводит.
  2. Метка `{{SOLUTION_KIND}}` в `intake.md` после блоков инструкций; её заполняют все
     три вызова `render_prompt("intake", …)` (`intake.py:40`, `:66`,
     `constraints_guard.py:177`); параметр `solution_kind="research"` по цепочке
     `run_intake_stage` → `run_intake`/`refine_brief`. Значения: четыре вида и `unknown`.
  3. Выбор человека сильнее модели: `resolve_solution_kind(selected, model_value)`:
     `selected ≠ unknown` → `selected`; `unknown` → значение модели из `MARKET_KINDS`,
     иначе `product`; `research` от модели при `unknown` не принимается.
  4. Колонка `briefs.solution_kind` (nullable, догон в `db.init_db`), `NULL` —
     `research`; `save_brief`/`load_brief` пишут и читают поле.
  5. Рабочее место: выбор вида на форме новой задачи, умолчание «Наука»; без поля —
     `research`. Подписи — на утверждение.
- **Критерии приёмки:**
  1. `pytest tests/test_solution_kind.py tests/test_schemas_e6.py -v` — проходят; есть
     тесты: `"solution_kind"` нет в `Brief(...).model_dump_json()`; `resolve_solution_kind`
     на пяти входах; круг `save_brief`→`load_brief` на четырёх видах; база без колонки
     догоняется и читает `research`; старый JSON отчёта без `analog_cards` читается с
     `[]`; `Candidate.country`/`AnalogCard.country` отвергают `"us"`, `"USA"`, `""`.
  2. `pytest tests/test_prompts.py -v` — проходит; `EXPECTED_PLACEHOLDERS["intake"] ==
     {"RAW_TASK_TEXT", "INDUSTRY_HINT", "SOLUTION_KIND"}`; версия файла = запись `CHANGELOG`.
  3. Мутация «`resolve_solution_kind` всегда отдаёт значение модели» →
     `pytest tests/test_solution_kind.py -q` — ≥1 failed.
  4. `rg -n "solution_kind" prompts/planner.md prompts/card_author.md prompts/verifier.md prompts/summary.md`
     — пусто (входы стадий «Науки» не меняются; на базе пусто).
  5. Правило 4: правится только `tests/test_prompts.py`. Общие критерии.
- **Инварианты:** №4 (кр. 2), №8 (кр. 1, 3), №7 (заглушки роутера), №9 (публичной
  формы и режима «Гос» на форме нет — `mode` приходит из `LaunchContext`).
- **Читать первым:** `schemas.py:20–160`, `brief_store.py:120–189`, `db.py:30–70`,
  `pipeline/intake.py`, `tests/test_prompts.py:1–80`.

### Э6-3 · Справочник перечня 430-р и три состояния страны
- **B · полный · зависимостей нет · parallel** с Э6-1
- **Файлы:** `backend/app/countries.py`, `backend/app/reference/countries_gov.yaml`,
  `tests/test_countries.py` (все новые)
- **Контракт:** раздел «Страна». YAML: `source_url` (официальная публикация № 430-р с
  изменениями), `edition` (дата последней учтённой редакции), `checked_at`,
  `unfriendly_430r: [{code, name_ru}]` (с территориями, которые перечень называет
  отдельно), `gov_excluded: [{code: "TW", reason: "РЕШ-241, РЕШ-260"}]`. Коды — строки
  в кавычках; загрузка — `lru_cache`, путь от `Path(__file__)`.
- **Критерии приёмки:**
  1. `pytest tests/test_countries.py -v` — проходят: `business` → `main` для `US`, `TW`,
     `CN`; `gov`: `CN` → `main`, `US`/`DE`/`JP`/`KR`/`GB` → `footnote`, `TW` →
     `excluded`, хотя `TW` есть в `unfriendly_430r`; `ValueError` на `"us"`, `"USA"`,
     режиме `"world"`; каждый код — `str` и `^[A-Z]{2}$` (ловушка YAML: `NO` → `False`);
     дублей нет; все 27 стран ЕС в перечне.
  2. `rg -n "\"TW\"|'TW'" backend/app` — пусто (Тайвань только в справочнике).
  3. Мутации «проверка сноски раньше исключения» и «`gov` → всегда `main`» — каждая
     ≥1 failed в `pytest tests/test_countries.py -q`. Общие критерии.
- **Инварианты:** №10 (кр. 1), №8 (кр. 1, 3), №7, №1 по духу (источник и дата сверки).
- **Границы:** потребителей не подключает; редакцию утверждает владелец в PR.
- **Читать первым:** `DECISIONS.md` РЕШ-241, 259, 260; `schemas.py:26–27`.

---

## Волна 2

### Э6-2а · Слой аналогов в пайплайне
- **A · полный · после Э6-1, Э6-3 · sequential** по промптам
- **Файлы:** `backend/app/pipeline/{analogs,run_analogs}.py` (новые),
  `backend/app/pipeline/{run_stages,orchestrator,stages,planning,pipeline_selection_flow,extraction,demo}.py`,
  `backend/app/search/yandex_search.py` (строка `source_types`), `prompts/planner.md`,
  `prompts/analog_scorer.md` (новый), `prompts/CHANGELOG.md`, `tests/test_prompts.py`,
  `tests/test_plan_types.py`, `tests/test_analog_stage.py`, `tests/test_analog_flow.py` (новые)
- **Контракт:**
  1. `planner.md`: метка `{{SOLUTION_KIND}}`; отдельное правило (не правило 1): при
     `SOLUTION_KIND ≠ research` ровно 2 формулировки `analog`, `lang: "en"`, план ≤ 12.
     `test_plan_types.py` учитывает это правило; `YandexSearchAdapter.source_types`
     получает `ANALOG` — тип достижим. `planning.py` снимает `analog` при `research`
     кодом, не полагаясь на модель.
  2. `split_analog_candidates(scored) -> (прочие, аналоги)` — перед
     `split_contest_candidates` в обоих местах; `ANALOG_CARDS_TARGET = 3`; `ANALOG` в
     `COVERAGE_EXCLUDED_SOURCE_TYPES`.
  3. `analogs.py` (чистые функции): `prefilter_analog_candidates(scored, mode)` —
     снимает кандидатов с известной страной `excluded` **до** вызова модели;
     `author_analog_card(router, brief, group)` — промпт `analog_scorer` (`BRIEF_JSON`,
     `ANALOG_CANDIDATES_JSON`, свой сериализатор со страной; `candidates_to_json` не
     меняется), `candidate_urls` — кодом, структурная страна группы перекрывает страну
     модели (расхождение — не публикуется); `select_publishable_analogs(cards, mode,
     limit)` — непустой `evidence`, `country_evidence_url` ∈ `candidate_urls`, статус
     ≠ `excluded`.
  4. `run_analogs.py`: секция последней, после конкурсной; строка журнала
     `analog_cards`; при `research` — ни одного вызова модели; стоп-кран не
     перехватывается (РЕШ-45); отбор — `_cut_by_relevance` и `group_for_cards`.
     Сводка получает только основные карточки.
  5. `analog_scorer.md` 1.0: каждый факт — в `evidence`; переносимость в РФ (санкции,
     регуляторика, игрок в РФ) — факт только со ссылкой, иначе «не установлено»;
     «что сделать в РФ» — совет без фактов. Текст — на утверждение.
- **Критерии приёмки:**
  1. `pytest tests/test_analog_stage.py tests/test_analog_flow.py -v` — проходят
     (заглушки, без сети): прогон `research` — нет вызова `analog_scorer`,
     `analog_cards == []`, покрытие прежнее; план `research` с `analog` от модели — после
     `planning` формулировок `analog` нет; прогон `product` — до 3 аналогов вне `cards`,
     `target_cards` не задет; кандидат `TW` при `gov` не доходит до модели; `TW` от
     модели по кандидату без страны — при `gov` не публикуется; вход сводки без аналогов.
  2. `pytest tests/test_prompts.py tests/test_plan_types.py -v` — проходят;
     `planner` = `{"BRIEF_JSON", "SOLUTION_KIND"}`, `analog_scorer` =
     `{"BRIEF_JSON", "ANALOG_CANDIDATES_JSON"}`.
  3. Мутации — каждая ≥1 failed в `pytest tests/test_analog_flow.py -q`: убрать вызов
     `prefilter_analog_candidates`; убрать проверку статуса в отборе; убрать снятие
     `analog` при `research`; убрать `ANALOG` из `COVERAGE_EXCLUDED_SOURCE_TYPES`.
  4. `rg -n "analog" prompts/card_author.md prompts/verifier.md prompts/summary.md prompts/scorer.md prompts/intake.md`
     — пусто (на базе пусто).
  5. `pytest tests/test_backend_layout.py -q` — проходит (`run_stages.py` ≤ 500).
  6. Правило 4: правятся только `test_prompts.py`, `test_plan_types.py`. Общие критерии.
- **Инварианты:** №1 (кр. 1), №4 (кр. 2), №5 (отказ адаптера — через `_safe_search`),
  №6 (модель — только `LLMRouter`), №7, №8 (кр. 1, 3), №9.
- **Границы:** адаптеры и белый список — Э6-2б/в; `config.py` не трогает (маршрут —
  по `LLM_FALLBACK_ORDER`); выгрузки — Э6-4; `replanner.md` не меняет.
- **Читать первым:** `run_stages.py:150–200, 355–410`, `pipeline_selection_flow.py`,
  `cards.py:44–76`, `tests/test_plan_types.py`, `prompts/planner.md:15–30`.

### Э6-4 · Секция «Что делают в мире» во всех выходах
- **B · лёгкий · после Э6-1, Э6-3 · parallel**
- **Файлы:** `backend/app/export/analogs.py` (новый),
  `backend/app/export/{view,markdown,pdf,docx,json}.py`,
  `backend/app/web/routes/report.py`, `backend/app/web/templates/{report,report_print}.html`,
  `backend/app/web/templates/_analog_card.html` (новый), `tests/test_export_analogs.py` (новый)
- **Контракт:** раздел «Выгрузки». Секция — после основных и конкурсных карточек, вне
  лимита 5–7; сноска — одна строка: название, страна, ссылка. JSON: `analog_cards:
  {main: [...], footnotes: [{title, country, url}]}`, `schema_version` остаётся
  `"1.0"`; `json.py` берёт поля брифа из вида — свой второй путь удаляется. Заголовок
  «Что делают в мире» задан SPEC, прочие подписи — на утверждение.
- **Критерии приёмки:**
  1. `pytest tests/test_export_analogs.py -v` — проходят. Фикстура: отчёт с аналогами
     `CN`, `US`, `TW` и одним без `evidence`, записан в базу, `briefs.mode = gov`. Для
     страницы отчёта, markdown, `render_print_html`, текста DOCX и JSON: `CN` — в
     секции, `US` — только в сносках, `TW` и бессылочный — нет в тексте вовсе. Тот же
     отчёт при `business`: все три со ссылками в секции.
  2. Мутации — каждая ≥1 failed в `pytest tests/test_export_analogs.py -q`: убрать
     наложение полей `briefs` в `build_export_view`; в любом одном выходе печатать
     `report.analog_cards` вместо `analog_sections`; `analog_sections` → всё в `main`.
  3. `rg -n "country_status" backend/app/export` — только `export/analogs.py`.
  4. `pytest tests/test_backend_layout.py -q` — проходит (`markdown.py` ≤ 500).
  5. Правило 4; JSON прежних отчётов несёт пустые `main`/`footnotes`. Общие критерии.
- **Инварианты:** №1 (кр. 1), №2 (замок черновика РЕШ-199 не тронут), №7, №8.
- **Границы:** отбор и стадию не трогает; экран ревью не меняет. По рабочему вопросу
  РЕШ-244 задача печатает факты аналогов клиенту — видимость закрыта кр. 1–2, задача
  входит в аудит этапа.
- **Читать первым:** `export/view.py:40–212`, `export/json.py`, `markdown.py:231–325`,
  `pdf.py:66–140`, `docx.py:46–106`, `verdicts.py:73–115`.

### Э6-6а · Настройки скана, токен, лимиты, роутинг по Host
- **C · полный · после Э6-1 · parallel**
- **Файлы:** `backend/app/{config,models,db,main}.py`,
  `backend/app/web/{host_routing,scan_limits}.py` (новые),
  `tests/test_host_routing.py`, `tests/test_scan_limits.py` (новые)
- **Контракт:** разделы «Настройки», «Роутинг по Host», «Пути», плюс:
  1. `host_routing.py` — чистая ASGI-прослойка; `build_app(app_env, *,
     public_scan_host="")` её подключает.
  2. `scan_limits.py`: `client_ip(request)` — последний адрес `X-Forwarded-For` только
     при соединении с частного адреса (наш прокси), иначе адрес соединения;
     `IpDailyCounter` — в памяти, ключ (сутки UTC+3, IP), `check`/`record` раздельно;
     `admit_scan(session, ip, now) -> ScanAdmission` (`ok | ip_limit |
     budget_exhausted`). Потрачено за сутки = `cost_rub` сканов за сутки UTC+3 (+ поиск
     Яндекса по Э-1) + резерв `SCAN_COST_LIMIT_RUB` на каждый незавершённый скан;
     допуск — если остаток ≥ `SCAN_COST_LIMIT_RUB`. IP не пишется ни в базу, ни в лог.
  3. `requests.scan_token` (`String(64)`, nullable), догон в `init_db`.
- **Критерии приёмки:**
  1. `pytest tests/test_host_routing.py -v` — проходят: публичный хост — `/request/1`,
     `/history`, `/requests/1/report` → 404 **без** `Authorization` (не 401); `/` → 302
     на `/scan`; `/health` 200; `/api/v1/requests/1` без ключа — 401, как сегодня; хост
     с портом и в верхнем регистре распознан; рабочий хост — `/scan` 404;
     `PUBLIC_SCAN_HOST` пуст — `/scan` 404 везде, рабочие пути прежние.
  2. `pytest tests/test_scan_limits.py -v` — проходят: третий скан с IP за сутки —
     `ip_limit`, в следующие сутки UTC+3 — `ok`; потрачено 291 ₽ — отказ, 285 ₽ —
     допуск; 2 незавершённых скана резервируют 20 ₽; поддельный `X-Forwarded-For` с
     публичного адреса соединения не меняет IP; `caplog` без строки IP; умолчания
     настроек — по РЕШ-258.
  3. Мутации — каждая ≥1 failed: убрать резерв незавершённых; доверять
     `X-Forwarded-For` всегда; пропускать рабочие пути на публичном хосте.
  4. `rg -ni "captcha" backend/ tests/` — пусто (РЕШ-265). Общие критерии.
- **Инварианты:** №2 (рабочие пути публичному хосту недоступны — кр. 1), №3, №7, №8,
  №9 (ни учётных записей, ни персональных данных в базе — кр. 2).
- **Границы:** маршрутов `/scan` не пишет (Э6-6в); Caddy и сервер не трогает.
- **Читать первым:** `main.py`, `config.py:28–45`, `web/api_access.py:79–122`,
  `models.py:62–92`, `db.py:30–70`.

---

## Волна 3

### Э6-2б · Адаптеры Wikidata и каталога YC
- **A · полный · после Э6-2а** (без типа `analog` в `planner.md` красный
  `test_plan_types`) **· parallel**
- **Файлы:** `backend/app/search/{wikidata,yc_catalog}.py` (новые),
  `backend/app/search/__init__.py`, `tests/test_wikidata.py`, `tests/test_yc_catalog.py` (новые)
- **Контракт:** оба — `source_types = (ANALOG,)`, `requires_key = False`,
  `needs_extraction = False`; транспортный отказ, HTTP-ошибка, нечитаемый ответ —
  `SearchError` (инв. 5), пустой успех — `[]`.
  * Wikidata: SPARQL `https://query.wikidata.org/sparql`, `format=json`, `User-Agent`
    с контактом из `crossref_mailto`; поиск по тексту формулировки, только компании и
    продукты. Кандидат: `url = https://www.wikidata.org/wiki/Q…`, `raw_id = Q…`,
    `year` — основание, `snippet` — «страна; основана; сайт; отрасль», `country` —
    P17→P297, только если значение одно.
  * YC: `GET https://yc-oss.github.io/api/companies/all.json` один раз на экземпляр;
    совпадение слов формулировки с именем, описаниями, тегами, отраслью; `url` —
    страница на `ycombinator.com` (иной узел — пропуск); `country` — только при
    однозначном сопоставлении региона.
- **Критерии приёмки:**
  1. `pytest tests/test_wikidata.py tests/test_yc_catalog.py -v` — проходят (respx):
     разбор в кандидатов с полями контракта; 500, 429, обрыв, не-JSON → `SearchError`;
     две страны P17 → `country is None`; кандидат YC не на `ycombinator.com` отброшен;
     второй поиск YC не делает второго запроса; `build_adapters` включает оба без ключей.
  2. `pytest tests/test_plan_types.py tests/test_search_failures.py -v` — проходят.
  3. Общие критерии; инв. 6 — пусто.
- **Инварианты:** №1 (ссылка — страница первоисточника), №5, №6, №7, №8.
- **Границы:** платных баз и Product Hunt API нет; живых вызовов нет.
- **Читать первым:** `search/base.py`, `search/__init__.py:100–234`, `search/crossref.py`.

### Э6-2в · Яндекс-поиск аналогов по белому списку доменов
- **B · полный · после Э6-2а · parallel**
- **Файлы:** `backend/app/search/yandex_parse.py`, `backend/app/search/analog_sources.py`
  (новый), `backend/app/reference/analog_sources.yaml` (новый), `tests/test_yandex_analog.py` (новый)
- **Контракт:** YAML: `checked_at`; `reports` (~25 доменов банков, фондов,
  консультантов: `{domain, kind}`); `catalogs` (`ycombinator.com`, `producthunt.com`,
  РЕШ-255, 256). `with_site_filter` сужает `analog` по списку в любом контуре;
  домены — пачками по пределу длины запроса (константа со ссылкой на документацию
  API), каждая пачка — платный запрос в пределах 6 на прогон, не влезшие — строка в
  `limit_problems`. Домен добавляется правкой YAML, без кода.
- **Критерии приёмки:**
  1. `pytest tests/test_yandex_analog.py -v` — проходят (respx, тело перехвачено):
     `analog` несёт `site:` доменов списка; длина каждой формулировки ≤ предела; не
     влезшие названы в `limit_problems`; кандидаты — `ANALOG`; домен со схемой, путём
     или дублем — ошибка загрузки; есть `ycombinator.com` и `producthunt.com`; доменов
     20–30.
  2. `pytest tests/test_search_yandex.py tests/test_yandex_limit_visibility.py -v` —
     зелёные без правок (сужение `contest`/`grant`/`web` прежнее).
  3. Мутация «снять сужение для `analog`» → ≥1 failed. Общие критерии.
- **Инварианты:** №5 (пропуск виден), №10 (`yaml.safe_load`), №7, №8.
- **Границы:** список утверждает владелец в PR (РЕШ-257); Product Radar — РЕШ-261.
- **Читать первым:** `yandex_parse.py:40–171`, `yandex_search.py:166–235`.

### Э6-6б · Автосверка фактов и вид экспресс-скана
- **C · полный · после Э6-1, Э6-4 · parallel**
- **Файлы:** `backend/app/pipeline/autoverify.py`, `backend/app/web/scan_view.py`,
  `tests/test_autoverify.py`, `tests/test_scan_view.py` (все новые)
- **Контракт** (автосверка РЕШ-252 — чистые функции кода поверх стадии `verifier`):
  1. `source_texts(rows) -> dict[str, str]`: `normalize_url(url)` → название, сниппет,
     организация, год (строки таблицы `candidates`).
  2. Факт сверен: `normalize_url(evidence.url)` есть в `source_texts` **и** каждое число
     утверждения (после нормализации пробелов и запятой) есть в тексте источника.
  3. Карточка сверена: `evidence` непуст, все факты сверены, `unsourced_claims` пуст,
     числа `essence` и `applicability` есть в текстах её источников. Аналог: то же для
     `evidence`, `traction`, `transferability_ru`; `country_evidence_url` ∈ `source_texts`.
  4. Поля карточки скана — белый список: `title`, `essence`, `maturity_stage`,
     `developer_org`, `evidence`, `applicability`, `gov_support` только с `url`. Нет
     `contact_leads`, `developer_team`, `next_step`, конкурсной секции, сводки. В
     строках e-mail и телефоны → «[контакт скрыт]» (РЕШ-264). `sources_unavailable`
     показывается. Пусто — «не найдено» (текст — на утверждение).
- **Критерии приёмки:**
  1. `pytest tests/test_autoverify.py -v` — проходят: ссылка не из кандидатов — не
     сверен; «35 %» без числа в сниппете — не сверен; «35%» при «35 %» — сверен; один
     несверенный факт из трёх — карточка не сверена; непустой `unsourced_claims` — не сверена.
  2. `pytest tests/test_scan_view.py -v` — проходят: `ScanView(..., banner=...)` →
     `TypeError`; `scan_payload` всегда с `banner == SCAN_BANNER`; в виде и JSON нет
     `contact_leads`, `developer_team`, `next_step`; подложенные в `essence` и
     `evidence.claim` телефон и почта заменены; 5 сверенных → 3; 0 → «не найдено»;
     аналог `TW` при `gov` отсутствует.
  3. Мутации — каждая ≥1 failed: снять проверку чисел; снять проверку ссылки; пускать
     при непустом `unsourced_claims`; убрать вычистку телефонов. Общие критерии.
- **Инварианты:** №1 (урезается объём, а не доказательность — кр. 1–3), №2 (плашка
  несъёмна — кр. 2), №7, №8.
- **Границы:** маршрутов и шаблонов не делает; загрузки страниц и поля `quote` не
  вводит; `verifier` не меняет.
- **Читать первым:** `cards.py:185–227`, `search/base.py:26–45`, `models.py:170–205`,
  `export/analogs.py`.

---

## Волна 4

### Э6-6в · Публичная форма, фоновый скан, страница, JSON и заявка
- **A · полный · после Э6-6а, Э6-6б, Э6-2а…в · sequential**
- **Файлы:** `backend/app/web/routes/scan.py`, `backend/app/web/scan_run.py` (новые),
  `backend/app/web/routes/run.py` (параметр настроек `_run_pipeline`),
  `backend/app/main.py`, `backend/app/web/templates/{scan_base,scan_form,scan_result,_scan_progress}.html`
  (новые), `docs/deploy.md`, `tests/test_scan_routes.py`, `tests/test_scan_run.py` (новые)
- **Контракт:**
  1. Форма: текст (предел длины) и вид «Продукт / Цифровое решение / Бизнес-модель /
     Не знаю» → `product | digital | business_model | unknown`; `research`, присланный
     руками, — 422. Полей контактов нет; текст проходит `app.anonymize`, как в `/api/v1`.
  2. Допуск: занято (`_active_runs`) → отказ без расхода лимита; `ip_limit` и
     `budget_exhausted` → 429 со страницей формы; для потолка — «лимит на сегодня
     исчерпан» (РЕШ-258).
  3. Фоновый скан по образцу `api_run`: интейк без вопросов с `SOLUTION_KIND`, `mode =
     business`, пайплайн с копией настроек `cost_limit_rub_per_request =
     SCAN_COST_LIMIT_RUB`, `target_cards = 3`; отказ — `failed` и честная страница.
  4. Страница и JSON — только из `ScanView`; плашка — в основе `scan_base.html`; ссылок
     рабочего места и «API» в основе нет. Кнопка «Заказать полный отчёт» — одна, после
     результата: `SCAN_ORDER_TELEGRAM_URL` (только `https://t.me/…`) и/или
     `mailto:SCAN_ORDER_EMAIL` с номером скана в теме; оба пусты — кнопки нет.
  5. `docs/deploy.md`, раздел «Публичный скан»: переменные с умолчаниями, блок
     edge-Caddy для публичного хоста **без** `basic_auth`; применение — шаг владельца.
- **Критерии приёмки:**
  1. `pytest tests/test_scan_routes.py tests/test_scan_run.py -v` — проходят
     (`TestClient`, заглушки роутера и адаптеров): на публичном хосте форма → 303 на
     `/scan/{token}` → результат с плашкой; JSON с `banner`; `research` — 422; третий
     скан с IP — 429; потолок — 429 и «лимит на сегодня исчерпан»; занято — отказ,
     повтор после освобождения проходит; роутер скана — лимит 10 ₽, `target_cards = 3`;
     бриф — `mode = business`, вид из формы, «Не знаю» → вид интейка; чужой токен — 404;
     элемент «следующего шага» на странице ровно один; подложенных телефона и почты нет
     ни в HTML, ни в JSON.
  2. Мутации — каждая ≥1 failed: убрать плашку из `scan_base.html`; принять `research`;
     не передать лимит 10 ₽ в роутер скана.
  3. `rg -n "194\.87" docs/deploy.md` — пусто (адрес — плейсхолдер; на базе пусто).
  4. Правило 4: прежние тесты веба зелёные без правок. Общие критерии.
- **Инварианты:** №1, №2 (скан — только `ScanView`; «Наука» и полный отчёт — прежний
  замок), №3, №5 (`sources_unavailable` на странице), №7, №8, №9 (из «Вне скоупа»
  выходят только форма и заявка, РЕШ-254).
- **Границы:** ни капчи, ни почты посетителя, ни оплаты, ни хранения заявок, ни
  `LeadEvent`; Caddy на сервере не трогает. Тексты — на утверждение в PR.
- **Читать первым:** `web/api_run.py`, `web/routes/api_v1.py:1–120`,
  `web/routes/run.py` (`_run_pipeline`, `_active_runs`), `scan_view.py`, `scan_limits.py`.

---

## Сводка файлов по волнам (кроме новых тестов задачи)

| В. | A | B | C |
|---|---|---|---|
| 1 | Э6-1: `schemas`, `models`, `db`, `brief_store`, `pipeline/{intake,constraints_guard,intake_flow}`, `routes/intake`, `index.html`, `intake.md`, `CHANGELOG`, `test_prompts` | Э6-3: `countries.py`, `reference/countries_gov.yaml` | — |
| 2 | Э6-2а: `pipeline/{analogs,run_analogs,run_stages,orchestrator,stages,planning,pipeline_selection_flow,extraction,demo}`, `yandex_search.py`, `planner.md`, `analog_scorer.md`, `CHANGELOG`, `test_prompts`, `test_plan_types` | Э6-4: `export/{analogs,view,markdown,pdf,docx,json}`, `routes/report.py`, `report.html`, `report_print.html`, `_analog_card.html` | Э6-6а: `config`, `models`, `db`, `main`, `web/{host_routing,scan_limits}` |
| 3 | Э6-2б: `search/{wikidata,yc_catalog,__init__}` | Э6-2в: `yandex_parse`, `search/analog_sources.py`, `reference/analog_sources.yaml` | Э6-6б: `pipeline/autoverify.py`, `web/scan_view.py` |
| 4 | Э6-6в: `routes/{scan,run}.py`, `scan_run.py`, `main.py`, шаблоны скана, `deploy.md` | — | — |

## Отложенности

* **DLG-Э6-1 · живой скан.** Ключей моделей на сервере нет (РЕШ-251, 265). Владелец:
  ключи в `.env` сервера, переменные скана, блок edge-Caddy публичного хоста (после
  «да» на список команд). Затем оркестратор — 2 скана («Продукт» и «Не знаю»),
  бюджет — **предложение до 30 ₽, утверждает владелец**. Проверка: плашка, нет
  контактов, у каждого факта ссылка, аналоги с переносимостью, ≤ 10 ₽ на скан.
  Вывод — `evidence/Э6-DLG-1-*`.
* **DLG-Э6-2 · утверждение в PR:** тексты формы, кнопки, отказов, подписей;
  `analog_scorer.md`, правки `planner.md` и `intake.md`; белый список; редакция 430-р.

## Эскалации (блокируют только названное)

* **Э-1 · что считают потолок 300 ₽ и стоп-кран 10 ₽** (§9 п. 2). Сегодня `cost_rub` —
  только модели; поиск Яндекса (до 6 платных запросов) не входит.
  А: потолок суток = модели + запросы Яндекса × `YANDEX_SEARCH_RUB_PER_QUERY`; стоп-кран
  10 ₽ — по моделям, как РЕШ-46 — честный потолок одной настройкой. Б: только модели —
  проще, фактический расход выше 300 ₽. **Рекомендую А.** Обратимо. Блокирует только
  слияние Э6-6а; по умолчанию делается А.
* **Э-2 · страна основных карточек в режиме «Гос».** У `SolutionCard` страны нет, фильтр
  волны действует на аналоги. А: основные карточки «Гос» страхует эксперт (`gov`
  приходит только из `/api/v1`, отчёт — после вердикта; скан — всегда `business`),
  страна карточки — отдельная задача. Б: страна в `SolutionCard` сейчас — правка
  `card_author.md`, то есть входа «Науки». **Рекомендую А.** Не блокирует.
* **Э-3 · поштучный вердикт на аналог.** Сейчас аналоги закрыты замком отчёта целиком и
  фильтром `evidence`; ревью и вердикты привязаны к `solution_cards`. **Рекомендую**
  долг следующей волны. Не блокирует.

## Автономные решения плана (строкой в DECISIONS.md)

1. Все схемные контракты этапа вносит Э6-1 в волне 1: у `schemas.py` один владелец на этап.
2. `solution_kind` исключён из дампа брифа, как поля экосистемы: `BRIEF_JSON` «Науки» прежний; значение — в колонке `briefs`.
3. `build_export_view` накладывает поля `briefs` (экосистема и вид) на бриф отчёта — одна точка вместо копии в `json.py`.
4. Выбор человека сильнее модели: вид ≠ «Не знаю» фиксирует код; «Не знаю» — рыночный вид модели, иначе `product`.
5. «Наука неизменна» доказывается кодом и прежними тестами без правок: `analog` при `research` снимается кодом, стадия аналогов не зовётся, `analog_cards` пуст.
6. Тип `analog` и `ANALOG` у адаптера Яндекса входят одной задачей: `test_plan_types` держит соответствие в обе стороны.
7. Маршрут `analog_scorer` в `config.py` не прописывается: без маршрута стадия идёт по `LLM_FALLBACK_ORDER`.
8. Секция аналогов собирается последней по образцу конкурсной; стоп-кран не перехватывается (РЕШ-45).
9. Автосверка РЕШ-252 — чистые функции кода (ссылка факта ∈ ссылок кандидатов, все числа — в тексте источника, `unsourced_claims` пуст) поверх `verifier`, без загрузки страниц и поля `quote`.
10. Тексты для автосверки — из таблицы `candidates`, без повторного обращения к сети.
11. Справочники этапа — в `backend/app/reference/`, не в `data/`: `data/` в контейнере — том, новые файлы образа на него не доезжают.
12. Коды стран — ISO alpha-2 строкой в кавычках (ловушка YAML: `NO` читается как `false`).
13. Страна кандидата Wikidata/YC — только однозначная; структурная страна перекрывает страну модели; аналог без подтверждённой страны не публикуется.
14. Сноска «Гос» — одна строка (название, страна, ссылка); JSON `schema_version` остаётся `1.0`.
15. Лимит по IP — в памяти процесса, IP не пишется ни в базу, ни в лог; рестарт обнуляет — риск закрывает потолок.
16. Суточный потолок — по базе (сумма сканов за сутки + резерв 10 ₽ на незавершённый): переживает рестарт и не перерасходуется гонкой.
17. Сутки — календарные, фиксированный UTC+3 без `zoneinfo`.
18. IP клиента — последний адрес `X-Forwarded-For` только при соединении с частного адреса; иначе адрес соединения.
19. Роутинг по Host — прослойка с белыми списками путей; `PUBLIC_SCAN_HOST` пуст — публичного режима нет.
20. Результат скана — по неугадываемому токену в `requests.scan_token`; номер запроса наружу не отдаётся.
21. Плашка — свойство `ScanView` и основы публичных шаблонов, не аргумент и не текст отдельной страницы.
22. «Без контактов» — белый список полей карточки скана плюс вычистка e-mail и телефонов; адреса закрывает белый список полей.
23. В скане нет `next_step` карточек, конкурсной секции и сводки: единственный «следующий шаг» — кнопка заявки.
24. Публичный скан — всегда режим `business`; «Гос» приходит только из `LaunchContext`.
25. Скан при занятой системе отказывает без расхода лимита.
26. Рабочее место получает выбор вида решения (умолчание «Наука») — иначе платный полный отчёт «Рынка» не завести.
27. Э6-5: `LeadEvent` и подписанный `LaunchContext` вынесены (нет приёмника, секрета, схемы); «следующий шаг» закрыт в Э6-6в.

## Самопроверка планировщика

1. Прочитаны SPEC §7а (обновлённый) и §2а «Вне скоупа»; РЕШ-238…265; `PLAN-E6-API.md`;
   CONVENTIONS §4, §5, §7, §9, §11, §13, §13а; INVARIANTS; CLAUDE.md; STATE.md; код —
   `schemas`, `models`, `config`, `main`, `api_access`, `api_run`, `export/{json,view}`,
   `pipeline_selection_flow`, `run_stages`, `cards`, `search/{base,yandex_search,yandex_parse}`,
   `demo`, `tests/test_plan_types.py`, `tests/test_prompts.py`.
2. У каждой задачи файлы; внутри волн пересечений нет (сводка). Общие контракты — по
   одному владельцу в волне. Пределы циклов — правило 1, для всех девяти задач.
3. Каждый критерий — команда с ожидаемым результатом; мутации — с «≥1 failed».
4. Вне скоупа в плане нет: личных кабинетов, биллинга, сбора контактов, капчи, Product
   Hunt API, реестров РЕШ-261.
5. Живые события: в задачах исполнителей живых порогов нет; DLG-Э6-1 проверяет то, что
   код производит — `analog` обслуживается Яндексом с Э6-2а и двумя адаптерами с Э6-2б,
   плашка и аналоги доходят до страницы и JSON через `ScanView`.
6. Отрицательные критерии прогнаны грепом на базе — все пусты: инв. 6;
   `"TW"|'TW'|430` в `backend/app`; `captcha` в `backend/`, `tests/`; `analog` в
   `prompts/`; `solution_kind` в `planner/card_author/verifier/summary.md`; `/scan` в
   `backend/`; `194\.87` в `docs/deploy.md`. Имена прежних тестов Яндекса сверены
   (`test_search_yandex.py`, `test_yandex_limit_visibility.py`).
7. Изменены только `.planning/PLAN.md` и `.planning/plans/PLAN-E5.md` (копия плана Э5).
