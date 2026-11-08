# rf-console: WBT Console (live RF map, site workspaces). Build context is rf-subsystem/
# (it reuses the rfanalytics Kismet adapter); see central/docker-compose.yml.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY analytics/pyproject.toml analytics/README.md /src/analytics/
COPY analytics/src /src/analytics/src
RUN pip install --no-cache-dir "/src/analytics[console]" && rm -rf /src

COPY console/server.py console/sites.py console/auth.py console/demo.py console/locate.py console/layout.example.yaml ./
COPY console/static ./static

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/session', timeout=4)"
CMD ["python", "server.py"]
