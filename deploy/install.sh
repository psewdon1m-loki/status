#!/usr/bin/env bash
set -Eeuo pipefail

COMMAND="${1:-}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="/opt/cake-status"
CONFIG_DIR="/etc/cake-status"
STATE_DIR="/var/lib/cake-status"
ENV_FILE="${CONFIG_DIR}/status.env"
ADMIN_GROUP="cake-status-admin"
SECRET_GROUP="cake-status-secret"

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "run as root" >&2
    exit 1
  fi
}

require_commands() {
  local missing=0
  for command in docker python3 openssl curl; do
    if ! command -v "${command}" >/dev/null 2>&1; then
      echo "missing required command: ${command}" >&2
      missing=1
    fi
  done
  docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is required" >&2; missing=1; }
  [[ "${missing}" -eq 0 ]]
}

install_files() {
  install -d -m 0755 "${INSTALL_DIR}/deploy" "${INSTALL_DIR}/secrets"
  install -m 0644 "${SOURCE_DIR}/deploy/docker-compose.release.yml" "${INSTALL_DIR}/deploy/docker-compose.release.yml"
  install -m 0755 "${SOURCE_DIR}/deploy/statusctl" "${INSTALL_DIR}/deploy/statusctl"
  for file in install.sh validate_env.py updater_common.py updater_daemon.py updater_client.py update_worker.py; do
    install -m 0755 "${SOURCE_DIR}/deploy/${file}" "${INSTALL_DIR}/deploy/${file}"
  done
  install -m 0644 "${SOURCE_DIR}/deploy/cake-status-updater.service" /etc/systemd/system/cake-status-updater.service
  ln -sfn "${INSTALL_DIR}/deploy/statusctl" /usr/local/sbin/cake-status
}

prepare() {
  require_root
  require_commands
  getent group "${ADMIN_GROUP}" >/dev/null || groupadd --system "${ADMIN_GROUP}"
  getent group "${SECRET_GROUP}" >/dev/null || groupadd --system "${SECRET_GROUP}"
  install -d -m 0755 "${CONFIG_DIR}" "${STATE_DIR}" "${STATE_DIR}/updater" "${STATE_DIR}/updater/jobs" "${STATE_DIR}/backups" /run/cake-status
  if [[ ! -f "${CONFIG_DIR}/status-vless-canaries.json" ]]; then
    install -m 0640 -g "${SECRET_GROUP}" "${SOURCE_DIR}/secrets/status-vless-canaries.example.json" "${CONFIG_DIR}/status-vless-canaries.json"
  fi
  if [[ ! -f "${ENV_FILE}" ]]; then
    local admin_token
    admin_token="$(openssl rand -hex 32)"
    install -m 0600 /dev/null "${ENV_FILE}"
    {
      echo "STATUS_VERSION=0.0.1"
      echo "STATUS_IMAGE_REF=ghcr.io/OWNER/cake-status@sha256:REPLACE_WITH_RELEASE_DIGEST"
      echo "STATUS_RELEASE_BASE_URL=https://github.com/OWNER/REPOSITORY/releases/download"
      echo "STATUS_UPSTREAM_URL=https://cake.shmoza.net/api/v1/public/status"
      echo "STATUS_HOME_URL=https://cake.shmoza.net/initialize/"
      echo "STATUS_ADMIN_TOKEN=${admin_token}"
      echo "STATUS_ALLOWED_HOSTS=statuscake.shmoza.net,localhost,127.0.0.1,::1"
      echo "STATUS_VLESS_CANARY_FILE_PATH=${CONFIG_DIR}/status-vless-canaries.json"
      echo "STATUS_SECRET_GID=$(getent group "${SECRET_GROUP}" | cut -d: -f3)"
      echo "STATUS_VLESS_PROBE_TIMEOUT_SECONDS=12"
      echo "STATUS_POLL_INTERVAL_SECONDS=30"
      echo "STATUS_UPSTREAM_TIMEOUT_SECONDS=8"
      echo "STATUS_PORT=18082"
    } > "${ENV_FILE}"
    chmod 0600 "${ENV_FILE}"
    echo "created ${ENV_FILE}; set the release repository and immutable image digest before install"
  fi
  install_files
  systemctl daemon-reload
  systemctl enable --now cake-status-updater.service
  echo "prepare complete"
}

deploy() {
  require_root
  require_commands
  python3 "${INSTALL_DIR}/deploy/validate_env.py" "${ENV_FILE}"
  docker compose --env-file "${ENV_FILE}" -f "${INSTALL_DIR}/deploy/docker-compose.release.yml" pull
  docker compose --env-file "${ENV_FILE}" -f "${INSTALL_DIR}/deploy/docker-compose.release.yml" up -d --no-build --remove-orphans
  local port
  port="$(awk -F= '$1 == "STATUS_PORT" { print $2 }' "${ENV_FILE}" | tail -n1)"
  port="${port:-18082}"
  for _ in $(seq 1 45); do
    if curl --fail --silent -H 'Host: statuscake.shmoza.net' "http://127.0.0.1:${port}/readyz" >/dev/null; then
      echo "Cake Status is ready on 127.0.0.1:${port}"
      return
    fi
    sleep 2
  done
  echo "Cake Status did not become ready" >&2
  exit 1
}

case "${COMMAND}" in
  prepare)
    prepare
    ;;
  install)
    require_root
    install_files
    systemctl daemon-reload
    systemctl enable --now cake-status-updater.service
    deploy
    ;;
  repair)
    require_root
    install_files
    chmod 0600 "${ENV_FILE}"
    chown root:"${SECRET_GROUP}" "${CONFIG_DIR}/status-vless-canaries.json"
    chmod 0640 "${CONFIG_DIR}/status-vless-canaries.json"
    systemctl daemon-reload
    systemctl restart cake-status-updater.service
    deploy
    ;;
  status)
    require_root
    docker compose --env-file "${ENV_FILE}" -f "${INSTALL_DIR}/deploy/docker-compose.release.yml" ps
    systemctl --no-pager --full status cake-status-updater.service
    ;;
  *)
    echo "usage: $0 {prepare|install|repair|status}" >&2
    exit 2
    ;;
esac
