#!/usr/bin/env bash
# Generates: private CA, broker cert (TLS 8883), API cert (HTTPS 8443), mosquitto passwd file, secrets, and prepares the
# pairing-PKI dir. Every private key / secret is mode 0600 owned by the uid of the ONE container that reads it;
# directories are 0700/0750; the CA private key lives OUTSIDE every mounted directory (deploy/ca-private, 0700).
#
# Usage: deploy/scripts/gen-certs.sh [host-ip-or-dns ...]   (SANs; default: 127.0.0.1 localhost)
# Ownership needs root (run with sudo, or it falls back to `sudo -n chown`; if neither works a warning is printed and
# the files stay owned by you -- fix before `docker compose up`).
set -euo pipefail
cd "$(dirname "$0")/.."
# container uids (see docker/Dockerfile.*; shared group 10000 is for the IPC sockets only)
UID_IQ=10001; UID_C4=10002; UID_OUT=10003; UID_MQ=1883
OUT=certs; SEC=secrets; MQ=mosquitto; PRIV=ca-private
umask 077
mkdir -p "$OUT/broker" "$OUT/api" "$SEC" data/pki "$PRIV"
SANS=("${@:-127.0.0.1}" localhost)
san_line() { local s="" i=1; for h in "${SANS[@]}"; do
  if [[ "$h" =~ ^[0-9.]+$ ]]; then s+="IP.$i=$h,"; else s+="DNS.$i=$h,"; fi; i=$((i+1)); done; echo "${s%,}"; }
ext() { printf "subjectAltName=%s\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n" "$(san_line | sed 's/IP\.[0-9]*=/IP:/g; s/DNS\.[0-9]*=/DNS:/g')"; }
if [ ! -f "$PRIV/ca.key" ]; then
  openssl req -x509 -newkey rsa:3072 -nodes -keyout "$PRIV/ca.key" -out "$OUT/ca.crt" -days 3650 -subj "/CN=BlackRelay Local CA" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign"
fi
for n in broker api; do
  openssl req -newkey rsa:2048 -nodes -keyout "$OUT/$n/$n.key" -out "$OUT/$n/$n.csr" -subj "/CN=blackrelay-$n"
  openssl x509 -req -in "$OUT/$n/$n.csr" -CA "$OUT/ca.crt" -CAkey "$PRIV/ca.key" -CAcreateserial \
    -CAserial "$PRIV/ca.srl" -out "$OUT/$n/$n.crt" -days 825 -extfile <(ext)
  rm -f "$OUT/$n/$n.csr"
done
gen_pw() { head -c 24 /dev/urandom | base64 | tr -d '/+=' ; }
[ -f "$SEC/mqtt_password" ] || { gen_pw > "$SEC/mqtt_password"; }
HA_PW=$(gen_pw); NR_PW=$(gen_pw)
PASSWD_TOOL=$(command -v mosquitto_passwd || true)
rm -f "$MQ/passwd"; touch "$MQ/passwd"
add() { if [ -n "$PASSWD_TOOL" ]; then "$PASSWD_TOOL" -b "$MQ/passwd" "$1" "$2"
        else docker run --rm -v "$PWD/$MQ:/w" eclipse-mosquitto:2.0.18@sha256:d12c8f80dfc65b768bb9acecc7ef182b976f71fb681640b66358e5e0cf94e9e9 mosquitto_passwd -b /w/passwd "$1" "$2"; fi; }
add blackrelay "$(cat "$SEC/mqtt_password")"; add homeassistant "$HA_PW"; add nodered "$NR_PW"
[ -f "$SEC/control4_token" ] || { echo "REPLACE_WITH_CONTROL4_TOKEN" > "$SEC/control4_token"; }

# ---- modes: secrets and keys 0600, public material 0644, directories 0700/0750
chmod 0600 "$OUT/broker/broker.key" "$OUT/api/api.key" "$PRIV/ca.key" "$SEC/mqtt_password" "$SEC/control4_token" "$MQ/passwd"
chmod 0644 "$OUT/ca.crt" "$OUT/broker/broker.crt" "$OUT/api/api.crt"
chmod 0700 "$PRIV" data/pki
chmod 0750 "$OUT" "$OUT/broker" "$OUT/api" "$SEC" "$MQ"
chmod 0755 "$OUT" "$OUT/broker" "$OUT/api"       # certs are public; keys inside are 0600 + owned by their reader
own() { local u=$1; shift; chown "$u" "$@" 2>/dev/null || sudo -n chown "$u" "$@" 2>/dev/null || \
        echo "WARNING: could not chown $* to uid $u (run as root): the container will not be able to read it" >&2; }
own $UID_MQ "$OUT/broker/broker.key" "$MQ/passwd"
own $UID_OUT "$OUT/api/api.key" "$SEC/mqtt_password"
own $UID_C4 "$SEC/control4_token"
own $UID_IQ data/pki
echo "homeassistant MQTT password: $HA_PW"; echo "nodered MQTT password: $NR_PW"
echo "Pairing PKI goes in deploy/data/pki (owner uid $UID_IQ, mode 0700). ca.key is in deploy/$PRIV (never mounted)."
echo "Control4 panel fingerprint: openssl s_client -connect <panel>:12345 </dev/null 2>/dev/null | openssl x509 -outform DER | sha256sum"
echo "Next: docker compose --profile '*' build && docker compose run --rm bridge hash-key   # paste key_id and argon2_hash into config/config.yaml"
