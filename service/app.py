from __future__ import annotations

import hmac
import json
import os
import re
import socket
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
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
MAX_REQUEST_BYTES = 64 * 1024
UPSTREAM_URL = os.environ.get(
    "STATUS_UPSTREAM_URL",
    "http://host.docker.internal:18080/api/v1/public/status",
).strip()
ALLOW_HTTP_UPSTREAM = os.environ.get("STATUS_ALLOW_HTTP_UPSTREAM", "0") == "1"
HOME_URL = os.environ.get("STATUS_HOME_URL", "https://cake.shmoza.net/initialize/").strip()
POLL_INTERVAL_SECONDS = min(3600, max(10, int(os.environ.get("STATUS_POLL_INTERVAL_SECONDS", "30"))))
UPSTREAM_TIMEOUT_SECONDS = min(30, max(1, int(os.environ.get("STATUS_UPSTREAM_TIMEOUT_SECONDS", "8"))))
CACHE_PATH = Path(os.environ.get("STATUS_CACHE_PATH", "/data/status-cache.json"))
TARGETS_PATH = Path(os.environ.get("STATUS_TARGETS_PATH", "/data/status-targets.json"))
ADMIN_TOKEN = os.environ.get("STATUS_ADMIN_TOKEN", "").strip()
VLESS_PROBE_TIMEOUT_SECONDS = min(20, max(1, int(os.environ.get("STATUS_VLESS_PROBE_TIMEOUT_SECONDS", "5"))))
LISTEN_HOST = os.environ.get("STATUS_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("STATUS_LISTEN_PORT", "8080"))

_state_lock = threading.RLock()
_current_payload: dict[str, Any] = {}
_last_good_payload: dict[str, Any] | None = None
_last_success_at: str | None = None
_targets: list[dict[str, Any]] = []


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bounded_text(value: Any, maximum: int, fallback: str = "") -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    return text[:maximum] or fallback


def normalized_status(value: Any) -> str:
    status = str(value or "unknown")
    return status if status in STATUS_VALUES else "unknown"


def normalize_targets(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 50:
        raise ValueError("invalid_targets")
    result: list[dict[str, Any]] = []
    seen_endpoints: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("invalid_target")
        key = str(item.get("configurationKey") or "")
        host = bounded_text(item.get("host"), 253).lower()
        try:
            port = int(item.get("port"))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_target_port") from exc
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("invalid_target_key")
        if not host or re.search(r"[\s/:@]", host) or not 1 <= port <= 65535:
            raise ValueError("invalid_target_endpoint")
        endpoint = f"{host}:{port}"
        if endpoint in seen_endpoints:
            continue
        seen_endpoints.add(endpoint)
        result.append(
            {
                "configurationKey": key,
                "label": bounded_text(item.get("label"), 96, "Подключение"),
                "host": host,
                "port": port,
            }
        )
    return result


def load_targets() -> list[dict[str, Any]]:
    try:
        return normalize_targets(json.loads(TARGETS_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def save_targets(targets: list[dict[str, Any]]) -> None:
    TARGETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = TARGETS_PATH.with_suffix(TARGETS_PATH.suffix + ".tmp")
    temporary.write_text(json.dumps(targets, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, TARGETS_PATH)


def current_targets() -> list[dict[str, Any]]:
    with _state_lock:
        return json.loads(json.dumps(_targets, ensure_ascii=False))


def replace_targets(value: Any) -> list[dict[str, Any]]:
    global _targets
    targets = normalize_targets(value)
    save_targets(targets)
    with _state_lock:
        _targets = targets
    return targets


def probe_target(target: dict[str, Any]) -> dict[str, Any]:
    checked_at = utc_now()
    try:
        with socket.create_connection((str(target["host"]), int(target["port"])), timeout=VLESS_PROBE_TIMEOUT_SECONDS):
            pass
        status = "operational"
    except (socket.gaierror, TimeoutError, OSError):
        status = "major_outage"
    return {
        "label": target["label"],
        "status": status,
        "message": public_status_message(status),
        "checkedAt": checked_at,
    }


def probe_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not targets:
        return []
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as executor:
        futures = {executor.submit(probe_target, target): target for target in targets}
        for future in as_completed(futures):
            target = futures[future]
            try:
                results[target["configurationKey"]] = future.result()
            except Exception:
                results[target["configurationKey"]] = {
                    "label": target["label"],
                    "status": "unknown",
                    "message": public_status_message("unknown"),
                    "checkedAt": utc_now(),
                }
    return [results[target["configurationKey"]] for target in targets]


def aggregate_target_status(results: list[dict[str, Any]]) -> str:
    if not results:
        return "unknown"
    statuses = [normalized_status(item.get("status")) for item in results]
    if all(status == "operational" for status in statuses):
        return "operational"
    if all(status == "major_outage" for status in statuses):
        return "major_outage"
    if any(status == "operational" for status in statuses):
        return "partial_outage"
    return "degraded" if any(status == "degraded" for status in statuses) else "unknown"


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


def apply_target_results(payload: dict[str, Any], results: list[dict[str, Any]], checked_at: str) -> dict[str, Any]:
    updated = json.loads(json.dumps(payload, ensure_ascii=False))
    components = updated.get("components") if isinstance(updated.get("components"), list) else []
    for component in components:
        if isinstance(component, dict) and component.get("key") == "specific_connections":
            status = aggregate_target_status(results)
            component["status"] = status
            component["message"] = public_status_message(status)
            component["checkedAt"] = checked_at
            component["details"] = results
            break
    overall = aggregate_status(components)
    updated["overallStatus"] = overall
    updated["message"] = public_status_message(overall)
    return updated


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
    target_results = probe_targets(current_targets())
    try:
        payload = apply_target_results(sanitize_upstream_payload(fetch_upstream(), checked_at), target_results, checked_at)
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
            _current_payload = apply_target_results(
                offline_payload(_last_good_payload, checked_at, _last_success_at),
                target_results,
                checked_at,
            )
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

    def admin_authorized(self) -> bool:
        if not ADMIN_TOKEN:
            return False
        supplied = str(self.headers.get("Authorization") or "")
        return hmac.compare_digest(supplied, f"Bearer {ADMIN_TOKEN}")

    def handle_put(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != "/api/v1/admin/targets":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self.admin_authorized():
            self.send_payload(
                HTTPStatus.UNAUTHORIZED,
                json_bytes({"error": "unauthorized"}),
                "application/json; charset=utf-8",
                "no-store",
            )
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if not 0 < length <= MAX_REQUEST_BYTES:
            self.send_payload(
                HTTPStatus.BAD_REQUEST,
                json_bytes({"error": "invalid_request_size"}),
                "application/json; charset=utf-8",
                "no-store",
            )
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            targets = replace_targets(body.get("targets") if isinstance(body, dict) else None)
            payload = poll_once()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self.send_payload(
                HTTPStatus.BAD_REQUEST,
                json_bytes({"error": "invalid_targets"}),
                "application/json; charset=utf-8",
                "no-store",
            )
            return
        self.send_payload(
            HTTPStatus.OK,
            json_bytes({"status": "updated", "targetCount": len(targets), "overallStatus": payload.get("overallStatus")}),
            "application/json; charset=utf-8",
            "no-store",
        )

    def do_GET(self) -> None:
        self.handle_get()

    def do_HEAD(self) -> None:
        self.handle_get()

    def do_PUT(self) -> None:
        self.handle_put()

    def log_message(self, message: str, *args: Any) -> None:
        safe_message = message % args
        print(f'{self.client_address[0]} - "{safe_message}"')


def main() -> None:
    global _current_payload, _last_good_payload, _last_success_at, _targets
    validate_configuration()
    cached, last_success = load_cache()
    _last_good_payload = cached
    _last_success_at = last_success
    _targets = load_targets()
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
