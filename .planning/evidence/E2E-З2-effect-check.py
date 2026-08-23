"""Проверка эффекта РЕШ-162 на сохранённых данных `data/scout.db` (только чтение).

Задача З-2, волна Э2-Е. Скрипт не пишет в базу (`?mode=ro`), не обращается в
сеть, считает измерителем `app.metrics.count_at_least_relevance` — тем же кодом,
который правит задача, — по кандидатам общего отбора (без конкурсного класса,
`source_type != "contest"`; конкурсные кандидаты в `select_top` не участвуют,
РЕШ-92, `pipeline_selection_flow.split_contest_candidates`).

Запуск: .venv/bin/python .planning/evidence/E2E-З2-effect-check.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.metrics import count_at_least_relevance  # noqa: E402

# `data/*.db` не версионируется (`.gitignore`) и не копируется в git worktree —
# файл существует только в основном каталоге репозитория. Путь абсолютный
# сознательно: относительный путь от воркспейса указывал бы на несуществующий
# `data/scout.db` внутри рабочей копии задачи.
DB_PATH = Path("/home/user/techmost-scout/data/scout.db")

REQUESTS = [(1, "Э2-Г"), (2, "Э2-Д")]


def main() -> None:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    cur = con.cursor()
    for request_id, label in REQUESTS:
        cur.execute(
            "SELECT score FROM candidates WHERE request_id = ? AND source_type != 'contest'",
            (request_id,),
        )
        scores = [json.loads(row[0]) for row in cur.fetchall()]
        count_4 = count_at_least_relevance(scores, minimum=4)
        count_3 = count_at_least_relevance(scores, minimum=3)
        print(
            f"request_id={request_id} ({label}): кандидатов в общем отборе={len(scores)}, "
            f"порог4={count_4}, порог3={count_3}"
        )


if __name__ == "__main__":
    main()
