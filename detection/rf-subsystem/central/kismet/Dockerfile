# Central Kismet server for the RF subsystem.
#
# Only the server and log tools are installed. Capture helpers run on the WBT
# nodes (or on a stand-in Linux machine) and connect to this server remotely.
#
# The Kismet version is pinned. Nodes must run capture helpers from the same
# release (see DESIGN.md, section 7.1). The Kismet apt repository only carries
# the current release, so bumping this value is required when the repo moves on.
FROM debian:bookworm-slim

ARG KISMET_VERSION=2026-10-R1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && curl -fsSL https://www.kismetwireless.net/repos/kismet-release.gpg.key \
      | gpg --dearmor -o /usr/share/keyrings/kismet-archive-keyring.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/kismet-archive-keyring.gpg] https://www.kismetwireless.net/repos/apt/release/bookworm bookworm main" \
      > /etc/apt/sources.list.d/kismet.list \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      kismet-core=${KISMET_VERSION} \
      kismet-logtools=${KISMET_VERSION} \
 && apt-get purge -y gnupg \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY kismet_site.conf /etc/kismet/kismet_site.conf
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh && mkdir -p /var/lib/kismet/logs

# 2501: web UI, REST API, and WebSocket remote capture.
# 3501: legacy TCP remote capture.
EXPOSE 2501 3501
VOLUME ["/var/lib/kismet", "/root/.kismet"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
