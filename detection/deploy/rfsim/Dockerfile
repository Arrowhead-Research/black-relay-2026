# RF observation simulator. Build context: rf-subsystem/simulator.
FROM ghcr.io/astral-sh/uv:0.5.11-python3.12-bookworm-slim
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra mqtt --no-editable
COPY scenarios ./scenarios
COPY sites ./sites
ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["rfsim"]
CMD ["--help"]
