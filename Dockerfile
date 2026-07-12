FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY hackqueue ./hackqueue
# [postgres] so the same image works for SQLite (default) and Postgres
RUN pip install '.[postgres]'

# Default scoring config; override by mounting your own at /app/scoring.toml
COPY scoring.toml ./scoring.toml

RUN useradd --create-home --uid 1000 hackqueue \
    && mkdir -p /app/data \
    && chown -R hackqueue:hackqueue /app/data
USER hackqueue

VOLUME ["/app/data"]

CMD ["hackqueue"]
