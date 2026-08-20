# Проверка (ревью+верификация) Э2-Г/З-2 · цикл 1

Вердикт: ПРИНЯТЬ

## Что сделано (проверено самостоятельно)

1. Синхронизация: git merge session/E2G + git merge worktree-agent-a8ef5161ae9efcff3
   в рабочей копии agent-a332bc699b13ba44e. git log --oneline показывает
   9d32cf6 в истории (слияние merge-коммитом ddb44f2).
2. Постановка задачи сверена: .planning/PLAN.md (карточка З-2, «Волна 0»),
   docs/team/INVARIANTS.md, .planning/DECISIONS.md (РЕШ-94, РЕШ-132, РЕШ-135).
3. Diff задачи выделен точно по её двум коммитам:
   ff1223e «Дробление stages.py (З-2): восемь модулей и фасад-реэкспорт» и
   3891b08 «З-2: сохранить логгер и подмену настроек при дроблении
   stages.py». Затронуты ровно: восемь новых модулей
   backend/app/pipeline/{cards,extraction,intake,planning,prompting,scoring,
   selection,summary}.py, фасад backend/app/pipeline/stages.py, новый
   tests/test_pipeline_layout.py, evidence-файлы задачи. cli.py,
   orchestrator.py, persistence.py, constraints_guard.py не тронуты —
   подтверждено git show --stat по обоим коммитам.
4. Перенос проверен построчно AST-сравнением, а не на слово: скрипт
   разобрал git show 8ba4a9c:backend/app/pipeline/stages.py (1418 строк) и
   все восемь новых модулей через ast, сравнил тела всех топ-уровневых
   def/async def/class (49 штук) и всех топ-уровневых присвоений
   (константы, включая LONG_CYCLE_CONTEST_DOMAINS, MAX_PUBLISHED_CARDS,
   _CALIBRATION_THRESHOLDS и т. д. — 24 штуки). Результат: 0 пропущенных,
   0 лишних, 0 расхождений тел, кроме одного — select_top — и там
   расхождение состоит ровно из заявленных пяти строк (комментарий +
   отложенный импорт get_settings), остальное тело побайтово совпадает.
   Других незаявленных правок логики в диффе переноса нет.
5. Фасад stages.py (120 строк, без единого def/async def/class) проверен
   не по __all__, а по фактическим обращениям: grep по tests/ и
   backend/app на "stages.имя", "from app.pipeline.stages import",
   "from app.pipeline import stages" — каждое имя, которое реально
   используется (run_intake, refine_brief, brief_from_raw_text,
   neutralize_placeholders, plan_search, replan_search, needs_second_pass,
   extract_candidates, deduplicate, score_candidates, select_top,
   select_publishable_cards, group_for_cards, author_card, verify_card,
   cap_contest_maturity, contest_maturity_ceiling, LONG_CYCLE_CONTEST_DOMAINS,
   LONG_CYCLE_CONTEST_NAMES, NeutralScoredCandidate, _has_neutral_score,
   _model_relevance, _neutral_scores, has_neutral_score, owner_answers_block,
   candidates_to_json, load_prompt, render_prompt, score_candidates,
   fallback_summary, summarize, count_source_type_coverage), присутствует
   в stages.py и в __all__. Пропусков не найдено.
6. Прогнан pytest -q целиком: 647 passed, 0 failed (в моей копии, где уже
   слиты и З-1, и З-3 — отсюда 647, а не заявленные исполнителем 644 в его
   локальной копии без З-1/З-3; разница объясняется тремя тестами
   test_probe.py из З-1, арифметика сходится: 641 база (8ba4a9c) + 3 (З-1)
   + 3 (test_pipeline_layout.py, З-2) = 647). Расхождение не является
   находкой — это следствие разного состава слияний у меня и у исполнителя.
7. ruff check . — All checks passed. ruff format --check . — 113 files
   already formatted.
8. Отдельно прогнаны tests/test_cli_run.py tests/test_pipeline_resilience.py
   tests/test_pipeline_layout.py (критерий 4 карточки) — 42 passed.
9. Все четыре обязательные мутации карточки поставлены и прогнаны мной
   лично (прицельным прогоном, РЕШ-135), а не приняты на слово:
   - (а) удалено имя run_intake из фасада → test_facade_reexports_
     required_names упал, плюс 19 тестов test_cli_run.py упали
     (AttributeError) — итого 20 failed, 2 passed. Совпадает с evidence.
   - (б) в _pick_top (selection.py) cap = max(2, limit // 2) заменён на
     cap = limit → test_select_top_limits_single_provider_dominance и
     test_select_top_with_zero_threshold_keeps_previous_behaviour упали —
     2 failed, 4 passed. Совпадает.
   - (в) в stages.py добавлена лишняя функция run_intake_copy рядом с
     реэкспортом → упали test_pipeline_modules_stay_within_line_limit
     (125 строк > 120) и test_stages_module_has_no_definitions — 2 failed,
     1 passed. Совпадает.
   - (г) scoring.py раздут до 621 строки → test_pipeline_modules_stay_
     within_line_limit упал (только этот тест) — 1 failed, 2 passed.
     Совпадает.
   После каждой мутации — git checkout -- на изменённый файл и git status
   --short → пусто. Дерево чистое на протяжении всей проверки и в конце.
   Все четыре мутации убили ожидаемые проверки на первой попытке — по
   РЕШ-135 полный набор поверх мутаций не требовался, обязателен только
   финальный прогон (сделан, п. 6).
10. Инвариант №6: grep -rn "httpx" backend/app — совпадения только в
    backend/app/cli.py и backend/app/pipeline/orchestrator.py (оба вне
    диффа задачи, не тронуты). Ни один из восьми новых модулей httpx не
    импортирует.
11. Инвариант №3: grep -rE "sk-|gigachat|Api-Key" backend/ tests/ находит
    только пред-существующие имена полей конфигурации/URL в config.py,
    cli.py, llm/gigachat.py — ни один из них не в диффе задачи и не
    секрет-значение. В восьми новых модулях и в stages.py — 0 совпадений.
    .env в .gitignore подтверждено.
12. Инвариант №4: render_prompt в prompting.py перенесён дословно (см. п.
    4), падает на незаполненной метке (raise ValueError, строка не
    менялась при переносе); pytest tests/test_prompts.py -q — 44 passed.
13. Инварианты №1, №2, №5, №7, №8, №9, №10 — задача не создаёт нового
    поведения продукта (только страж структуры, у которого свой тест —
    инвариант №8 соблюдён этим тестом); поведение стадий (карточки,
    подтверждения, отказ источников, extract_json_block) перенесено
    дословно и покрыто существующими тестами, которые не редактировались
    (git diff --name-only -- tests/ между 5fc6873 и 3891b08 по факту даёт
    ровно tests/test_pipeline_layout.py, проверено отдельно по обоим
    коммитам задачи). YAML-документов задача не касается — №10 не
    применим к диффу. Сеть при прогоне не использовалась (№7) — офлайн
    прогон, respx/заглушки не подменялись мной специально, они уже в
    тестах.

## Разбор двух нестандартных мест — по существу, не пересказ

### 1. Логгер прибит строкой "app.pipeline.stages" вместо __name__

Проверено предметно, а не на слово. В пяти модулях (cards.py,
extraction.py, planning.py, scoring.py, selection.py) логгер получает имя
явной строкой; intake.py, prompting.py, summary.py логгер не используют
вовсе.

Проверил механику caplog: caplog.at_level(logging.INFO,
logger="app.pipeline.stages") — в pytest это выставляет уровень
конкретно названному логгеру, а не «всем логгерам, чьё имя начинается с
этой строки». Если бы модули использовали __name__ (app.pipeline.cards,
app.pipeline.scoring и т. д.), вызовы caplog.at_level(...,
logger="app.pipeline.stages") в одиннадцати существующих тестах
(test_stages.py, test_contest_queries.py, test_metrics.py,
test_pipeline_second_pass.py) не поднимали бы уровень у логгеров-детей —
записи INFO/WARNING из этих модулей отсеклись бы на уровне самого
логгера ещё до хендлера caplog, и тесты ловили бы пустоту. Это не
теоретическое рассуждение: формат вывода в cli.py:181 и main.py:51 —
"%(asctime)s %(levelname)-7s %(name)s | %(message)s" — не несёт
%(module)s/%(pathname)s, то есть имя логгера — единственный источник
происхождения записи и в проде, и в тестах.

Суждение. Это не маскировка ради удобства — это единственный способ
одновременно удержать оба жёстких требования задачи: «поведение не
изменилось ни на шаг» (наблюдаемое поведение включает то, что ловит
caplog) и «существующие тесты не правятся» (это критерий приёмки, не
пожелание). Альтернатива — поменять логгер на __name__ — прямо нарушила
бы второе требование явно и предсказуемо. Решение задокументировано в
каждом файле и в сообщении коммита 3891b08, а не спрятано.

Цена, которую я называю прямо. Она реальна и не нулевая: после дробления
по логу app.pipeline.stages невозможно отличить, пришла ли запись из
cards.py, extraction.py, planning.py, scoring.py или selection.py — ни в
проде, ни при живой отладке через docker logs. Разработчик, ищущий код
по строке лога, будет искать его в файле stages.py, где определений
больше нет, и найдёт нужный модуль только через grep по тексту
сообщения. Это прямое противоречие цели самого дробления — «код стало
легче найти» — применительно к логам. Это осознанный, честно названный
долг, а не блокирующий дефект: он вынужден рамками задачи («тесты не
трогать»), и снять его можно только отдельной задачей, которая
одновременно поправит логгеры и адресные caplog-вызовы в тестах — то
есть задачей, которая явно выходит за границы переноса. Рекомендую не
блокировать, но занести долг в DECISIONS.md или следующую карточку по
orchestrator.py/логированию, чтобы через два дробления никто не удивился
одинаковым именам логгеров в разных файлах.

### 2. select_top читает get_settings отложенным импортом из фасада

Проверил: stages.py на уровне модуля импортирует select_top из
selection.py. selection.py, в свою очередь, не импортирует stages на
уровне модуля — импорт "from app.pipeline.stages import get_settings"
стоит внутри тела функции select_top, исполняется только при вызове.
Поэтому "python -c import app.pipeline.stages" не падает (проверено
лично, плюс подтверждено в evidence) — жёсткого цикла на этапе загрузки
модулей нет.

Но по существу — архитектурный цикл есть, только сдвинутый по времени:
stages грузит selection при импорте, а select_top, будучи вызванной,
снова тянет stages, который к тому моменту уже прогружен (вызывается
после старта оркестратора/CLI, когда app.pipeline.stages гарантированно
в sys.modules). Работает это исключительно потому, что реальный порядок
вызовов в проекте всегда начинается с импорта фасада раньше первого
вызова select_top — совпадение процесса запуска, а не структурное
свойство. Проверил причину: ровно два теста (test_persistence.py:994,
test_stages.py:208) монки-патчат stages.get_settings, а не
app.config.get_settings и не selection.get_settings — при прямом "from
app.config import get_settings" на уровне модуля (как было раньше и как
логично для selection.py как «низкого» модуля) патч stages.get_settings
до selection.py не долетел бы: имя get_settings, единожды связанное при
импорте, не следит за атрибутом объекта stages. Значит и это —
вынужденный ответ на то же ограничение «тесты не трогать», не
произвольный выбор.

Суждение. Да, фасад стал скрытой зависимостью в обратную сторону:
низовой модуль selection.py теперь опирается на существование модуля
stages.py, который сам является лишь реэкспортом selection.py.
Формально критерий задачи выполнен буквально (результат: "python -c
import app.pipeline.stages" проходит — п. 5 раздела «Что делать»), и
выбор способа явно оставлен исполнителю тем же пунктом плана. Но это
именно то, о чём предупреждает план в п. 5 применительно к
cards.py/constraints_guard.py («импорт указывает на конкретный модуль, а
не на фасад, если иначе получается цикл») — и там разрешение цикла ушло
в сторону «импортировать конкретный модуль», а в selection.py — в
противоположную сторону («импортировать фасад»). Несогласованность в
выборе направления есть, но я не считаю её ошибкой: для
cards.py/constraints_guard естественный низкоуровневый источник
(constraints_guard) существовал и был доступен напрямую без потери
монки-патча; здесь низкоуровневый источник (app.config.get_settings)
существовал, но именно прямой импорт из него и ломает тест —
единственный работающий вариант без правки тестов — это чтение через
объект, на который «смотрит» монки-патч, то есть через stages.

Во что это выльется при следующем дроблении. Если stages.py когда-нибудь
будет не просто фасадом, а удалён совсем (естественный конец жизни
переходного фасада, когда все потребители перейдут на прямые импорты) —
selection.select_top сломается немедленно и не при импорте, а при первом
вызове в рантайме, то есть тесты, гоняющие select_top без прогона именно
той ветки (relevance_min=None), эту поломку не поймают, пока кто-то не
уберёт stages.py буквально. Это отложенная мина, а не сегодняшний
дефект. Рекомендую не блокировать — решение соответствует букве
критерия задачи и добросовестно задокументировано в коде, — но занести
находку в DECISIONS.md как условие для будущей задачи «удаление facade
stages.py»: прежде чем убирать фасад, поправить оба монки-патч-теста и
вернуть selection.py на прямой "from app.config import get_settings".

## Итог по обоим местам

Оба — не блокирующие находки, а вынужденные и честно названные
компромиссы одного корня: жёсткое требование карточки «существующие
тесты не правятся» при одновременном требовании «поведение не
изменилось ни на шаг» оставляет ровно эти два выхода. Я называю цену
явно (см. выше), но не блокирую по ним — оба соответствуют букве
критериев карточки и задокументированы в коде и в сообщении коммита, а
не спрятаны.

## Блокирующие находки

Нет.

## Советы (не блокируют)

1. Занести в DECISIONS.md или в долг следующей задачи по orchestrator.py:
   единый логгер "app.pipeline.stages" на пять модулей теряет
   происхождение записи в логах прод/отладки — обосновано и вынуждено
   сейчас, но не должно застыть навсегда.
2. При будущем удалении фасада stages.py — синхронно поправить
   tests/test_persistence.py:994 и tests/test_stages.py:208 (монки-патч
   get_settings) и вернуть selection.py на прямой импорт "from app.config
   import get_settings", иначе select_top тихо сломается при первом
   вызове после удаления фасада.
3. Расхождение 644 (у исполнителя) vs 647 (в моей слитой копии) в
   счётчике pytest -q стоит явно комментировать в будущих evidence-файлах
   — зависит от состава слияний на момент прогона, не от задачи.

## Проверено (фактический вывод команд)

- git log --oneline -2 (после merge): ddb44f2 Merge branch
  worktree-agent-a8ef5161ae9efcff3..., далее 9d32cf6 З-2: доказательства
  проверок... — коммит задачи в истории.
- python -m pytest -q (корень репозитория, вся копия после слияния всей
  волны 0): 647 passed, 1 warning in 235.92s.
- python -m pytest tests/test_cli_run.py tests/test_pipeline_resilience.py
  tests/test_pipeline_layout.py -q: 42 passed in 5.50s.
- python -m pytest tests/test_prompts.py -q: 44 passed in 0.30s.
- python -m ruff check .: All checks passed!
- python -m ruff format --check .: 113 files already formatted.
- grep -rn httpx backend/app: только backend/app/cli.py и
  backend/app/pipeline/orchestrator.py (вне диффа задачи).
- grep -rE sk-|gigachat|Api-Key backend/ tests/: только пред-существующие
  имена полей в config.py/cli.py/llm/gigachat.py, вне диффа задачи и вне
  восьми новых модулей.
- AST-сравнение тел 49 функций/классов и 24 констант между git show
  8ba4a9c:backend/app/pipeline/stages.py и восемью новыми модулями: 0
  пропущенных, 0 лишних, 1 расхождение тела (select_top, ожидаемое и
  заявленное).
- Мутации (а)-(г) карточки — все четыре поставлены и прогнаны лично,
  результаты (20 failed/2 passed; 2 failed/4 passed; 2 failed/1 passed; 1
  failed/2 passed) совпадают с .planning/evidence/E2G-З2-mutations.txt;
  git status --short после каждого отката — пусто.
- git status --short в конце проверки — пусто.

## Handoff

handoff:
  from: reviewer
  to: orchestrator
  task: "Э2-Г/З-2"
  did:
    - "Синхронизирована рабочая копия (merge session/E2G, merge worktree-agent-a8ef5161ae9efcff3), подтверждён коммит 9d32cf6"
    - "Перенос сверен построчно AST-сравнением: 49 функций/классов и 24 константы совпадают дословно, кроме заявленного и обоснованного расхождения в select_top"
    - "Фасад stages.py проверен по фактическим импортам в коде и тестах, не по __all__ — пропусков нет"
    - "Все четыре обязательные мутации карточки поставлены и прогнаны лично (не приняты на слово), результаты совпали с evidence исполнителя"
    - "Разобраны оба нестандартных места (логгер, отложенный импорт get_settings) — оба признаны вынужденными и не блокирующими, с явно названной ценой и рекомендацией зафиксировать долг"
    - "Прогнаны pytest -q (647 passed), ruff check, ruff format --check"
    - "Проверены инварианты №1-10, грабли §0.1 — нарушений нет"
  artifacts:
    - ".planning/evidence/E2G-З2-check.md (этот отчёт)"
  self_audit: pass
  checks:
    - "pytest -q: 647 passed, 1 warning in 235.92s"
    - "ruff check .: All checks passed!"
    - "ruff format --check .: 113 files already formatted"
    - "мутации (а)-(г): все убили ожидаемые проверки на первой попытке, дерево чистое после каждого отката"
  escalate: none
  read_first:
    - ".planning/PLAN.md (карточка З-2)"
    - "backend/app/pipeline/stages.py"
    - "backend/app/pipeline/selection.py"
    - "docs/team/INVARIANTS.md"
  notes: "Вердикт ПРИНЯТЬ, цикл 1, второй цикл не требуется. Два совета вынесены как долг для DECISIONS.md/будущих задач, блокирующих находок нет."
