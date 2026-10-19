# Верификация Э6-2а «Слой аналогов в пайплайне»
Вердикт: PASS (закрыто 7 из 7; DLG-1 открыт, на вердикт не влияет)
База: ветка claude/e6-2a, HEAD 8e28b02; проверка предка 776b9c1 (merge-base is-ancestor) — ок.
Среда: Python 3.13.5 из venv jolly-wilbur, PYTHONPATH — backend этой копии (app.__file__ внутри копии).

| № | Критерий | Команда | Факт | Статус |
|---|---|---|---|---|
| 1 | Тесты аналогов проходят, сценарии покрыты | pytest tests/test_analog_stage.py tests/test_analog_flow.py -v | 44 passed in 12.50s. Сценарии: research без analog_scorer и analog_cards == [] — test_analog_flow.py:260-291; analog от модели при research снят после planning — test_analog_stage.py (research снимает analog; plan_search без analog при research; guard на research); product до 3 аналогов вне cards, target_cards не задет — test_analog_flow.py:299-318 (len(cards)==MAIN_COUNT); TW при gov не доходит до модели — :377-384 (вызовов AnalogCard 1, страны US); TW от модели по кандидату без страны при gov не публикуется — :395-401; вход сводки без аналогов — :321-339 (summary_inputs == report.cards) | ок |
| 2 | Промпты и типы плана; метки | pytest tests/test_prompts.py tests/test_plan_types.py -q | 72 passed in 0.84s. test_prompts.py:29 planner = BRIEF_JSON, SOLUTION_KIND; :44 analog_scorer = BRIEF_JSON, ANALOG_CANDIDATES_JSON | ок |
| 3a | Мутация: убран вызов prefilter_analog_candidates (run_analogs.py:48 → analog_scored) | pytest tests/test_analog_flow.py -q | 1 failed, 14 passed (кандидат TW в gov не доходит до модели). Откат checkout | ок |
| 3b | Мутация: убрана проверка статуса страны в select_publishable_analogs (analogs.py:149 if False and …) | то же | 1 failed, 14 passed (TW от модели по кандидату без страны в gov не публикуется). Откат | ок |
| 3c | Мутация: убрано снятие analog при research (planning.py:105 → kept = analog[:ANALOG_QUERIES]) | то же | 1 failed, 14 passed (research не зовёт analog_scorer и не шлёт analog источнику). Откат | ок |
| 3d | Мутация: ANALOG убран из COVERAGE_EXCLUDED_SOURCE_TYPES (extraction.py:324) | то же | 1 failed, 14 passed (аналоги не засчитываются в покрытие типов, assert test_analog_flow.py:347). Откат | ок |
| 4 | analog не упомянут в чужих промптах | rg -n "analog" prompts/card_author.md prompts/verifier.md prompts/summary.md prompts/scorer.md prompts/intake.md | пусто, rc=1 | ок |
| 5 | Страж размера файлов | pytest tests/test_backend_layout.py -q; wc -l run_stages.py | 3 passed; run_stages.py 457 строк (≤ 500) | ок |
| 6 | Правило 4: правки тестов | git diff --stat 776b9c1 HEAD -- tests/ | новые: test_analog_flow.py (450), test_analog_stage.py (365); названные: test_prompts.py (10), test_plan_types.py (+34); вынужденные, приняты ревью: test_cli_run.py (2 строки), test_brief_ecosystem_fields.py (6 строк). Иных нет | ок (две вынужденные правки отмечены) |
| 7a | Полный pytest | pytest -q -p no:cacheprovider | 6 failed, 1501 passed in 295s. Все 6 failed — tests/test_docker_compose_backup_runtime.py (известная Linux-специфика). 1501 ≥ 1447 + новые | ок |
| 7b | Линтер | ruff check . ; ruff format --check . | All checks passed!; 230 files already formatted | ок |
| 7c | Инв. 6 | rg -ln httpx backend/app минус допустимые | пусто (rc=1) | ок |
| 7d | Секретов нет в диффе | дифф 776b9c1..HEAD по backend, tests, prompts + rg добавленных строк с sk- / Api-Key | пусто (rc=1). Общий rg по backend/ tests/ находит лишь прежние строки (комментарий config.py, докстрока patents_view.py, test_config.py, фиктивный ключ в test_dedup_judge_probe.py) — вне диффа задачи | ок |

git status --short после мутаций и откатов — чисто.

## Инварианты
№1 evidence непуст — select_publishable_analogs отбрасывает карточки без evidence, тест зелёный · №3 секретов в диффе нет · №4 test_prompts.py зелёный (72 passed) · №5 отказ модели по аналогу не роняет прогон (тест в test_analog_flow.py:432) · №6 пусто · №7 pytest без сети (заглушки/respx), 1501 passed · №8 backend/ и tests/ изменены вместе · №2, №9, №10 нарушений в диффе нет.

## Известные отступления (не FAIL)
- _kind_kwargs в intake_flow.py оставлен долгом.
- Починка Б1 закоммичена и прогнана оркестратором (ревью ц.2 подтвердило).
- В диффе также backend/app/search/yandex_search.py (4 строки) — вне перечня критериев, отмечено.

## Отложено
- DLG-1: прогон в контейнере python:3.12-slim (РЕШ-249) невозможен — docker info: "error during connect: ... pipe/dockerDesktopLinuxEngine: The system cannot find the file specified" (Docker Desktop не запущен). Действие владельца: запустить Docker Desktop и повторить docker run --rm -v <копия>:/src:ro python:3.12-slim sh -c "cp -r /src /w && cd /w && pip install -q -e '.[dev,export]' && pytest -q". На вердикт не влияет по условию задания.
