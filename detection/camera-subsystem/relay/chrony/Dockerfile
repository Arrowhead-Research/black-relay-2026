# Multi-arch: alpine:3.23.8 has linux/amd64 and linux/arm64. chrony's upstream version is pinned.
# Alpine replaces a package's revision (-rN) in place, so pinning that too would break a rebuild.
FROM alpine:3.23.8
RUN apk add --no-cache 'chrony~4.8'
COPY chrony.conf /etc/chrony/chrony.conf
EXPOSE 123/udp
# -x: never adjust the clock, so no clock privilege is needed. -d: stay in the foreground.
ENTRYPOINT ["chronyd", "-d", "-x", "-f", "/etc/chrony/chrony.conf"]
