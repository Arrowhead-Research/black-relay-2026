# Panel simulator + physical bridge bench, publishing to this stack's broker. Build context: detection/.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends mosquitto mosquitto-clients openssl curl sqlite3 procps \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY physical-subsystem /app/physical-subsystem
COPY fusion-service/scripts/physical-sim-bench.sh /app/fusion-service/scripts/physical-sim-bench.sh
RUN python3 -m venv /app/physical-subsystem/.venv \
    && /app/physical-subsystem/.venv/bin/pip install -q -e /app/physical-subsystem -r /app/physical-subsystem/harness/requirements.txt
COPY simulator-deploy/physical-sim/run.sh /app/run.sh
ENTRYPOINT ["/bin/bash", "/app/run.sh"]
