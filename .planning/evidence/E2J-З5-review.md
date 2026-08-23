# Ревью З-5 · Э2-Ж · цикл 1
Вердикт: ПРИНЯТЬ

## Проверено по существу

1. **google_search удалён.** `backend/app/search/google_search.py` и
   `tests/test_search_google.py` удалены целиком; реестр `search/__init__.py`
   (ADAPTERS, REQUIRED_SETTINGS, CONSTRUCTOR_SETTINGS, `__all__`) вычищен;
   `config.py` — поля `google_api_key`/`google_cse_id`/`google_lang_restrict`
   убраны из `Settings`. Мёртвая ссылка в докстроке `yandex_search.py` (была
   `backend/app/search/google_search.py`) заменена на `docs/sources.md` — это
   правка ссылки, не логики: сам код адаптера Яндекса не тронут (проверено
   построчным диффом файла). `grep -rni google backend/app --include='*.py'`
   даёт только `patents.google.com` в `rospatent.py`/`patents_view.py`
   (легитимно — карточка ссылки на Google Patents, не Custom Search) и
   объяснительные комментарии про удаление в `cli.py`/`config.py`/
   `yandex_search.py`. Настроек `google_*` в `Settings` нет.

2. **Колонки tokens_in/tokens_out.** Убраны из `models.py` (`PipelineLog`),
   `pipeline/persistence.py` (`StageRecord` и запись `PipelineLogRow`).
   `grep -n 'tokens_in\|tokens_out' backend/app -r` находит только
   `llm/base.py` (`Usage`, `estimate_cost`), `llm/deepseek.py`, `llm/yandex.py`
   — это другая сущность (учёт токенов роутером моделей для расчёта
   стоимости), она не тронута, и это правильно: границы задачи не нарушены.

3. **Порог relevance_min.** `Settings.model_fields['relevance_min'].default
   == 3` — подтверждено и в проверке исполнителя, и своим прогоном
   (`test_relevance_min_default_is_three`, ранее
   `test_relevance_min_default_is_zero_cutoff_disabled` — тест не исчез, а
   переименован и закрепляет новое значение с честным объяснением в
   докстроке). `test_contest_gate_and_order.py`:
   `test_при_выключенной_отсечке_конкурсная_очередь_прежняя` теперь явно
   выключает порог через `settings.model_copy(update={"relevance_min": 0})` —
   тест по-прежнему проверяет ту же логику (слабый конкурсный кандидат
   доезжает до секции при выключенной отсечке), просто больше не полагается
   на умолчание. Выхолащивания нет — условие теста то же, входные данные
   явные. `test_pipeline_second_pass.py` аналогично: `monkeypatch.setenv` +
   `get_settings.cache_clear()`, с honest-докстрокой о том, что `select_top`
   читает порог из закэшированных `get_settings()`, а не из объекта
   `settings` пайплайна — это существующее поведение кода, не побочный эффект
   правки. `tests/test_metrics.py` не тронут и зелёный (12 passed) — связка
   измеритель/отбор не пострадала.

4. **Инвариант 6.** Новая формулировка в `INVARIANTS.md` описывает
   фактическую карту (`llm/`, `search/`, `cli.py`, `pipeline_context.py`,
   `pipeline_search.py`). Прогнал грепом из формулировки сам:
   `grep -rln 'httpx' backend/app --include='*.py' | grep -vE
   'backend/app/(llm/|search/|cli\.py$|pipeline/pipeline_context\.py$|pipeline/pipeline_search\.py$)'`
   — пусто, 0 срабатываний. `pytest tests/test_team_docs.py -q` — 35 passed,
   таблица инвариантов не сломана.

5. **Страж 500 (`tests/test_backend_layout.py`).** Рекурсивный обход
   `backend/app`, список исключений — ровно три файла
   (`cli.py`, `search/openalex.py`, `search/yandex_search.py`), записаны
   парами имя→текущий размер. Второй тест
   (`test_exceptions_do_not_grow_unnoticed`) сравнивает фактический размер
   каждого исключения с зафиксированным и падает, если он вырос — это
   сильнее, чем просто «вычесть файлы из проверки»: список исключений не
   тихая дыра, а замороженный на дату задачи предел, и рост `cli.py` (или
   любого другого исключения) выше сегодняшнего размера будет пойман именно
   этим тестом, а не общим лимитом 500 (в общий лимит эти файлы не попадают
   вовсе, пока их имя в `_EXCEPTIONS`). Третий тест
   (`test_backend_app_directory_is_not_empty`) страхует от тихого прохождения
   на пустом каталоге — по образцу `test_scripts_layout.py`, как и требовала
   карточка.
   Мутацию (лимит 400 вместо 500) прогнал сам, без правки файла — реализовал
   ту же логику в отдельном скрипте на актуальном дереве:
   результат — **четыре** файла, а не три: `pipeline/persistence.py` (407),
   `pipeline/pipeline_search.py` (423), `pipeline/run_flow.py` (498),
   `search/cyberleninka.py` (431). Это заранее оговорённое обстоятельство
   (см. постановку задачи ревью) — карточка ссылалась на состояние кода на
   момент планирования, сейчас оно иное; исполнитель назвал числа честно, не
   подгонял вывод. Страж работает: при лимите 500 все четыре укладываются, ни
   один не считается нарушением.

6. **cli.py в диффе.** Явного упоминания `cli.py` в списке файлов карточки
   З-5 нет, но: (а) план сам предупреждает об этом в разделе «Порядок и
   параллельность» — «З-5 идёт после З-3 сознательно: обе трогают config.py и
   cli.py»; (б) правка необходима по существу, а не косметически: доктор
   строит таблицу `SECRET_CHECKS` и для каждой строки вызывает
   `settings.has_secret(field_name)` (`cli.py:208`, `config.py:146-150`,
   `getattr(self, name, None)`). Не убери исполнитель строки
   `GOOGLE_API_KEY`/`GOOGLE_CSE_ID` из `SECRET_CHECKS`, доктор продолжил бы
   молча (без падения — `getattr` с default) показывать «ключ не задан» для
   настройки, которой в `Settings` больше не существует, — то есть врал бы
   пользователю про несуществующую возможность, что прямо противоречит цели
   самой задачи («держать в докторе строку про несуществующие настройки
   незачем»). Правка минимальна: только строки `SECRET_CHECKS` про Google и
   докстрока `_intake_stage_record` про `tokens_in`/`tokens_out` (прямое
   следствие пункта 2). Считаю правку обоснованной и не выходом за границы
   задачи.

7. **Числа тестов.** Полный прогон в копии: `745 passed, 1 failed`. Базовая
   линия задачи — 760 passed, 0 failed (проверено живым прогоном `pytest -q`
   во временном git worktree на коммите-базе 1dfb2b8, не через слова
   исполнителя). Разница объясняется до последнего теста:
   - минус 15 тестовых элементов `tests/test_search_google.py` (11 функций,
     из них две параметризованы: `test_исчерпанная_квота_не_роняет_пайплайн`
     ×2, `test_нормализация_адреса` ×4 — итого 9+2+4=15 удалённых кейсов);
   - минус 2 параметризованных кейса `tests/test_search_adapters.py` для
     `google_search` (тест генерируется по живому реестру `ADAPTERS`, файл в
     диффе не значится — это ожидаемое автоматическое следствие удаления
     адаптера из реестра, а не потерянная проверка);
   - плюс 3 новых теста `tests/test_backend_layout.py`;
   - net: 760 − 15 − 2 + 3 = 746 итоговых тестовых элементов — ровно то, что
     собирает `pytest --collect-only -q` в копии (746). Внутри этих 746:
     745 passed + 1 known failed. Переименования тестов (`test_openalex.py`,
     `test_search_yandex.py`, `test_stages.py`) дают net 0 к количеству —
     сверено `git show <rev>:<file> | grep -c '^def test_\|^async def test_'`
     на базе и на диффе.
   1 failed — единственный красный, ожидаемый:
   `tests/test_config.py::test_в_примере_нет_переменных_которых_settings_не_читает`
   (`GOOGLE_API_KEY`, `GOOGLE_CSE_ID`, `GOOGLE_LANG_RESTRICT` остались в
   `.env.example`, недоступном исполнителю). Файл `tests/test_config.py` не
   тронут диффом (`git diff 1dfb2b8 749fc72 -- tests/test_config.py` — пусто)
   — тест не подмаркирован xfail и не изменён, ровно то, что было оговорено.

## Инварианты 1–10

1 — не применимо (карточки/`evidence` не тронуты). 2 — не применимо (черновик
не тронут). 3 — секретов не найдено; `grep -rE "sk-|gigachat|Api-Key"
backend/ tests/` даёт только легитимные строки (заголовки `Api-Key` в
адаптерах, тестовые фиктивные `sk-test`/`sk-fake-leak-check-777`), значения
боевых ключей нигде не печатаются. 4 — не применимо (промпты не тронуты);
`pytest tests/test_prompts.py -q` — 44 passed. 5 — не применимо напрямую
(адаптер удалён целиком, а не исправлен); оставшиеся адаптеры не тронуты. 6 —
проверен и подтверждён (см. п. 4 выше), формулировка починена, суть та же. 7
— тесты не обращаются в сеть, новых внешних URL не добавлено. 8 — на каждое
изменение в `backend/` есть изменение в `tests/` (config.py/cli.py/models.py/
persistence.py/search — покрыты test_contest_gate_and_order.py,
test_openalex.py, test_pipeline_second_pass.py, test_search_yandex.py,
test_stages.py, test_backend_layout.py). 9 — diff сверен с картой файлов
задачи; единственное расхождение (`cli.py`) разобрано в п. 6 и признано
обоснованным, решение об умолчании `relevance_min=3` — решение владельца
(РЕШ-174), не самодеятельность. 10 — `pytest tests/test_team_docs.py` зелёный
(35 passed), новый и изменённые YAML/frontmatter не проверялись отдельно,
т.к. диффом тронут только один YAML-файл (`E2J-З5-handoff.yaml`) — прошёл
`yaml.safe_load` без ошибок.

## Handoff исполнителя
Артефакты существуют (список `artifacts` сверен с `git diff --name-only`,
расхождение только в `cli.py`, разобрано выше и явно названо самим
исполнителем в `notes` — не скрыто). `checks` — фактический вывод команд, не
слова на веру (перепроверено самостоятельно, числа совпали). `self_audit:
pass` и `escalate: none` не противоречат друг другу: единственное отклонение
от карточки (мутация даёт 4 файла, а не 3) — заранее известное расхождение
формулировки задачи с фактическим состоянием кода, не дефект исполнения;
второе (cli.py вне явного списка) — исполнитель сам вынес вопрос на решение
ревьюера в `notes`, это корректная эскалация внутри отчёта, а не сокрытие.

## Советы (не блокируют)
1. `docs/sources.md`: новый раздел про Google неплохо бы прямой ссылкой
   указывал на строку РЕШ-174 в `PLAN.md`/`DECISIONS.md` — сейчас ссылка есть,
   претензий нет, это просто отметка для следующего читателя.
2. В `test_backend_layout.py` можно было бы добавить прямой комментарий-пример
   мутации (лимит 400 → 4 файла) в докстроку теста, чтобы будущий ревьюер не
   пересчитывал вручную — сейчас это работа ревью, не блокирует приёмку.

## Проверено (команды и вывод)
- `pytest -q` (копия, коммит 749fc72): `745 passed, 1 failed in 45.62s` —
  единственный красный: `test_config.py::test_в_примере_нет_переменных_которых_settings_не_читает`
  (ожидаемо, `.env.example` вне зоны исполнителя).
- `pytest -q` (временный git worktree на базе 1dfb2b8): `760 passed in 46.98s`
  — подтверждена базовая линия задачи независимо от слов исполнителя.
- `pytest --collect-only -q` копия/база: 746 / 760 — разница объяснена по
  файлам в п. 7 отчёта.
- `ruff check .`: `All checks passed!`
- `ruff format --check .`: `103 files already formatted`
- `pytest tests/test_team_docs.py -q`: `35 passed`
- `pytest tests/test_metrics.py -q`: `12 passed`
- `pytest tests/test_backend_layout.py -v`: 3 passed (без мутации);
  мутация лимита до 400 (прогнана отдельным скриптом, файл теста не менялся)
  → `[('pipeline/persistence.py', 407), ('pipeline/pipeline_search.py', 423),
  ('pipeline/run_flow.py', 498), ('search/cyberleninka.py', 431)]`.
- `grep -rni 'google' backend/app --include='*.py'`: только
  `patents.google.com` (rospatent.py, patents_view.py) и комментарии про
  удаление (cli.py, config.py, yandex_search.py).
- `grep -n 'tokens_in\|tokens_out' backend/app -r`: только llm/base.py,
  llm/deepseek.py, llm/yandex.py (Usage роутера, вне границ задачи).
- `grep -rln 'httpx' backend/app --include='*.py' | grep -vE
  'backend/app/(llm/|search/|cli\.py$|pipeline/pipeline_context\.py$|pipeline/pipeline_search\.py$)'`:
  пусто.
- `grep -rE "sk-|gigachat|Api-Key" backend/ tests/`: только легитимные
  упоминания (заголовки авторизации, фиктивные тестовые ключи), боевых
  значений нет.
- `git diff 1dfb2b8 749fc72 -- tests/test_config.py`: пусто (тест не тронут).
