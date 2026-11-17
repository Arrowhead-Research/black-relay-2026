# rf-analytics service image: runs `rfanalytics live` (Kismet -> analytics -> MQTT).
# Built by central/docker-compose.yml (profile "analytics").
FROM ghcr.io/astral-sh/uv:0.5.11-python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra mqtt --no-editable

ENV PATH="/app/.venv/bin:$PATH"
COPY config /app/config
VOLUME ["/data"]
ENTRYPOINT ["rfanalytics"]
CMD ["--help"]
