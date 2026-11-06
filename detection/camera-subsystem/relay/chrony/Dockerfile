# Multi-arch: alpine:3.23.8 has linux/amd64 and linux/arm64. chrony's exact revision is pinned, and
# 4.8-r2 is in both architectures' index. Alpine keeps only the newest revision, so once it replaces
# this one a build stops here, by design, until README.md's Install section has been followed.
FROM alpine:3.23.8
ARG RELAY_CHRONY_VERSION=4.8-r2
RUN apk add --no-cache "chrony=${RELAY_CHRONY_VERSION}"
COPY chrony.conf /etc/chrony/chrony.conf
EXPOSE 123/udp
# -x: never adjust the clock, so no clock privilege is needed. -d: stay in the foreground.
ENTRYPOINT ["chronyd", "-d", "-x", "-f", "/etc/chrony/chrony.conf"]
