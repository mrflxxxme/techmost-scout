"""Проверка Т-17 в условиях слитого Т-14: rospatent объявлен рабочим.

Живых вызовов нет — меняется только атрибут `implemented` класса адаптера в
памяти процесса, ровно то, что даёт слияние Т-14. Файлы не трогаются.
"""

import sys

sys.path.insert(0, r"C:\Users\KUklonskiy\techmost-scout\.claude\worktrees\E2A-T17\backend")
sys.path.insert(0, r"C:\Users\KUklonskiy\techmost-scout\.claude\worktrees\E2A-T17\tests")

from app.search import ADAPTERS  # noqa: E402

ADAPTERS["rospatent"].implemented = True

import test_plan_types as t  # noqa: E402

print("rospatent.implemented =", ADAPTERS["rospatent"].implemented)
print("типы промпта      :", sorted(t._planner_allowed_types()))
print("типы адаптеров    :", sorted(t._implemented_adapter_source_types()))
for name in (
    "test_каждый_тип_промпта_обслуживается_рабочим_адаптером",
    "test_каждый_рабочий_адаптер_достижим_типом_из_промпта",
    "test_типы_промпта_подмножество_source_type",
    "test_патентный_запрос_один_и_на_русском",
    "test_патент_достижим_обоими_патентными_адаптерами",
    "test_версия_planner_совпадает_с_changelog",
):
    getattr(t, name)()
    print("PASSED", name)
