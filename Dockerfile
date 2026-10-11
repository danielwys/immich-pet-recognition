FROM ghcr.io/astral-sh/uv:0.12.19 AS uv
FROM python:3.12-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY scripts ./scripts
RUN uv sync --frozen --no-dev --extra cpu && \
    useradd --uid 10001 --create-home pets && mkdir /data && chown pets:pets /data
ENV PATH="/app/.venv/bin:$PATH" PET_DATA_DIR=/data PYTHONUNBUFFERED=1
USER pets
EXPOSE 8090
CMD ["immich-pets", "--host", "0.0.0.0", "--port", "8090"]
