# Cake Status

Independent public status page for Cake Project. The service polls Watcher's sanitized public status API, keeps the last valid snapshot on disk, and remains available when the Watcher host is down.

## Local development

Watcher must be listening on `127.0.0.1:18080`.

```sh
docker compose up -d --build
```

Open `http://127.0.0.1:18082/`. Health is available at `/health`, and the public JSON contract at `/api/v1/public/status`.

## Production

Copy `.env.example` to `.env`, set the public HTTPS Watcher URL and Cake Project home URL, then start Docker Compose. The container binds only to loopback; terminate HTTPS in a separately managed reverse proxy.

An nginx example is available at `deploy/nginx.conf.example`. Replace `status.example.com` with the dedicated status hostname and provision its TLS certificate before enabling the file.

When upstream is reachable, the response is sanitized and cached. When it is unavailable:

- Watcher, new connection creation, and subscription delivery become unavailable;
- selected VLESS targets become unknown because they can no longer be refreshed;
- the overall status becomes a major outage;
- the last successful snapshot remains on disk for labels and recovery.

The service never receives Dashboard credentials, VLESS URIs, UUIDs, subscription tokens, or private status details.
