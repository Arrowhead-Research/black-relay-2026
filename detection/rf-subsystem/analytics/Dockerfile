# rf-analytics service image: runs `rfanalytics live` (Kismet -> analytics -> MQTT).
# Runs on the central server from the repo-root compose.yaml (service rf-analytics, profile "rf");
# published as ghcr.io/arrowhead-research/black-relay/rf-analytics by publish-images.yml.
FROM ghcr.io/astral-sh/uv:0.5.11-python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra mqtt --no-editable

ENV PATH="/app/.venv/bin:$PATH"
COPY config /app/config
# Unprivileged; /data (outbox, events) belongs to it so a new named volume inherits that owner.
RUN useradd --system --uid 10020 --home-dir /data --shell /usr/sbin/nologin rfanalytics \
    && mkdir -p /data && chown rfanalytics:rfanalytics /data
USER rfanalytics
VOLUME ["/data"]
ENTRYPOINT ["rfanalytics"]
CMD ["--help"]
