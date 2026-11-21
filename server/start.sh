#!/bin/bash

readonly MANUAL_ENROLLMENT=manual_enrollment
readonly AUTOMATED_ENROLLMENT=automated_enrollment

ENROLLMENT_TYPE=${AUTOMATED_ENROLLMENT}

function main() {
  set_env_for_takserver_enrollment_type

  compose_tak_and_check_container_status
  wait_for_plugin_service
  wait_for_shared_volumes

  echo "Running the Army Software Factory Configurator"
  docker exec takserver /opt/configurator/configurator.sh

  compose_gateway_and_check_container_status

  chown -R "$_USER:$_GROUP" ./plugins

  if [[ "$ENROLLMENT_TYPE" == "${MANUAL_ENROLLMENT}" ]]; then
    wait_for_cert_validation
    import_cert_to_keychain
  fi

  # the configurator wrote the public certs into ./shared as the container's user: hand them to the caller
  chown -R "$_USER:$_GROUP" ./shared

  open_browser_to_takserver
}

function set_env_for_takserver_enrollment_type() {
  if grep -q "ENV ENROLLMENT_TYPE=manual_enrollment" "Dockerfile-takserver"; then
    ENROLLMENT_TYPE=${MANUAL_ENROLLMENT}
    set_takserver_address 8443
  else
    set_takserver_address 8446
  fi

  RESOURCE=$(ls takserver-docker-*.zip)
  RESOURCE_FOLDER=./${RESOURCE%.zip}
  USER_AUTH_FILE=/opt/tak/UserAuthenticationFile.xml
  PK_FILE_PATH=./shared/Takserver-Admin-1/certs/Takserver-Admin-1.p12
  _USER=$(id -un)
  _GROUP=$(id -gn)
}

function set_takserver_address() {
  set_machine_ip
  PORT=$1
  TAKSERVER_ADDRESS="https://$IP:$PORT"
}

function set_machine_ip() {
  # The ports are published on BIND_ADDR only (.env), so that is where the server answers.
  IP=${BIND_ADDR:-$(sed -n 's/^BIND_ADDR=//p' "$(dirname "$0")/.env" 2>/dev/null | tail -n1 | tr -d "\"'")}
  if [[ "$IP" == "0.0.0.0" ]]; then
    echo "BIND_ADDR must be one address (the VPN's), not 0.0.0.0: every published port binds to it." && exit 1
  fi
  if [[ -n "$IP" ]]; then
    return
  fi
  if [[ "$OSTYPE" == "darwin"* ]]; then
    IP=$(ifconfig | grep "inet " | grep -v "127.0.0.1" | awk '{print $2}' | head -n1)
  else
    IP=$(ip "address" | grep "inet " | grep -v "127.0.0.1" | awk '{print $2}' | head -n1 | cut -d "/" -f 1)
  fi
}


function compose_tak_and_check_container_status() {

  if [[ "$OSTYPE" == "darwin"* ]]; then
    docker pull eclipse-temurin:17-jammy
    docker pull postgres:15.1
  fi

  if which docker-compose >/dev/null; then
    docker-compose up -d --build takserver-core takserver-db
  else
    docker compose up -d --build takserver-core takserver-db
  fi

  TAKSERVER_CONTAINER_NOT_RUNNING=$(docker ps | grep -E "\btakserver(\s|$)")
  TAKSERVERDB_CONTAINER_NOT_RUNNING=$(docker ps | grep -w "tak-database")

  if [ -z "$TAKSERVER_CONTAINER_NOT_RUNNING" ] || [ -z "$TAKSERVERDB_CONTAINER_NOT_RUNNING" ]; then
    echo "TAK Server containers failed to start.  Shutting down..." && exit 1
  else
    echo "takserver and tak-database containers were successfully created."
  fi
}

function compose_gateway_and_check_container_status() {
  # The http monitor polls Fusion, so it starts only when Fusion is configured (.env or the environment).
  FUSION=${FUSION_URL:-$(sed -n 's/^FUSION_URL=//p' "$(dirname "$0")/.env" 2>/dev/null | tail -n1 | tr -d "\"'")}
  SERVICES="gateway-http"
  [ -n "$FUSION" ] && SERVICES="gateway-http gateway-http-monitor"

  if which docker-compose >/dev/null; then
    docker-compose up -d --build $SERVICES
  else
    docker compose up -d --build $SERVICES
  fi

  if [ -z "$(docker ps -q --filter name=gateway-http --filter status=running)" ]; then
    echo "Gateway containers failed to start.  Shutting down..." && exit 1
  else
    echo "gateway containers were successfully created."
  fi
}

function wait_for_plugin_service() {
  JPSOUT=$(docker exec takserver jps | grep takserver-pm | cut -d " " -f 1)

  echo -e "\nWaiting for plugin service to start..."
  until docker exec takserver jmap -histo:live "$JPSOUT" | grep "tak.server.plugins.service.PluginService" &>/dev/null; do
    sleep 3
  done
  echo -e "Plugin Service Started\n"
}

function wait_for_shared_volumes() {
  echo -e "Waiting for shared volume..."
  until [ -d ./shared ]; do
    sleep 3
  done
  echo -e "Shared Volume Created\n"
}

function wait_for_cert_validation() {
  echo -e "Waiting for Admin Cert Validation\n"
  until docker exec takserver cat "$USER_AUTH_FILE" 2>/dev/null | grep -q Takserver-Admin-1 &>/dev/null; do
    sleep 3
  done
}

function wait_for_cert_generation() {
  echo -e "Waiting for cert generation..."
  until test -f "$PK_FILE_PATH"; do
    sleep 2.5
  done
  echo -e "Admin Cert Found!!\n"
}

function import_cert_to_keychain() {
  if [[ "$OSTYPE" == "darwin"* ]]; then
    wait_for_cert_generation
    security import "$PK_FILE_PATH" -k ~/Library/Keychains/login.keychain-db -P atakatak
  else
    wait_for_cert_generation
    if ! pk12util -d sql:"$HOME"/.pki/nssdb -i "$PK_FILE_PATH" -W atakatak; then
      echo -e "\nCertificate import failed... Either you do not have libnss3-tools installed or something went wrong.\n
      Please import your certificate manually from the ./shared folder

      - Go to Settings > Privacy and Security > Security > Manage Certificates\n"
    fi
  fi
}

function open_browser_to_takserver() {

  docker restart takserver
  echo -e "Waiting for server to receive connections..."

  if command -v wget &>/dev/null; then
    TAKSERVER_RESPONSE=$(wget --spider -S --no-check-certificate "https://$IP:8446" 2>&1 | grep "HTTP/" | awk '{print $2}')
    until [[ "$TAKSERVER_RESPONSE" == "403" ]]; do
      TAKSERVER_RESPONSE=$(wget --spider -S --no-check-certificate "https://$IP:8446" 2>&1 | grep "HTTP/" | awk '{print $2}')
      sleep 1
    done
  else
    TAKSERVER_RESPONSE=$(curl -kw '%{http_code}' -so /dev/null "https://$IP:8446")
    until [[ "$TAKSERVER_RESPONSE" == "403" ]]; do
      TAKSERVER_RESPONSE=$(curl -kw '%{http_code}' -so /dev/null "https://$IP:8446")
      sleep 1
    done
  fi

  echo -e "Opening browser to $TAKSERVER_ADDRESS ..."

  if [[ "$OSTYPE" == "darwin"* ]]; then
    open -a "Google Chrome" "$TAKSERVER_ADDRESS"
  else
    firefox --new-window "$TAKSERVER_ADDRESS" &>/dev/null
  fi
}

main