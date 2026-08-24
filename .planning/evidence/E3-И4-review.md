# Ревью И-4 · цикл 1
Вердикт: ПРИНЯТЬ

## Проверено (сам прогнал)
- `pytest tests/test_cli_run.py -v`: 22 passed
- `git diff 8122125..e3/i4 --stat -- tests/test_cli_run.py`: пусто (файл не тронут — ключевое доказательство неизменности поведения CLI)
- `pytest tests/test_intake_flow.py -v`: 6 passed (обе ветки отказа, оба стоп-крана, пустые ответы, успешный путь без оговорок)
- `pytest -q`: 881 passed
- `ruff check .`: All checks passed
- `ruff format --check .`: 158 files already formatted
- `pytest tests/test_backend_layout.py -v`: 3 passed
- `pytest tests/test_team_docs.py -v`: 35 passed
- `wc -l`: cli_run.py 301, intake_flow.py 327 (оба ≤500)
- `grep -rnE '(status=|status = )"(intake|briefing|searching|drafted|validated|exported|failed)"' backend/app --include='*.py'`: пусто
- `grep -nE "console|print\(|typer" backend/app/pipeline/intake_flow.py`: только упоминание `typer.prompt` в докстроке как текст, ни print, ни импорта console/typer нет
- `grep -rE "sk-|gigachat|Api-Key" backend/ tests/`: только существующие некритичные совпадения (заголовки авторизации Yandex/PatentsView, тестовые фиктивные ключи) — нового не добавлено
- `grep -rln "httpx" backend/app --include='*.py' | grep -vE "...(llm/|search/|cli_doctor.py|pipeline_context.py|pipeline_search.py)"`: пусто
- Импорт-цепочка: `from app.cli_run import (INTAKE_FALLBACK_CAVEAT, ...)` в cli.py жив; `backend/app/cli.py` вообще не тронут диффом (`git diff --stat` по нему пуст)
- Diff по файлам ровно соответствует списку задачи: `backend/app/pipeline/intake_flow.py` (новый), `backend/app/cli_run.py`, `tests/test_intake_flow.py` (новый), плюс evidence-файлы исполнителя

## Поведенческая эквивалентность (построчное сравнение с базой)
Сравнил новый `intake_flow.py` + переписанный `cli_run.py` со старым `cli_run.py`
(база 8122125, строки ~181–487) по смыслу:
- Порядок стадий (`run_intake` → диалог у вызывающего → `refine_brief` →
  `retry_constraints_if_missing`) сохранён.
- `INTAKE_FALLBACK_CAVEAT` и `REFINE_FALLBACK_CAVEAT` — побайтово те же строки
  (сверено `diff` по исходным константам), добавляются в тех же ветках отказа.
- `CONSTRAINTS_UNRESOLVED_NOTICE` из `export/markdown.py` — файл не тронут,
  используется так же.
- Оба стоп-крана (`CostLimitExceededError` на `run_intake`, на `refine_brief`,
  на `retry_constraints_if_missing`) пробрасываются наружу как есть и перед
  этим пишут след `_persist_intake_stop` с `RequestStatus.FAILED` (было —
  литерал `"failed"`, теперь перечисление — ожидаемая правка по границе И-1).
  `record=None` для `resolve_constraints_stage`, чтобы не задваивать запись —
  сохранено дословно, включая объяснение в докстроке.
- `neutralize_placeholders` по-прежнему вызывается в `cli_run.py` при
  явном `--industry` (инвариант №4) — код не переехал и не потерян.
- Тексты предупреждений в лог и в консоль различаются («с ответами владельца»
  в логе vs «с вашими ответами» в консоли) — это сохранено из базы дословно,
  не унифицировано и не переформулировано.
- Модуль `intake_flow.py` не печатает и не спрашивает: сам не зовёт `console`,
  `typer.prompt`; вопросы и печать остались в `cli_run._run`.

## Мутационная проверка
Убрал `REFINE_FALLBACK_CAVEAT` из ветки отказа `refine_brief_stage` (вернул
`caveats=[]` вместо `caveats=[REFINE_FALLBACK_CAVEAT]`). Прогон
`tests/test_intake_flow.py tests/test_cli_run.py`: упал
`test_refine_brief_failure_keeps_owner_answers_in_own_field` (ожидаемая оговорка
пропала). `tests/test_cli_run.py` мутацию не поймал (22 passed) — целиком
`pytest -q` не гонял на мутации, достаточно того, что новый прицельный тест
модуля её ловит; это ожидаемо, так как `test_cli_run.py` не бьётся с новым
модулем напрямую, только через реэкспорт функций. Правка отменена,
`git diff --stat` пуст, тесты снова зелёные.

## Блокирующие
Нет.

## Советы (не блокируют)
1. В `refine_brief_stage` текст для `log.warning` продублирован как отдельный
   литерал вместо переиспользования переменной `warning` (в базовом коде было
   так же — лог и консоль несли разные формулировки, — но теперь, когда оба
   текста живут рядом в одной функции, разница видна отчётливее и может
   ввести в заблуждение при будущей правке).
2. Замечание исполнителя в handoff про устаревший docstring `cli.py` (строки
   7–17, всё ещё описывает интейк как код `cli_run.py` дословно) — за
   границами задачи, справедливо не тронуто; стоит завести отдельной строкой
   в бэклог доводки волны 1.
