# Cake Status

Independent public status page for Cake Project. The service polls Watcher's sanitized public status API, keeps the last valid snapshot on disk, and independently checks selected VLESS endpoints even when the Watcher host is down.

## Local development

Watcher must be listening on `127.0.0.1:18080`.

```sh
docker compose up -d --build
```

Open `http://127.0.0.1:18082/`. Health is available at `/health`, and the public JSON contract at `/api/v1/public/status`.

Locally, the containers use Docker's `host.docker.internal` name: status reads Watcher on port `18080`, while Watcher synchronizes selected targets to status on port `18082`. VLESS hostnames are resolved by the status container itself using its configured Docker/system DNS resolver.

## Production

Copy `.env.example` to `.env`, set the public HTTPS Watcher URL, Cake Project home URL, and a long random `STATUS_ADMIN_TOKEN`, then start Docker Compose. Configure Watcher with the same token and this service's private or public HTTPS URL. The container binds only to loopback; terminate HTTPS in a separately managed reverse proxy.

An nginx example is available at `deploy/nginx.conf.example`. Replace `status.example.com` with the dedicated status hostname and provision its TLS certificate before enabling the file.

Use separate hostnames for production, ideally with DNS records pointing to different servers, for example `cake.shmoza.net` for Watcher and `status.cake.shmoza.net` for status. They may share one parent domain. Using one exact hostname with different URL paths requires a common reverse proxy and creates a shared failure point, so it is not recommended for an independent status page.

When upstream is reachable, the response is sanitized and cached. When it is unavailable:

- Watcher, new connection creation, and subscription delivery become unavailable;
- selected VLESS targets continue to be resolved and TCP-probed directly by this service;
- the overall status becomes a major outage;
- the last successful snapshot remains on disk for labels and recovery.

Watcher synchronizes only the selected target's opaque configuration key, public label, host, and port through the authenticated `PUT /api/v1/admin/targets` endpoint. The service never receives Dashboard credentials, VLESS URIs, UUIDs, subscription tokens, or private status details.
