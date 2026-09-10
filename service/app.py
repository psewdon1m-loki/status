from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = ROOT / "static"
STATUS_VALUES = {"operational", "degraded", "partial_outage", "major_outage", "unknown"}
COMPONENT_KEYS = ("connection", "specific_connections", "new_connections", "subscriptions")
COMPONENT_TITLES = {
    "connection": "Подключение к Loki",
    "specific_connections": "Работоспособность конкретных подключений",
    "new_connections": "Создание новых подключений",
    "subscriptions": "Получение и обновление подписок",
}
MAX_RESPONSE_BYTES = 256 * 1024
UPSTREAM_URL = os.environ.get(
    "STATUS_UPSTREAM_URL",
    "http://host.docker.internal:18080/api/v1/public/status",
).strip()
ALLOW_HTTP_UPSTREAM = os.environ.get("STATUS_ALLOW_HTTP_UPSTREAM", "0") == "1"
HOME_URL = os.environ.get("STATUS_HOME_URL", "https://cake.shmoza.net/initialize/").strip()
POLL_INTERVAL_SECONDS = min(3600, max(10, int(os.environ.get("STATUS_POLL_INTERVAL_SECONDS", "30"))))
UPSTREAM_TIMEOUT_SECONDS = min(30, max(1, int(os.environ.get("STATUS_UPSTREAM_TIMEOUT_SECONDS", "8"))))
CACHE_PATH = Path(os.environ.get("STATUS_CACHE_PATH", "/data/status-cache.json"))
LISTEN_HOST = os.environ.get("STATUS_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("STATUS_LISTEN_PORT", "8080"))

_state_lock = threading.RLock()
_current_payload: dict[str, Any] = {}
_last_good_payload: dict[str, Any] | None = None
_last_success_at: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bounded_text(value: Any, maximum: int, fallback: str = "") -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    return text[:maximum] or fallback


def normalized_status(value: Any) -> str:
    status = str(value or "unknown")
    return status if status in STATUS_VALUES else "unknown"


def public_status_message(status: str) -> str:
    return {
        "operational": "Работает",
        "degraded": "Работает с ограничениями",
        "partial_outage": "Частичная недоступность",
        "major_outage": "Недоступно",
        "unknown": "Ожидается проверка",
    }.get(status, "Ожидается проверка")


def aggregate_status(components: list[dict[str, Any]]) -> str:
    by_key = {str(item.get("key")): normalized_status(item.get("status")) for item in components}
    if by_key.get("connection") == "major_outage":
        return "major_outage"
    statuses = [by_key.get(key, "unknown") for key in COMPONENT_KEYS]
    if any(status == "major_outage" for status in statuses):
        return "partial_outage"
    if any(status in {"degraded", "partial_outage"} for status in statuses):
        return "degraded"
    if all(status == "operational" for status in statuses):
        return "operational"
    return "unknown"


def sanitize_detail(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "label": bounded_text(value.get("label"), 96, "Подключение"),
        "status": normalized_status(value.get("status")),
        "message": bounded_text(value.get("message"), 160, public_status_message(normalized_status(value.get("status")))),
        "checkedAt": bounded_text(value.get("checkedAt"), 64) or None,
    }


def sanitize_upstream_payload(value: Any, checked_at: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("invalid_upstream_payload")
    source_components = value.get("components")
    if not isinstance(source_components, list):
        raise ValueError("invalid_upstream_components")

    source_by_key = {
        str(item.get("key")): item
        for item in source_components[:50]
        if isinstance(item, dict) and str(item.get("key")) in COMPONENT_KEYS
    }
    components: list[dict[str, Any]] = []
    for key in COMPONENT_KEYS:
        source = source_by_key.get(key, {})
        status = normalized_status(source.get("status"))
        if key == "connection":
            status = "operational"
        details = [
            detail
            for item in (source.get("details") if isinstance(source.get("details"), list) else [])[:50]
            if (detail := sanitize_detail(item)) is not None
        ]
        component = {
            "key": key,
            "title": bounded_text(source.get("title"), 128, COMPONENT_TITLES[key]),
            "status": status,
            "message": bounded_text(source.get("message"), 160, public_status_message(status)),
            "checkedAt": bounded_text(source.get("checkedAt"), 64) or checked_at,
        }
        if key == "specific_connections":
            component["details"] = details
        components.append(component)

    overall = aggregate_status(components)
    return {
        "schemaVersion": 1,
        "overallStatus": overall,
        "message": public_status_message(overall),
        "checkedAt": bounded_text(value.get("checkedAt"), 64) or checked_at,
        "components": components,
        "upstreamAvailable": True,
        "upstreamCheckedAt": checked_at,
        "lastSuccessAt": checked_at,
        "homeUrl": HOME_URL,
    }


def offline_payload(cached: dict[str, Any] | None, checked_at: str, last_success_at: str | None) -> dict[str, Any]:
    cached_components = {
        str(item.get("key")): item
        for item in ((cached or {}).get("components") or [])
        if isinstance(item, dict)
    }
    components: list[dict[str, Any]] = []
    for key in COMPONENT_KEYS:
        previous = cached_components.get(key, {})
        status = "major_outage" if key in {"connection", "new_connections", "subscriptions"} else "unknown"
        component = {
            "key": key,
            "title": bounded_text(previous.get("title"), 128, COMPONENT_TITLES[key]),
            "status": status,
            "message": public_status_message(status),
            "checkedAt": checked_at if key != "specific_connections" else last_success_at,
        }
        if key == "specific_connections":
            component["details"] = [
                {
                    "label": bounded_text(item.get("label"), 96, "Подключение"),
                    "status": "unknown",
                    "message": public_status_message("unknown"),
                    "checkedAt": last_success_at,
                }
                for item in (previous.get("details") if isinstance(previous.get("details"), list) else [])[:50]
                if isinstance(item, dict)
            ]
        components.append(component)
    return {
        "schemaVersion": 1,
        "overallStatus": "major_outage",
        "message": public_status_message("major_outage"),
        "checkedAt": checked_at,
        "components": components,
        "upstreamAvailable": False,
        "upstreamCheckedAt": checked_at,
        "lastSuccessAt": last_success_at,
        "homeUrl": HOME_URL,
    }


def validate_configuration() -> None:
    parsed = urlparse(UPSTREAM_URL)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("STATUS_UPSTREAM_URL must be an absolute HTTP(S) URL without credentials or a fragment")
    if parsed.scheme != "https" and not ALLOW_HTTP_UPSTREAM:
        raise ValueError("STATUS_UPSTREAM_URL must use HTTPS unless STATUS_ALLOW_HTTP_UPSTREAM=1")
    home = urlparse(HOME_URL)
    if home.scheme not in {"http", "https"} or not home.hostname or home.username or home.password:
        raise ValueError("STATUS_HOME_URL must be an absolute HTTP(S) URL")


def load_cache() -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        payload = value.get("payload") if isinstance(value, dict) else None
        last_success = bounded_text(value.get("lastSuccessAt"), 64) if isinstance(value, dict) else ""
        if not isinstance(payload, dict):
            return None, None
        sanitized = sanitize_upstream_payload(payload, last_success or None)
        return sanitized, last_success or None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None, None


def save_cache(payload: dict[str, Any], last_success_at: str) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CACHE_PATH.with_suffix(CACHE_PATH.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"payload": payload, "lastSuccessAt": last_success_at}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, CACHE_PATH)


def fetch_upstream() -> dict[str, Any]:
    request = Request(
        UPSTREAM_URL,
        headers={"Accept": "application/json", "User-Agent": "CakeStatus/1.0"},
        method="GET",
    )
    expected = urlparse(UPSTREAM_URL)
    with build_opener().open(request, timeout=UPSTREAM_TIMEOUT_SECONDS) as response:
        final = urlparse(response.geturl())
        expected_port = expected.port or (443 if expected.scheme == "https" else 80)
        final_port = final.port or (443 if final.scheme == "https" else 80)
        if (final.scheme, final.hostname, final_port) != (expected.scheme, expected.hostname, expected_port):
            raise ValueError("cross_origin_upstream_redirect")
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError("upstream_payload_too_large")
    return json.loads(payload.decode("utf-8"))


def poll_once() -> dict[str, Any]:
    global _current_payload, _last_good_payload, _last_success_at
    checked_at = utc_now()
    try:
        payload = sanitize_upstream_payload(fetch_upstream(), checked_at)
        with _state_lock:
            _last_good_payload = payload
            _last_success_at = checked_at
            _current_payload = payload
        try:
            save_cache(payload, checked_at)
        except OSError:
            pass
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        with _state_lock:
            _current_payload = offline_payload(_last_good_payload, checked_at, _last_success_at)
    with _state_lock:
        return json.loads(json.dumps(_current_payload, ensure_ascii=False))


def current_payload() -> dict[str, Any]:
    with _state_lock:
        return json.loads(json.dumps(_current_payload, ensure_ascii=False))


def poll_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        poll_once()
        stop_event.wait(POLL_INTERVAL_SECONDS)


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "CakeStatus/1.0"

    def security_headers(self, content_type: str, cache_control: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

    def send_payload(self, status: HTTPStatus, payload: bytes, content_type: str, cache_control: str) -> None:
        self.send_response(status)
        self.security_headers(content_type, cache_control)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def serve_static(self, relative_path: str, content_type: str) -> None:
        path = STATIC_ROOT / relative_path
        try:
            payload = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        cache = "no-store" if relative_path == "index.html" else "public, max-age=3600"
        self.send_payload(HTTPStatus.OK, payload, content_type, cache)

    def handle_get(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in {"/", "/index.html"}:
            self.serve_static("index.html", "text/html; charset=utf-8")
        elif path == "/app.js":
            self.serve_static("app.js", "text/javascript; charset=utf-8")
        elif path == "/styles.css":
            self.serve_static("styles.css", "text/css; charset=utf-8")
        elif path == "/assets/cake-mark.png":
            self.serve_static("assets/cake-mark.png", "image/png")
        elif path == "/assets/favicon.png":
            self.serve_static("assets/favicon.png", "image/png")
        elif path == "/api/v1/public/status":
            self.send_payload(HTTPStatus.OK, json_bytes(current_payload()), "application/json; charset=utf-8", "no-store")
        elif path == "/health":
            payload = current_payload()
            self.send_payload(
                HTTPStatus.OK,
                json_bytes({"status": "ok", "upstreamAvailable": bool(payload.get("upstreamAvailable"))}),
                "application/json; charset=utf-8",
                "no-store",
            )
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_GET(self) -> None:
        self.handle_get()

    def do_HEAD(self) -> None:
        self.handle_get()

    def log_message(self, message: str, *args: Any) -> None:
        safe_message = message % args
        print(f'{self.client_address[0]} - "{safe_message}"')


def main() -> None:
    global _current_payload, _last_good_payload, _last_success_at
    validate_configuration()
    cached, last_success = load_cache()
    _last_good_payload = cached
    _last_success_at = last_success
    _current_payload = offline_payload(cached, utc_now(), last_success)
    stop_event = threading.Event()
    poller = threading.Thread(target=poll_loop, args=(stop_event,), name="status-poller", daemon=True)
    poller.start()
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()
        poller.join(timeout=2)


if __name__ == "__main__":
    main()
