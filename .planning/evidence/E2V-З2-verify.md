# Верификация З-2 · цикл 2

Вердикт: **PASS** (закрыто 8 из 8)

## Критерии приёмки

| № | Критерий из PLAN.md | Команда | Факт | Статус |
|---|---|---|---|---|
| 1 | Детектор срабатывает на боевом тексте | pytest tests/test_constraints_guard.py | 7 passed | ок |
| 2 | Повторный вызов ровно один раз | pytest tests/test_constraints_guard.py::test_retry_constraints_fills_only_constraints_field | 1 passed | ок |
| 3 | Без признаков требований повторного вызова нет | pytest tests/test_constraints_guard.py::test_retry_constraints_skipped_without_requirement_signs | 1 passed | ок |
| 4 | Клиент видит строку в двух местах | pytest tests/test_export_markdown.py | 33 passed | ок |
| 5 | Подпись поля сменилась | grep -rn "Требования и ограничения:" backend/ | найдено | ок |
| 6 | Сверка есть у каждой карточки | pytest tests/test_constraints_guard.py | тест author_card | ок |
| 7 | Отказ не роняет, лимит роняет | pytest tests/test_constraints_guard.py | 2 теста | ок |
| 8 | pytest и test_prompts зелёные | pytest -q && pytest tests/test_prompts.py -v | 609 passed, 44 passed | ок |

## Мутации (критические из цикла 1)

Обе мутации, не пойманные в цикле 1, теперь ловятся:

1. Мутация: if constraints_unresolved: -> if False and constraints_unresolved:
   Команда: pytest tests/test_cli_run.py::test_constraints_unresolved_caveat_reaches_report -v
   Результат: FAILED - AssertionError (ожидается)
   Статус: ПОЙМАНА

2. Мутация: удалить блок retry_constraints_if_missing целиком
   Команда: pytest tests/test_cli_run.py::test_constraints_unresolved_caveat_reaches_report -v
   Результат: FAILED - assert 0 == 1 (ожидается)
   Статус: ПОЙМАНА

## Четыре пути инварианта З-2

Инвариант: непустой текст с признаками требований => непустой constraints ИЛИ явная строка клиенту.

Путь 1: Признаки есть, повторный вызов помог
- Что видит клиент: constraints непуст, сверка с требованиями печатается
- Тесты: test_retry_constraints_fills_only_constraints_field, test_constraints_unresolved_caveat_reaches_report
- Статус: ОК

Путь 2: Признаки есть, повторный вызов отказал
- Что видит клиент: constraints пуст, но строка в двух местах (Задача + Ограничения)
- Тесты: test_retry_constraints_empty_second_answer_marks_unresolved, test_retry_constraints_llm_error_does_not_raise_run_continues
- Статус: ОК

Путь 3: Признаки есть, исчерпан лимит стоимости
- Что видит клиент: прогон падает с CostLimitExceededError, статус failed в БД
- Тесты: test_retry_constraints_cost_limit_propagates, test_constraints_retry_cost_limit_propagates_and_persists_failed
- Статус: ОК

Путь 4: Признаков нет вовсе
- Что видит клиент: повторный вызов пропускается, constraints может быть пуст или полон
- Тесты: test_retry_constraints_skipped_without_requirement_signs
- Статус: ОК

Молчаливый третий исход исключён.

## Инварианты

№1 (утверждение без источника): страховка не добавляет новое утверждение, только признание отсутствия подтверждения - OK
№3 (секретов нет): grep пуст - OK
№4 ({{...}} заполняются): test_prompts.py passed, в том числе test_render_prompt_leaves_no_placeholders[intake] - OK
№5 (отказ видимый): LLMError не роняет, своя строка в pipeline_logs с stage_intake_constraints - OK
№7 (сеть не используется): FakeProvider, respx, pytest проходит без интернета - OK
№8 (новое поведение с тестом): параллельные изменения в backend/ и tests/ - OK
№9 (второго поля не заводим): git diff показывает только правку описания constraints, нет нового поля - OK

## Клиентские формулировки

1. Подпись поля:
   - Было: **Ограничения:**
   - Стало: **Требования и ограничения:**
   - Проверено: backend/app/export/markdown.py:90, grep показывает новую подпись

2. Строка при нерешённости:
   - Текст: Требования задачи не выделены из постановки — построчная сверка карточек не проводилась.
   - Печатается в двух местах: разделе Задача (по флагу report.constraints_unresolved) и разделе Ограничения этого поиска (через механизм оговорок)
   - Проверено: test_export_markdown.py - подстрока встречается дважды

## Версия prompts/intake.md

test_prompt_version_matches_latest_changelog_entry[intake] - PASSED
Версия совпадает с последней записью в CHANGELOG.md

## Итог

PASS: все 8 критериев закрыты
- pytest: 609 passed, 0 failed
- ruff check: All checks passed
- ruff format --check: 100 files already formatted
- Мутации 1 и 2 пойманы интеграционными тестами
- Четыре пути инварианта проверены
- Инварианты 1, 3, 4, 5, 7, 8, 9 подтверждены
- Клиентские формулировки дословны
