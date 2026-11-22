# Small toolbox for the central store scripts: age (encryption), oras (OCI artifacts),
# htpasswd (registry users), openssl (the store's TLS CA); bash + mosquitto_passwd for the physical bridge's
# bootstrap service (detection/physical-subsystem/deploy/scripts/bootstrap.sh). Nothing secret is ever baked in.
FROM ghcr.io/oras-project/oras:v1.3.0@sha256:6ce045ce069a89934d6666b8b49f9c4c0145201bd6de6dbe2aee267814c55468 AS oras

FROM alpine:3.22@sha256:49340b346798875b5592e5d420241f2f9cfba5f47e62ebb1446a1b34734e898f
RUN apk add --no-cache age apache2-utils openssl tar bash mosquitto
COPY --from=oras /bin/oras /usr/local/bin/oras
