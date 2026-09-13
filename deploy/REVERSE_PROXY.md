# Reverse-proxy contract

This repository does not install or configure nginx, Caddy, DNS, or certificates. The server administrator supplies those layers for `statuscake.shmoza.net`.

The proxy must:

- terminate valid HTTPS for `statuscake.shmoza.net` and forward to `127.0.0.1:18082`;
- preserve the exact `Host: statuscake.shmoza.net` header (the application rejects other hosts);
- expose `/`, static assets, `/robots.txt`, and `/api/v1/public/status` publicly;
- keep `/livez`, `/readyz`, and `/health` private to local monitoring unless an external health monitor explicitly needs them;
- allow `PUT /api/v1/admin/targets` only from the fixed Watcher server IP allowlist and preserve its `Authorization: Bearer …` header;
- reject other methods on the admin route, apply a small request-body limit, and rate-limit failed authorization attempts;
- add or preserve `X-Robots-Tag: noindex, nofollow, noarchive`;
- set upstream timeouts below the proxy's global defaults and never cache the status JSON or admin response;
- rotate access/error logs and ensure the Authorization header is not included in the log format.

Watcher and status can be on different servers. Configure Watcher with:

```text
LOKI_WATCHER_STATUS_SERVICE_URL=https://statuscake.shmoza.net
LOKI_WATCHER_STATUS_SERVICE_TOKEN=<same value as STATUS_ADMIN_TOKEN>
```

Configure status with a Watcher URL reachable from the status server, for example:

```text
STATUS_UPSTREAM_URL=https://cake.shmoza.net/api/v1/public/status
```

DNS may therefore point `cake.shmoza.net` and `statuscake.shmoza.net` to different IP addresses. There is no shared Docker network or same-host dependency.
