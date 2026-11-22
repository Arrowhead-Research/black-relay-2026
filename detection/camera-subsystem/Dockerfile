# One image for the camera node's software: the bridge and the camera link.
# Build context is detection/camera-subsystem. Entry points: camerabridge, camera-link.
# Python 3.13 on Debian trixie, pinned by digest (the multi-arch index of 3.13-slim-trixie).
ARG PYTHON=python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

FROM ${PYTHON} AS build
RUN pip install --no-cache-dir uv==0.11.32 && python -m venv /opt/venv
WORKDIR /src
COPY bridge/pyproject.toml bridge/uv.lock bridge/
COPY cameralink/pyproject.toml cameralink/uv.lock cameralink/
# Third-party packages exactly as the uv.lock files pin them, hashes checked.
RUN for p in bridge cameralink; do \
      (cd $p && uv export --frozen --no-dev --no-emit-project --no-header -o /tmp/$p.txt); \
      /opt/venv/bin/pip install --no-cache-dir --require-hashes -r /tmp/$p.txt; \
    done
COPY bridge/src bridge/src
COPY cameralink/src cameralink/src
# README.md etc. are not needed: the projects name no readme.
RUN /opt/venv/bin/pip install --no-cache-dir --no-deps ./bridge ./cameralink

FROM ${PYTHON}
# systemctl and timedatectl talk to the host's systemd over its sockets (no init runs here).
# docker-cli is for `docker inspect/logs frigate` through the mounted docker socket.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates systemd docker-cli \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1
LABEL org.opencontainers.image.title="black-relay camera node"
CMD ["camerabridge", "--help"]
