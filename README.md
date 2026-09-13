# Cake Status

Independent public status service for Cake Project. The public host is `statuscake.shmoza.net`; TLS and the server reverse proxy are intentionally managed outside this repository.

The service combines two independent sources:

- sanitized Cake service state from Watcher's public API;
- end-to-end VLESS/REALITY canary checks executed locally through a pinned Xray binary.

Watcher and status may run on different servers and use unrelated IP addresses. They communicate over HTTPS: status polls `STATUS_UPSTREAM_URL`, and Watcher sends the selected public target list to `PUT /api/v1/admin/targets`. The admin endpoint requires a shared Bearer token; the server reverse proxy must additionally restrict its source IPs.

## Status semantics

API schema v2 exposes separate `summaries.services` and `summaries.connections` values. The legacy top-level `overallStatus` mirrors only the Cake services summary during migration.

- Loss of Watcher makes its checks unavailable; it does not change an independently measured VLESS result.
- A subscription check that cannot run is `unknown` with `checkStatus: unavailable`, not a confirmed outage.
- A selected connection without a runtime canary is `unknown` with `checkStatus: not_configured`.
- TCP reachability alone is never reported as a working VLESS connection.

`/livez` reports process liveness. `/readyz` reports that the status process initialized and can serve state; it does not depend on Watcher being reachable. `/health` remains a liveness compatibility alias.

## Local development

Watcher normally listens on `127.0.0.1:18080`.

```sh
docker compose up -d --build
```

Open `http://127.0.0.1:18082/`. The Docker/system resolver used by the status container resolves target hostnames. The local empty canary example deliberately produces “Проверка не настроена”.

To test a real selected connection, copy `secrets/status-vless-canaries.example.json` to the gitignored `secrets/status-vless-canaries.json`, set `STATUS_VLESS_CANARY_FILE_PATH`, and add a dedicated canary URI:

```json
{
  "schemaVersion": 1,
  "probes": {
    "vpn.example.net:443": {
      "uri": "vless://DEDICATED-CANARY-UUID@vpn.example.net:443?...",
      "testUrl": "https://www.gstatic.com/generate_204",
      "expectedStatus": 204
    }
  }
}
```

Use a dedicated, revocable canary identity rather than a customer connection. The URI is mounted read-only at runtime and is excluded from images, APIs, logs, caches, and logical backups.

## Production install and operations

The current development/product version is `v0.0.1` (`VERSION` contains the SemVer value `0.0.1`). Only a `status-v0.0.1`-style Git tag starts a status-service release. Releases build once, test and scan the exact image digest, then publish a checksummed install bundle and manifest.

On the status server, extract the release bundle and run:

```sh
sudo bash ./deploy/install.sh prepare
sudoedit /etc/cake-status/status.env
sudoedit /etc/cake-status/status-vless-canaries.json
sudo cake-status install
```

The generated environment remains mode `0600`. The canary file is `root:cake-status-secret` and mode `0640`; only that numeric group is added to the non-root container process. `STATUS_IMAGE_REF` must be an immutable `image@sha256:...` reference. `STATUS_RELEASE_BASE_URL` points at the repository's release-download root.

Operational commands:

```sh
sudo cake-status status
sudo cake-status logs 200
sudo cake-status backup /var/backups/cake-status.zip
sudo cake-status restore /var/backups/cake-status.zip
sudo cake-status update-check 0.0.2
sudo cake-status update 0.0.2
sudo cake-status update-status JOB_ID
sudo cake-status repair
```

Update requests accept an exact semantic version only. The root-owned local controller validates the release manifest and immutable image digest, creates a logical backup, deploys, waits for readiness and the v2 API contract, and restores the previous environment/image if the gate fails. Job state persists under `/var/lib/cake-status/updater/jobs`.

See `deploy/REVERSE_PROXY.md` for the contract expected from the separately managed server proxy.
