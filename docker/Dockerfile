# Образ приложения. Собирается на этапе Э5-1 (docs/SPEC.md).

FROM python:3.12-slim

WORKDIR /srv/scout

COPY pyproject.toml README.md ./
COPY backend ./backend
COPY prompts ./prompts
COPY data/gov_support.yaml ./data/gov_support.yaml

RUN pip install --no-cache-dir .

ENV DATABASE_URL=sqlite+aiosqlite:////srv/scout/data/scout.db

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "backend"]
