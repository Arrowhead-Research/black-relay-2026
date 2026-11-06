# Multi-arch: alpine:3.23.8 has linux/amd64 and linux/arm64, and nftables is in its main repository
# for both. The upstream version is pinned. Alpine replaces the -rN revision in place, so pinning
# that too would break a rebuild.
FROM alpine:3.23.8
RUN apk add --no-cache 'nftables~1.1.5'
COPY guard.sh verify.sh /netguard/
RUN chmod 0555 /netguard/guard.sh /netguard/verify.sh
ENTRYPOINT ["/netguard/guard.sh"]
