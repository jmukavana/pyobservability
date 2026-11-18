# Build from the repository root:
#   docker build -f examples/fastapi/Dockerfile -t pyobs-fastapi-example .
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-group extras --extra fastapi --extra otlp \
    && uv pip install --python /opt/venv/bin/python uvicorn

COPY examples/fastapi ./examples/fastapi

RUN useradd --uid 10001 --no-create-home --home-dir /app app \
    && mkdir /app/.audit-spool \
    && chown app /app/.audit-spool
USER app
VOLUME /app/.audit-spool

EXPOSE 8000
CMD ["uvicorn", "examples.fastapi.app:app", "--host", "0.0.0.0", "--port", "8000"]
