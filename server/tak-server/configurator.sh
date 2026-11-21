#!/bin/bash

function main(){

  setup

  if [ "$ENROLLMENT_TYPE" = "manual_enrollment" ]; then
    create_user_and_admin_certificates
  fi

  wait_for_plugin_service_to_start

  chmod +x /opt/tak/utils/UserManager.jar
  grant_gateway_admin
  if [ "$ENROLLMENT_TYPE" = "manual_enrollment" ]; then

    for i in $(seq 1 "$ADMINS_COUNT"); do
      create_tak_server_admin_user $i
      if [ "$SERVER_NAME" != "" ]; then
        create_manual_enrollment_mission_package "Admin" $i
      else
        copy_manual_enrollment_certificates_to_shared_directory "Admin" $i
      fi
    done

    for i in $(seq 1 "$USER_COUNT"); do
      create_tak_server_client_user $i
      if [ "$SERVER_NAME" != "" ]; then
        create_manual_enrollment_mission_package "User" $i
      else
        copy_manual_enrollment_certificates_to_shared_directory "User" $i
      fi
    done

    if [ "$SERVER_NAME" != "" ]; then
      copy_manual_enrollment_mission_packages_to_shared_directory
    fi

  else
    create_automated_enrollment_admin_account
    create_automated_enrollment_client_account
    setup_automated_enrollment_mission_package_file_structure
    if [ "$SERVER_NAME" != "" ]; then
      create_automated_enrollment_mission_package
      zip_mission_package "mission_package"
      copy_automated_enrollment_mission_package_to_shared_directory
    else
      cp /opt/tak/certs/files/truststore-intermediate.p12 /opt/tak/certs/shared/
    fi
  fi

  publish_public_certs_to_shared_directory

  touch "$DONE_MARKER"
}

# Copies what a person or client needs from the PKI to ./shared, on every OS. This is an allowlist on purpose:
# files/ also holds the CA and server private keys (root-ca-do-not-share.key, ca-do-not-share.key,
# intermediate.key, takserver.key), which must never leave the tak-pki volume.
function publish_public_certs_to_shared_directory(){
  local f
  for f in root-ca.pem intermediate.pem pytak-gateway.pem pytak-gateway.key pytak-gateway.p12; do
    cp "/opt/tak/certs/files/$f" /opt/tak/certs/shared/
  done
  # pytak-gateway.key is the gateway's client credential (password-protected): keep it owner-only
  chmod 600 /opt/tak/certs/shared/pytak-gateway.key /opt/tak/certs/shared/pytak-gateway.p12
}

# The PKI (CA, intermediate, takserver cert) is no longer made here: the "pki" service builds it once into
# the tak-pki volume (tak-server/pki-init.sh) and this container only mounts it.
# The marker lives on the container filesystem, so a restart skips the setup but a recreated container
# (new DB accounts, new mission package) runs it again. Everything below is safe to run twice.
DONE_MARKER=/tmp/.configurator-done

function setup(){
  if [ -f "$DONE_MARKER" ]; then
      exit 0
  fi
  cd /opt/tak/certs || echo "/opt/tak/certs folder doesn't exist"
  if [ ! -s /opt/tak/certs/files/intermediate.pem ]; then
      echo "No PKI in /opt/tak/certs/files: the pki service has not built it (is the tak-pki volume mounted?)" >&2
      exit 1
  fi
}

function setup_manual_enrollment_mission_package_file_structure(){
    mkdir -p /opt/tak/certs/files/Takserver-"$1"-"$2"
    mkdir -p /opt/tak/certs/files/Takserver-"$1"-"$2"/certs
    if [ "$SERVER_NAME" != "" ]; then
      mkdir -p /opt/tak/certs/files/Takserver-"$1"-"$2"/manifest
    fi
}

function create_user_and_admin_certificates(){
  for i in $(seq 1 "$ADMINS_COUNT"); do
    setup_manual_enrollment_mission_package_file_structure "Admin" $i
    ./makeCert.sh client Takserver-Admin-"$i"
  done

  for i in $(seq 1 "$USER_COUNT"); do
    setup_manual_enrollment_mission_package_file_structure "User" $i
    ./makeCert.sh client Takserver-User-"$i"
  done
}

function wait_for_plugin_service_to_start(){
  JPSOUT=$(jps | grep takserver-pm | cut -d " " -f 1)
  until jmap -histo:live "$JPSOUT" | grep "tak.server.plugins.messaging.PluginMessenger"; do
    sleep 1
  done
}

function create_tak_server_admin_user(){
    java -jar /opt/tak/utils/UserManager.jar certmod -A /opt/tak/certs/files/Takserver-Admin-"$1".pem | tee -a scrape_file.txt
}

# The gateway removes a site's mission on TAK Server (DELETE /Marti/api/missions/<name>), which TAK allows only
# for an admin or the mission's owner. Without this the gateway can create a mission but not delete it (HTTP 403).
function grant_gateway_admin(){
    java -jar /opt/tak/utils/UserManager.jar certmod -A /opt/tak/certs/files/pytak-gateway.pem | tee -a scrape_file.txt
}

function create_tak_server_client_user(){
    java -jar /opt/tak/utils/UserManager.jar usermod -c /opt/tak/certs/files/Takserver-User-"$1".pem Takserver-User-"$1" | tee -a scrape_file.txt
}

function create_pref_and_manifest_files_by_role_and_count(){
  sed -e"s/{cert_path}/Takserver-$1-$2/" -e"/enrollForCertificateWithTrust/d" -e"/cacheCreds/d" -e"/useAuth/d" tak-server-configurator-template.pref > /opt/tak/certs/files/Takserver-"$1"-"$2"/certs/tak-server-configurator.pref
  sed "s/{cert_path}/Takserver-$1-$2/" tak-server-configurator-manifest.xml > /opt/tak/certs/files/Takserver-"$1"-"$2"/manifest/tak-server-configurator-manifest.xml
}

function zip_mission_package(){
  cd /opt/tak/certs/files/"$1"
  jar -cvf mission_package.zip manifest certs
  cd ../..
}

function create_manual_enrollment_mission_package(){
  mv /opt/tak/certs/files/Takserver-"$1"-"$2".p12 /opt/tak/certs/files/Takserver-"$1"-"$2"/certs
  cp /opt/tak/certs/files/truststore-root.p12 /opt/tak/certs/files/Takserver-"$1"-"$2"/certs
  create_pref_and_manifest_files_by_role_and_count "$1" "$2"
  zip_mission_package Takserver-"$1"-"$2"
}

function copy_manual_enrollment_mission_packages_to_shared_directory(){
  cp /opt/tak/certs/files/truststore-root.p12 shared/ 2>/dev/null
  cp -r /opt/tak/certs/files/Takserver-*/ shared/ 2>/dev/null
  find . -type f -regex '/opt/tak/certs/files/Takserver-Admin-[0-9].pem' -exec cp {} shared/ 2>/dev/null
}

function copy_manual_enrollment_certificates_to_shared_directory(){
  cp /opt/tak/certs/files/Takserver-"$1"-"$2".p12 /opt/tak/certs/files/Takserver-"$1"-"$2"/certs
  cp -r /opt/tak/certs/files/Takserver-"$1"-"$2"/ /opt/tak/certs/shared/
}

function create_automated_enrollment_admin_account(){
    java -jar /opt/tak/utils/UserManager.jar usermod -A -p "$AUTOMATED_ADMIN_PASSWORD" "$AUTOMATED_ADMIN_USERNAME"
}

function create_automated_enrollment_client_account(){
    java -jar /opt/tak/utils/UserManager.jar usermod -p "$AUTOMATED_CLIENT_PASSWORD" "$AUTOMATED_CLIENT_USERNAME"
}

function setup_automated_enrollment_mission_package_file_structure(){
    mkdir -p /opt/tak/certs/files/mission_package/certs
    mkdir -p /opt/tak/certs/files/mission_package/manifest
}

function create_automated_enrollment_mission_package(){
  cp /opt/tak/certs/files/truststore-intermediate.p12 /opt/tak/certs/files/mission_package/certs
  create_automated_enrollment_pref_and_manifest_files

  cd /opt/tak/certs/files/mission_package
  jar -cvf mission_package.zip manifest certs
  cd ../..
}

function create_automated_enrollment_pref_and_manifest_files(){
  sed -e"/{cert_path}/d" -e"/clientPassword/d" -e"s/root/intermediate/" tak-server-configurator-template.pref > /opt/tak/certs/files/mission_package/certs/tak-server-configurator.pref
  sed -e"/{cert_path}/d" -e"s/root/intermediate/" tak-server-configurator-manifest.xml > /opt/tak/certs/files/mission_package/manifest/tak-server-configurator-manifest.xml
}

function copy_automated_enrollment_mission_package_to_shared_directory(){
    cp /opt/tak/certs/files/truststore-intermediate.p12 /opt/tak/certs/shared/
    cp -r /opt/tak/certs/files/mission_package/ /opt/tak/certs/shared/
}

main