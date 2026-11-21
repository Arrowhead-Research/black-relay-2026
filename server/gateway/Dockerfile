# One exact base image, by tag and digest, so what runs is what was scanned (T-HARDEN-15). Alpine,
# not Debian: the Debian 13 base carried 206 findings with no fix published (61 high), every one in
# a system package the gateway never runs (util-linux, login, mount, perl, glibc). This base carries
# 3, none high. To move on, change the tag and digest together, rebuild, and rescan the image.
FROM ghcr.io/astral-sh/uv:python3.13-alpine@sha256:f2d6278d0821c35881b010ddbf75825049661a98dbfdf61bbd1407e8dd544d00

# Alpine's fixes published since the base image was built. The base holds Python's libraries through
# one package, .python-rundeps; naming the ones we use first keeps them when it is removed, and drops
# the rest. sqlite-libs stays: the registry (registry.py) is a SQLite file.
RUN apk upgrade --no-cache \
 && apk add --no-cache gdbm libbz2 libcrypto3 libffi libssl3 libuuid ncurses-libs readline sqlite-libs xz-libs zlib \
 && apk del --no-cache .python-rundeps

# The base image's own pip is never used here (uv installs everything) and carried findings.
RUN python -m pip uninstall -y pip

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

# Not root (trivy DS002). The pki service installs the gateway's certificates for this uid on the gateway-certs volume.
RUN adduser -S -u 10001 -H gateway

# The registry (houses, hashed keys, sensors) lives here: mount a volume, or it dies with the container.
# A new named volume takes this directory's owner, so the non-root user can write it.
RUN mkdir /data && chown 10001 /data
ENV REGISTRY_DB=/data/registry.db

USER 10001
