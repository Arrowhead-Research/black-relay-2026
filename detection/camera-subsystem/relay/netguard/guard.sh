#!/bin/sh
# Loads the egress rules into this namespace, which MediaMTX shares, then waits. Exits non-zero if
# they cannot be loaded, so the container is never healthy and MediaMTX never starts.
#
# gortsplib follows an RTSP redirect from whatever answers on a node's address, and sends the
# broker's login with it. It has no way to turn that off: gortsplib v5.6.7 client.go:1524-1547,
# and MediaMTX 1.21.3 internal/staticsources/rtsp/source.go:180 sets no such option.
set -eu

ready=/guard/ready
rm -f "$ready"

case "$RELAY_SITE_SUBNET" in
*[!0-9./]* | "")
    echo "netguard: RELAY_SITE_SUBNET is not an IPv4 network" >&2
    exit 1
    ;;
esac
central=""
if [ -n "${RELAY_CENTRAL_IP:-}" ]; then
    case "$RELAY_CENTRAL_IP" in
    *[!0-9.]*)
        echo "netguard: RELAY_CENTRAL_IP is not an IPv4 address" >&2
        exit 1
        ;;
    esac
    central="ip daddr $RELAY_CENTRAL_IP tcp dport 8554 drop"
fi

# Atomic: nft applies the whole file or none of it. No rule for loopback, as MediaMTX's own
# addresses count as loopback here, and that is how a redirect would reach its control port.
# No DNS either, so a redirect to a host name goes nowhere.
if ! nft -f - <<RULES; then
table inet netguard {
    chain output {
        type filter hook output priority filter; policy drop;
        ct state established,related accept
        $central
        ip daddr $RELAY_SITE_SUBNET tcp dport 8554 accept
        udp sport $RELAY_WEBRTC_UDP_PORT accept
        counter drop
    }
}
RULES
    echo "netguard: could not load the rules. This needs CAP_NET_ADMIN and nf_tables in the host's kernel. MediaMTX will not start." >&2
    exit 1
fi

/netguard/verify.sh || {
    echo "netguard: the rules did not verify. MediaMTX will not start." >&2
    exit 1
}
touch "$ready"
chmod 0644 "$ready"
echo "netguard: egress limited to TCP 8554 on $RELAY_SITE_SUBNET"
exec sleep infinity
