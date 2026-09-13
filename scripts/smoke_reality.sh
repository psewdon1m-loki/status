#!/usr/bin/env bash
set -Eeuo pipefail

: "${STATUS_IMAGE:?set STATUS_IMAGE to the exact image under test}"
RUN_SUFFIX="${GITHUB_RUN_ID:-$$}-${GITHUB_RUN_ATTEMPT:-0}"
NETWORK="cake-status-reality-${RUN_SUFFIX}"
SERVER="cake-status-reality-server-${RUN_SUFFIX}"
CLIENT="cake-status-reality-client-${RUN_SUFFIX}"
XRAY_IMAGE="ghcr.io/xtls/xray-core:26.9.9@sha256:45338c4df61fda061c47ce62aafda6c5d7d59cbdefc33f2e335d8b0c748b748a"

cleanup() {
  docker rm -f "${CLIENT}" "${SERVER}" >/dev/null 2>&1 || true
  docker network rm "${NETWORK}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create "${NETWORK}" >/dev/null
docker run -d --name "${SERVER}" --network "${NETWORK}" --network-alias host.docker.internal \
  -v "$(pwd)/tests/xray-server.json:/etc/xray/config.json:ro" \
  "${XRAY_IMAGE}" run -c /etc/xray/config.json >/dev/null
docker run -d --name "${CLIENT}" --network "${NETWORK}" -p 127.0.0.1:18083:8080 \
  -v "$(pwd)/tests/vless-canary.integration.json:/run/secrets/status-vless-canaries.json:ro" \
  -e STATUS_ALLOW_HTTP_UPSTREAM=1 \
  -e STATUS_UPSTREAM_URL=http://127.0.0.1:9/api/v1/public/status \
  -e STATUS_ALLOWED_HOSTS=statuscake.shmoza.net,127.0.0.1 \
  -e STATUS_ADMIN_TOKEN=integration-status-token-abcdefghijklmnopqrstuvwxyz \
  -e STATUS_VLESS_CANARY_FILE=/run/secrets/status-vless-canaries.json \
  "${STATUS_IMAGE}" >/dev/null

for attempt in $(seq 1 30); do
  if curl --fail --silent -H 'Host: statuscake.shmoza.net' http://127.0.0.1:18083/readyz >/dev/null; then
    break
  fi
  sleep 1
done

curl --fail --silent --request PUT \
  -H 'Host: statuscake.shmoza.net' \
  -H 'Authorization: Bearer integration-status-token-abcdefghijklmnopqrstuvwxyz' \
  -H 'Content-Type: application/json' \
  --data-binary '{"targets":[{"configurationKey":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","label":"REALITY Canary","host":"host.docker.internal","port":19090}]}' \
  http://127.0.0.1:18083/api/v1/admin/targets >/dev/null

curl --fail --silent -H 'Host: statuscake.shmoza.net' http://127.0.0.1:18083/api/v1/public/status |
  python -c 'import json,sys; value=json.load(sys.stdin); assert value["upstreamAvailable"] is False; assert value["summaries"]["connections"]["status"] == "operational"; detail=value["components"][1]["details"][0]; assert detail["status"] == "operational" and detail["checkStatus"] == "completed"'
