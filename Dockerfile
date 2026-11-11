# Base images are pinned by digest by the pin-base-images workflow and kept current by
# Dependabot (audit L14, #336); uv has a stage of its own so Dependabot sees it.
FROM ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c AS uv

FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
WORKDIR /app
RUN useradd --system --uid 10001 kelpie && mkdir -m 0700 /data && chown kelpie /data
COPY --from=build --chown=kelpie /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
# Listen on all interfaces inside the container; publish the port deliberately.
ENV KELPIE_HOST=0.0.0.0
USER kelpie
EXPOSE 9464
ENTRYPOINT ["kelpie"]
CMD ["serve"]
