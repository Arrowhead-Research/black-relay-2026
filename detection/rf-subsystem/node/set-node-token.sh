#!/usr/bin/env bash
# Turn on node-agent authentication: generate one random token, put it in each
# node's /etc/rf-node/rf-node.env (NODE_TOKEN, mode 600) and restart its
# rf-node-agent, then put it in the central host's central/.env
# (CONSOLE_NODE_TOKEN) and restart the console. Nodes first, so no heartbeat
# is rejected in between. The token travels on stdin over SSH, never on a
# command line, and is not printed. Run it again to rotate the token.
#
#   node/set-node-token.sh -k ~/.ssh/<node key> -c dev@<central host> <node> [<node> ...]
#
# <node> is anything ssh accepts (pi@192.0.2.10). -d sets the central stack's
# directory (default /srv/sensor-fusion/detection/rf-subsystem/central).
set -euo pipefail

key="" central="" dir="/srv/sensor-fusion/detection/rf-subsystem/central"
while getopts "k:c:d:" opt; do
  case "$opt" in
    k) key="$OPTARG" ;;
    c) central="$OPTARG" ;;
    d) dir="$OPTARG" ;;
    *) exit 2 ;;
  esac
done
shift $((OPTIND - 1))
[ -n "$key" ] && [ -n "$central" ] && [ $# -ge 1 ] || { sed -n '2,12p' "$0"; exit 2; }

token="$(openssl rand -hex 24)"

for node in "$@"; do
  case "$node" in *@*) target="$node" ;; *) target="pi@$node" ;; esac
  printf '%s\n' "$token" | ssh -i "$key" -o ConnectTimeout=15 "$target" '
    read -r t
    sudo sh -c "umask 077; grep -v ^NODE_TOKEN= /etc/rf-node/rf-node.env > /etc/rf-node/.rf-node.env.new; echo NODE_TOKEN=$t >> /etc/rf-node/.rf-node.env.new; mv /etc/rf-node/.rf-node.env.new /etc/rf-node/rf-node.env"
    sudo systemctl restart rf-node-agent
    echo "$(hostname): token set, agent $(systemctl is-active rf-node-agent)"'
done

printf '%s\n' "$token" | ssh -o ConnectTimeout=15 "$central" "
  read -r t
  cd '$dir'
  umask 077
  { grep -v '^CONSOLE_NODE_TOKEN=' .env || true; echo \"CONSOLE_NODE_TOKEN=\$t\"; } > .env.new && mv .env.new .env && chmod 600 .env
  docker compose up -d rf-console >/dev/null 2>&1
  echo 'central: CONSOLE_NODE_TOKEN set, console restarted'"
