# Multi-arch: python:3.13.13-slim-trixie has linux/amd64 and linux/arm64. --only-binary means no
# compiler is needed on either, and the lock file's hashes cover both architectures' wheels.
FROM python:3.13.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src RELAY_SCHEMA_DIR=/app/schemas

WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes --only-binary :all: -r requirements.lock
COPY src ./src
# compose.yaml gives this build the contract's own schemas, so the API cannot hold a stale copy.
COPY --from=schemas . ./schemas

RUN useradd --uid 10001 --no-create-home relay && install -d -o relay -g relay /data
USER relay

EXPOSE 8080
CMD ["uvicorn", "--factory", "relayapi.app:create_app", "--host", "0.0.0.0", "--port", "8080", \
     "--no-server-header", "--no-access-log", "--timeout-graceful-shutdown", "5"]
