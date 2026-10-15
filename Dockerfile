FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm
WORKDIR /app
RUN useradd --system --uid 10001 kelpie && mkdir /data && chown kelpie /data
COPY --from=build --chown=kelpie /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
USER kelpie
EXPOSE 9464
ENTRYPOINT ["kelpie"]
CMD ["serve"]
