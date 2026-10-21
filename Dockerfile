FROM python:3.13-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.13-slim-bookworm
WORKDIR /app
RUN useradd --system --uid 10001 app
COPY --from=build /app/.venv /app/.venv
COPY ontology ./ontology
COPY evals ./evals
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER app
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"
CMD ["ontoagents", "serve"]
