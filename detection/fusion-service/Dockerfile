# Build from detection/: docker build -f fusion-service/Dockerfile .
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.11 /uv /usr/local/bin/uv
WORKDIR /app
COPY fusion-service/pyproject.toml fusion-service/uv.lock ./
COPY fusion-service/src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
RUN groupadd --gid 10001 fusion && useradd --uid 10001 --gid fusion --no-create-home fusion \
    && mkdir -p /data /config /contracts/camera /contracts/rf /contracts/physical && chown fusion:fusion /data
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY camera-subsystem/bridge/schemas/ /contracts/camera/
COPY rf-subsystem/schemas/ /contracts/rf/
COPY physical-subsystem/schemas/ /contracts/physical/
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER fusion
EXPOSE 8090
ENTRYPOINT ["fusion-service"]
CMD ["serve", "--config", "/config/config.yaml", "--host", "0.0.0.0", "--port", "8090"]
