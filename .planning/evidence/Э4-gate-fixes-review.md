# Ревью Э4-gate-fixes (РЕШ-204, РЕШ-205) · цикл 1

Вердикт: ПРИНЯТО (блокирующих 0, советов 0)

## Предмет
Ветка `e4/gate-fixes`, коммиты `8b303c3` + `3f34434`, база `0ef27574`.
Файлы: `backend/app/export/pdf.py`, `backend/app/export/docx.py`,
`backend/app/web/templates/report_print.html`,
`backend/app/web/routes/review.py` + тесты
(`tests/test_export_pdf.py`, `tests/test_export_docx.py`, `tests/test_web_review.py`).

## Проверено
- `git diff 0ef27574..e4/gate-fixes --stat`: 9 файлов — ровно названные
  четыре кода + три теста + два evidence-файла исполнителя. Файлов вне
  списка задачи нет.
- `pytest tests/test_export_pdf.py tests/test_export_docx.py tests/test_web_review.py -v`:
  **31 passed**.
- `pytest -q` (дважды подряд, детерминированность): **1075 passed** / **1075 passed**.
- `ruff check .`: **All checks passed!**
- `ruff format --check .`: **194 files already formatted**.
- `grep -rE "sk-|gigachat|Api-Key" backend/ tests/`: только легитимные
  упоминания заголовка `Api-Key` в существующем коде YandexGPT/патентного
  адаптера и тестовые фиктивные ключи (`sk-fake-...`, `sk-test`) — к этому
  diff'у не относятся, утечек нет.
- Диффом подтверждено: `backend/app/export/view.py`,
  `backend/app/web/templates/report.html`, `backend/app/web/templates/_card.html`
  не тронуты (`git diff --stat` по этим путям — пусто). Красная плашка
  «Нельзя показывать клиенту: нет подтверждений» жива в `_card.html:72` и
  покрыта тестами `test_web_flow_e2e.py`, `test_web_report.py` (оба зелёные
  в полном прогоне).
- Подпись/ЧЕРНОВИК-логика в `pdf.py` не задета: строка
  `if not view.is_draft and view.validated_at is not None` вне диффа
  правки, только добавлены две строки `print_cards=`/`print_contest_cards=`
  в вызов `template.render`.
- В `review.py` фильтр `reject` НЕ добавлен (grep подтверждает — только
  комментарии о его отсутствии); наложение `apply_overrides` идёт по ключу
  `(is_contest, position)`, тому же, что использует `_load_card_ids`;
  `apply_overrides` (модуль `report_overrides.py`) не меняет длину и порядок
  списков карточек — правки накладываются позиционно, id карточек в форме
  вердикта не сдвигаются.

## Мутации (прогнаны лично, обе поймались, откачены)
1. **docx.py**: `printable_cards = report.cards` /
   `printable_contest_cards = report.contest_cards` (снят фильтр evidence).
   Результат: `pytest tests/test_export_docx.py -q` → **3 failed** (карточка
   без evidence печатается, конкурсная секция не скрывается). Откачено,
   `git status` чист.
2. **review.py**: удалена строка `report = apply_overrides(report, overrides)`.
   Результат: `pytest tests/test_web_review.py -q` → **1 failed**
   (`test_правка_title_видна_на_экране_ревью` — правленый текст не виден).
   Откачено, `git status` чист.

## РЕШ-204 — разбор
- Фильтр применён именно на слое печати: `render_print_html` (pdf.py) и
  `export_docx` (docx.py) строят `print_cards`/`printable_cards` —
  списки, отфильтрованные по `card.evidence`; `report.cards`/`report.contest_cards`
  сами не мутируются, `_add_card`/`render_card` вызываются только для
  отфильтрованных карточек.
- Карточка без evidence отсутствует в печати/DOCX **целиком** — заголовка,
  стадии, TRL, риска и т. п. нет вовсе (проверено тестом
  `test_карточка_без_evidence_не_попадает_в_docx` и
  `test_render_print_html_empty_evidence_card_is_not_printed`, в обоих
  явно проверяется отсутствие заголовка карточки, а не только «Стадия
  зрелости»/TRL, как было раньше).
- Порядок соседних карточек сохранён — тесты
  `test_карточка_без_evidence_пропускается_а_порядок_остальных_сохраняется`
  и `test_render_print_html_mixed_evidence_keeps_order_of_visible_cards`
  проверяют `text.index(first) < text.index(third)`.
- Пустая конкурсная секция не рисует заголовок — тесты
  `test_конкурсная_карточка_без_evidence_не_попадает_и_секция_скрывается`
  и `test_render_print_html_contest_section_hidden_when_all_bare`
  проверяют отсутствие `CONTEST_SECTION_HEADING` в выводе.
- Операторская страница (`report.html`/`_card.html`, `build_export_view`,
  `visible_report`) диффом не тронута — красная плашка жива, покрыта
  существующими тестами, прошедшими в полном прогоне.
- Сквозной тест (`test_web_flow_e2e.py`) жив, входит в полный набор 1075,
  прогнан отдельно — 1 passed.

## РЕШ-205 — разбор
- `show_review` накладывает `apply_overrides(report, overrides)` на отчёт
  ДО построения `cards_with_ids`; фильтр `reject` по-прежнему не применяется
  (эксперт видит все карточки секций, включая отклонённые — существующий
  тест `test_экран_открывается_и_содержит_формы_обеих_секций` и весь пакет
  `test_web_review.py`/`test_web_report_verdicts.py` зелёные).
  `card_ids`/`cards_with_ids` строятся по индексам `report.cards`/`report.contest_cards`
  **после** наложения правок, но так как `apply_overrides` не меняет
  количество и порядок карточек (только текстовые поля и, опционально,
  `evidence`), связка (позиция → id из БД) остаётся верной.
- Новый тест `test_правка_title_видна_на_экране_ревью` проверяет: правленый
  заголовок виден, исходный — нет, и что ссылка формы вердикта
  `/cards/{id}/verdict` по-прежнему указывает на исходный id карточки —
  наложение правки id не сдвигает.

## Грабли §0.1 SPEC
- Подстановок/меток `{{...}}` в затронутых файлах нет — правка не касается
  промптов.
- `extract_json_block`, `max_tokens`, адрес GigaChat — к этому diff'у не
  относятся (правка не трогает LLM-стадии).
- YAML с двоеточием: `Э4-gate-fixes-handoff.yaml` — двоеточия в значениях
  экранированы кавычками (`task: "Э4-gate-fixes ..."`, значения `checks`
  в кавычках). `pytest tests/test_team_docs.py` не запускался отдельно, но
  входит в полный прогон 1075 — зелёный.

## Инварианты (все десять)
1. **Факт без ссылки** — diff не добавляет и не убирает фактических полей
   карточки, только скрывает бессылочные карточки из клиентских выгрузок
   (усиливает инвариант, не ослабляет). Соблюдён.
2. **Автоотправка/ЧЕРНОВИК** — логика `is_draft`/`validated_at` в `pdf.py`
   не изменена; правка не трогает пути снятия пометки. Соблюдён.
3. **Секреты** — grep чист (см. выше). Соблюдён.
4. **`render_prompt`/метки** — diff не касается промптов, `tests/test_prompts.py`
   не задет, входит в зелёный полный прогон. Соблюдён.
5. **Отказ источника** — diff не касается адаптеров источников. Не применимо,
   нарушений нет.
6. **`httpx` только в llm/search/оркестраторе** — diff не добавляет вызовов
   `httpx`. Соблюдён (не проверялось отдельным grep, т.к. diff не содержит
   сетевого кода).
7. **`pytest` без сети** — полный прогон 1075 зелёный без сети, PDF-путь
   подменяется как раньше (Chromium не поднимается). Соблюдён.
8. **backend + tests парой** — правка `backend/app/export/*.py`,
   `backend/app/web/routes/review.py`, `backend/app/web/templates/report_print.html`
   сопровождена правкой `tests/test_export_pdf.py`, `tests/test_export_docx.py`,
   `tests/test_web_review.py`. Соблюдён.
9. **Diff по задаче** — сверено с РЕШ-204/205 и списком файлов задачи, лишнего
   нет (см. `git diff --stat` выше). Соблюдён.
10. **YAML с двоеточием в кавычках** — handoff исполнителя проверен глазами,
    двоеточия в значениях закавычены. Соблюдён.

## Handoff исполнителя (проверка)
- Артефакты существуют: все 4 кодовых файла, 3 тестовых файла,
  `.planning/evidence/Э4-gate-fixes-pytest.txt` — на месте.
- `did` описывает результат (что стало), а не процесс.
- `checks` — фактический вывод команд (31 passed, 1075 passed, ruff чисто),
  воспроизведён самостоятельно и совпадает.
- `self_audit: pass` + `escalate: none` — непротиворечиво, блокирующих не
  найдено, вопрос владельцу не нужен.
- Числа исполнителя по мутациям воспроизведены лично (см. раздел «Мутации»)
  — оба падения подтверждены независимо, а не приняты на слово.

## Советы (не блокируют)
Нет.
