#!/bin/sh
# Healthy only if the rules are in the kernel now, and the guard's work finished.
set -eu
rules="$(nft list chain inet netguard output)"
case "$rules" in *"policy drop"*) ;; *) exit 1 ;; esac
case "$rules" in *"ip daddr $RELAY_SITE_SUBNET tcp dport 8554 accept"*) ;; *) exit 1 ;; esac
case "$rules" in *"counter packets"*"drop"*) ;; *) exit 1 ;; esac
if [ -n "${RELAY_CENTRAL_IP:-}" ]; then
    case "$rules" in *"ip daddr $RELAY_CENTRAL_IP tcp dport 8554 drop"*) ;; *) exit 1 ;; esac
fi
