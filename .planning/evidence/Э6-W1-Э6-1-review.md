# Ревью Э6-1 · цикл 1
Вердикт: ВЕРНУТЬ (блокирующих 1, советов 3)

## Блокирующие
1. [§5: поведение без теста; инв. 8] Веб-проводка вида решения не покрыта ни одним тестом.
   Три мутации пережили **весь** набор (1409 passed, 0 failed), не только целевые файлы:
   - `routes/intake.py::_confirm_brief` — убрана строка `fields["solution_kind"] = saved.solution_kind ...`
     (при подтверждении брифа выбор человека молча сбрасывается в `research`; это самое хрупкое
     место задачи, `brief_from_form` без поля даёт «Науку»);
   - `routes/intake.py::create_request` — убрана передача `solution_kind=kind` в `run_intake_stage`
     (выбор формы вообще не доходит до интейка);
   - `_deidentification_warning.html` — убрано скрытое поле `solution_kind` (отступление исполнителя
     ради этого поля тестом не защищено).
   Тесты `test_solution_kind.py` проверяют слои ниже (resolve, run_intake, brief_store, статический
   `index.html`), но ни разу не гонят `POST /requests` и `POST /brief` с видом. Нужны тесты на
   TestClient (образец — `tests/test_web_intake.py`, заглушка роутера): (а) `POST /requests` с
   `solution_kind=digital` → вид доходит до промпта/черновика в базе; (б) подтверждение брифа
   сохраняет не-`research` вид, ушедший с черновиком; (в) страница предупреждения об обезличивании
   несёт скрытое поле `solution_kind`. Каждая из трёх мутаций должна ронять свой тест.

## Советы (не блокируют)
1. `_kind_kwargs` (intake_flow.py:154) дырой не является: для `research` умолчание `run_intake`/`refine_brief`
   то же самое, для остальных видов аргумент передаётся, мутация resolve его ловит. Но это
   «костыль ради заменителей старых тестов»: `run_intake_stage` получает `solution_kind="research"` и всё равно
   не передаёт его. Когда Э6-2а будет трогать эти заменители, стоит передавать всегда и убрать функцию.
   Ошибку она не прячет.
2. В `persistence_write.py` вид на insert не пишется (NULL = research) — исполнитель уже отметил; для Э6-2а
   добавить обязательно, иначе прогон из CLI потеряет вид.
3. Тест на заполнение метки в `constraints_guard` есть только косвенно (4 теста `test_constraints_guard.py`
   падают без `SOLUTION_KIND`, мутация поймана) — отдельного теста с `solution_kind != research` нет;
   по желанию добавить.

## Проверено
- Прогон (из корня копии, python из .venv jolly-wilbur): `pytest -q --deselect tests/test_docker_compose_backup_runtime.py` — 1409 passed, 0 failed; `ruff check .` — All checks passed; `ruff format --check .` — 222 files already formatted. Целевые `test_solution_kind.py + test_schemas_e6.py + test_prompts.py` — зелёные.
- Критерии 1–2: тесты есть по списку карточки (круг 4 видов, догон колонки + NULL→research, старый JSON без `analog_cards`, `"us"/"USA"/""` отвергаются, `resolve_solution_kind` на 5 входах); `EXPECTED_PLACEHOLDERS["intake"]` расширен, версия 1.4 = запись CHANGELOG.
- Критерий 3, мутация «resolve всегда отдаёт значение модели» → `test_solution_kind.py`: 8 failed (убита). Мутация «guard не передаёт SOLUTION_KIND» → 4 failed в `test_constraints_guard.py` (убита; без метки `render_prompt` падает, а guard глотает исключение — но тесты ловят). Откат `git checkout`.
- Критерий 4: `solution_kind` в planner/card_author/verifier/summary.md — нет; в `prompts/` только intake.md и CHANGELOG.md. Вне `pipeline/intake*`, `constraints_guard`, `brief_store`, `routes/intake`, `models`, `schemas` поле нигде не читается.
- Критерий 5 / правило 4: `git diff --stat b65bbb4 HEAD -- tests/` — test_prompts.py (1 строка) + два новых файла. «Наука»: `BRIEF_JSON` прежний (`exclude=True`, тест на идентичность дампа есть), стадии не видят поля.
- Инв. 4: все три вызова `render_prompt("intake")` заполняют метку (intake, refine, guard). Инв. 3: новых секретов нет (совпадения grep — прежние комментарии про Api-Key в config/llm). Инв. 6: httpx вне разрешённых мест — пусто. Инв. 7: сеть не нужна. Инв. 9: публичной формы и режима «Гос» на форме нет. Инв. 1, 2, 5, 10: не затронуты.
- Выбор человека сильнее модели; unknown → из `MARKET_KINDS`, иначе `product`; `research` от модели при unknown не проходит (тест есть); запасной бриф при отказе интейка получает выбор/`product`. `IntakeResult` снимает значение вне перечня — разбор не падает.
- Схемы: `AnalogCard`, `SourceType.ANALOG`, `Candidate.country`, `Report.analog_cards` — по контракту плана, без поведения; старый `report_json` без `analog_cards` читается с `[]`. Колонка `briefs.solution_kind` (nullable) в `ADDED_COLUMNS`.
- Файлы ≤500 строк (максимум routes/intake.py 435), типы/докстроки/русский соблюдены. Файл вне списка задачи: `_deidentification_warning.html` (заявлено в handoff, обоснование верное — без поля вид терялся бы; допускается, но см. находку 1).
- Это цикл 1; второй — последний.
