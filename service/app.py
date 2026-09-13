from __future__ import annotations

import hmac
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener

try:
    from vless_probe import probe_vless_target
except ModuleNotFoundError:  # Supports package-style imports in tests and tooling.
    from service.vless_probe import probe_vless_target

ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = ROOT / "static"
STATUS_VALUES = {"operational", "degraded", "partial_outage", "major_outage", "unknown"}
COMPONENT_KEYS = ("connection", "specific_connections", "new_connections", "subscriptions")
SERVICE_COMPONENT_KEYS = ("connection", "new_connections", "subscriptions")
COMPONENT_TITLES = {
    "connection": "Подключение к Loki",
    "specific_connections": "Работоспособность конкретных подключений",
    "new_connections": "Создание новых подключений",
    "subscriptions": "Получение и обновление подписок",
}
MAX_RESPONSE_BYTES = 256 * 1024
MAX_REQUEST_BYTES = 64 * 1024
UPSTREAM_URL = os.environ.get("STATUS_UPSTREAM_URL", "http://host.docker.internal:18080/api/v1/public/status").strip()
ALLOW_HTTP_UPSTREAM = os.environ.get("STATUS_ALLOW_HTTP_UPSTREAM", "0") == "1"
HOME_URL = os.environ.get("STATUS_HOME_URL", "https://cake.shmoza.net/initialize/").strip()
POLL_INTERVAL_SECONDS = min(3600, max(10, int(os.environ.get("STATUS_POLL_INTERVAL_SECONDS", "30"))))
UPSTREAM_TIMEOUT_SECONDS = min(30, max(1, int(os.environ.get("STATUS_UPSTREAM_TIMEOUT_SECONDS", "8"))))
CACHE_PATH = Path(os.environ.get("STATUS_CACHE_PATH", "/data/status-cache.json"))
TARGETS_PATH = Path(os.environ.get("STATUS_TARGETS_PATH", "/data/status-targets.json"))
ADMIN_TOKEN = os.environ.get("STATUS_ADMIN_TOKEN", "").strip()
VLESS_PROBE_TIMEOUT_SECONDS = min(30, max(2, int(os.environ.get("STATUS_VLESS_PROBE_TIMEOUT_SECONDS", "12"))))
LISTEN_HOST = os.environ.get("STATUS_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("STATUS_LISTEN_PORT", "8080"))
ALLOWED_HOSTS = {
    item.strip().lower().rstrip(".")
    for item in os.environ.get("STATUS_ALLOWED_HOSTS", "statuscake.shmoza.net,localhost,127.0.0.1,::1").split(",")
    if item.strip()
}

_state_lock = threading.RLock()
_current_payload: dict[str, Any] = {}
_last_good_payload: dict[str, Any] | None = None
_last_success_at: str | None = None
_targets: list[dict[str, Any]] = []
_initialized = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bounded_text(value: Any, maximum: int, fallback: str = "") -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    return text[:maximum] or fallback


def log_event(event: str, **fields: Any) -> None:
    record = {"timestamp": utc_now(), "event": event}
    for key, value in fields.items():
        if key in {"authorization", "token", "uri", "body"}:
            continue
        if isinstance(value, str):
            record[key] = bounded_text(value, 256)
        elif isinstance(value, (int, float, bool)) or value is None:
            record[key] = value
    print(json.dumps(record, ensure_ascii=True, separators=(",", ":")), flush=True)


def normalized_status(value: Any) -> str:
    status = str(value or "unknown")
    return status if status in STATUS_VALUES else "unknown"


def public_status_message(status: str) -> str:
    return {
        "operational": "Работает штатно",
        "degraded": "Работает с ограничениями",
        "partial_outage": "Частичная недоступность",
        "major_outage": "Недоступно",
        "unknown": "Проверка недоступна",
    }.get(status, "Проверка недоступна")


def check_message(status: str, check_status: str) -> str:
    if check_status == "not_configured":
        return "Проверка не настроена"
    if check_status != "completed":
        return "Проверка недоступна"
    return public_status_message(status)


def normalize_targets(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 50:
        raise ValueError("invalid_targets")
    result: list[dict[str, Any]] = []
    seen_endpoints: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("invalid_target")
        key = str(item.get("configurationKey") or "")
        host = bounded_text(item.get("host"), 253).lower().rstrip(".")
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
        result.append({
            "configurationKey": key,
            "label": bounded_text(item.get("label"), 96, "Подключение"),
            "host": host,
            "port": port,
        })
    return result


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def load_targets() -> list[dict[str, Any]]:
    try:
        return normalize_targets(json.loads(TARGETS_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def current_targets() -> list[dict[str, Any]]:
    with _state_lock:
        return json.loads(json.dumps(_targets, ensure_ascii=False))


def replace_targets(value: Any) -> list[dict[str, Any]]:
    global _targets
    targets = normalize_targets(value)
    _atomic_json(TARGETS_PATH, targets)
    with _state_lock:
        _targets = targets
    return targets


def probe_target(target: dict[str, Any]) -> dict[str, Any]:
    outcome = probe_vless_target(str(target["host"]), int(target["port"]), timeout_seconds=VLESS_PROBE_TIMEOUT_SECONDS)
    status = normalized_status(outcome.get("status"))
    check_status = bounded_text(outcome.get("checkStatus"), 32, "unavailable")
    result: dict[str, Any] = {
        "label": target["label"],
        "status": status,
        "serviceStatus": status if check_status == "completed" else "unknown",
        "checkStatus": check_status,
        "message": check_message(status, check_status),
        "checkedAt": utc_now(),
    }
    if isinstance(outcome.get("latencyMs"), int):
        result["latencyMs"] = max(0, min(int(outcome["latencyMs"]), 120_000))
    return result


def probe_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not targets:
        return []
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(targets))) as executor:
        futures = {executor.submit(probe_target, target): target for target in targets}
        for future in as_completed(futures):
            target = futures[future]
            try:
                results[target["configurationKey"]] = future.result()
            except Exception:
                results[target["configurationKey"]] = {
                    "label": target["label"], "status": "unknown", "serviceStatus": "unknown",
                    "checkStatus": "unavailable", "message": "Проверка недоступна", "checkedAt": utc_now(),
                }
    return [results[target["configurationKey"]] for target in targets]


def aggregate_connections(results: list[dict[str, Any]]) -> str:
    if not results:
        return "unknown"
    statuses = [normalized_status(item.get("status")) for item in results]
    known = [status for status in statuses if status != "unknown"]
    if not known:
        return "unknown"
    if all(status == "operational" for status in statuses):
        return "operational"
    if len(known) == len(statuses) and all(status == "major_outage" for status in known):
        return "major_outage"
    if any(status == "major_outage" for status in known):
        return "partial_outage"
    return "degraded"


def aggregate_services(components: list[dict[str, Any]]) -> str:
    by_key = {str(item.get("key")): normalized_status(item.get("status")) for item in components}
    statuses = [by_key.get(key, "unknown") for key in SERVICE_COMPONENT_KEYS]
    if by_key.get("connection") == "major_outage":
        return "major_outage"
    if all(status == "operational" for status in statuses):
        return "operational"
    if any(status == "major_outage" for status in statuses):
        return "partial_outage"
    if any(status in {"degraded", "partial_outage"} for status in statuses):
        return "degraded"
    return "unknown" if all(status == "unknown" for status in statuses) else "degraded"


def apply_summaries(payload: dict[str, Any]) -> dict[str, Any]:
    components = payload.get("components") if isinstance(payload.get("components"), list) else []
    details: list[dict[str, Any]] = []
    for component in components:
        if isinstance(component, dict) and component.get("key") == "specific_connections":
            details = component.get("details") if isinstance(component.get("details"), list) else []
            break
    services = aggregate_services(components)
    connections = aggregate_connections(details)
    payload["schemaVersion"] = 2
    payload["summaries"] = {
        "services": {"status": services, "message": public_status_message(services)},
        "connections": {"status": connections, "message": public_status_message(connections)},
    }
    payload["overallStatus"] = services
    payload["message"] = public_status_message(services)
    return payload


def sanitize_upstream_payload(value: Any, checked_at: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("components"), list):
        raise ValueError("invalid_upstream_payload")
    source_by_key = {
        str(item.get("key")): item for item in value["components"][:50]
        if isinstance(item, dict) and str(item.get("key")) in COMPONENT_KEYS
    }
    components: list[dict[str, Any]] = []
    for key in COMPONENT_KEYS:
        source = source_by_key.get(key, {})
        status = "operational" if key == "connection" else normalized_status(source.get("status"))
        check_status = bounded_text(source.get("checkStatus"), 32, "completed")
        component: dict[str, Any] = {
            "key": key,
            "title": bounded_text(source.get("title"), 128, COMPONENT_TITLES[key]),
            "status": status,
            "serviceStatus": normalized_status(source.get("serviceStatus") or status),
            "checkStatus": check_status,
            "message": (
                check_message(status, check_status)
                if check_status != "completed"
                else bounded_text(source.get("message"), 160, check_message(status, check_status))
            ),
            "checkedAt": bounded_text(source.get("checkedAt"), 64) or checked_at,
        }
        if key == "specific_connections":
            component["details"] = []
        components.append(component)
    return apply_summaries({
        "checkedAt": bounded_text(value.get("checkedAt"), 64) or checked_at,
        "components": components, "upstreamAvailable": True, "upstreamCheckedAt": checked_at,
        "lastSuccessAt": checked_at, "homeUrl": HOME_URL,
    })


def offline_payload(cached: dict[str, Any] | None, checked_at: str, last_success_at: str | None) -> dict[str, Any]:
    cached_components = {
        str(item.get("key")): item for item in ((cached or {}).get("components") or []) if isinstance(item, dict)
    }
    components: list[dict[str, Any]] = []
    for key in COMPONENT_KEYS:
        previous = cached_components.get(key, {})
        component: dict[str, Any] = {
            "key": key,
            "title": bounded_text(previous.get("title"), 128, COMPONENT_TITLES[key]),
            "status": "unknown",
            "serviceStatus": normalized_status(previous.get("serviceStatus") or previous.get("status")),
            "checkStatus": "unavailable",
            "message": "Проверка недоступна",
            "checkedAt": checked_at,
        }
        if key == "specific_connections":
            component["details"] = []
        components.append(component)
    return apply_summaries({
        "checkedAt": checked_at, "components": components, "upstreamAvailable": False,
        "upstreamCheckedAt": checked_at, "lastSuccessAt": last_success_at, "homeUrl": HOME_URL,
    })


def apply_target_results(payload: dict[str, Any], results: list[dict[str, Any]], checked_at: str) -> dict[str, Any]:
    updated = json.loads(json.dumps(payload, ensure_ascii=False))
    components = updated.get("components") if isinstance(updated.get("components"), list) else []
    for component in components:
        if isinstance(component, dict) and component.get("key") == "specific_connections":
            status = aggregate_connections(results)
            detail_checks = [str(item.get("checkStatus") or "unavailable") for item in results]
            if not results or (detail_checks and all(value == "not_configured" for value in detail_checks)):
                check_status = "not_configured"
            elif any(value == "completed" for value in detail_checks):
                check_status = "completed"
            else:
                check_status = "unavailable"
            component.update({
                "status": status, "serviceStatus": status, "checkStatus": check_status,
                "message": check_message(status, check_status), "checkedAt": checked_at, "details": results,
            })
            break
    return apply_summaries(updated)


def validate_configuration() -> None:
    parsed = urlparse(UPSTREAM_URL)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("STATUS_UPSTREAM_URL must be an absolute HTTP(S) URL without credentials or a fragment")
    if parsed.scheme != "https" and not ALLOW_HTTP_UPSTREAM:
        raise ValueError("STATUS_UPSTREAM_URL must use HTTPS unless STATUS_ALLOW_HTTP_UPSTREAM=1")
    home = urlparse(HOME_URL)
    if home.scheme not in {"http", "https"} or not home.hostname or home.username or home.password:
        raise ValueError("STATUS_HOME_URL must be an absolute HTTP(S) URL")
    if not ALLOWED_HOSTS:
        raise ValueError("STATUS_ALLOWED_HOSTS must contain at least one host")


def load_cache() -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        payload = value.get("payload") if isinstance(value, dict) else None
        last_success = bounded_text(value.get("lastSuccessAt"), 64) if isinstance(value, dict) else ""
        if not isinstance(payload, dict):
            return None, None
        return sanitize_upstream_payload(payload, last_success or None), last_success or None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None, None


def save_cache(payload: dict[str, Any], last_success_at: str) -> None:
    _atomic_json(CACHE_PATH, {"payload": payload, "lastSuccessAt": last_success_at})


def fetch_upstream() -> dict[str, Any]:
    request = Request(UPSTREAM_URL, headers={"Accept": "application/json", "User-Agent": "CakeStatus"}, method="GET")
    expected = urlparse(UPSTREAM_URL)
    with build_opener().open(request, timeout=UPSTREAM_TIMEOUT_SECONDS) as response:
        final = urlparse(response.geturl())
        expected_port = expected.port or (443 if expected.scheme == "https" else 80)
        final_port = final.port or (443 if final.scheme == "https" else 80)
        if (final.scheme, final.hostname, final_port) != (expected.scheme, expected.hostname, expected_port):
            raise ValueError("cross_origin_upstream_redirect")
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("upstream_payload_too_large")
    return json.loads(body.decode("utf-8"))


def poll_once() -> dict[str, Any]:
    global _current_payload, _last_good_payload, _last_success_at
    checked_at = utc_now()
    target_results = probe_targets(current_targets())
    try:
        payload = apply_target_results(sanitize_upstream_payload(fetch_upstream(), checked_at), target_results, checked_at)
        with _state_lock:
            _last_good_payload, _last_success_at, _current_payload = payload, checked_at, payload
        try:
            save_cache(payload, checked_at)
        except OSError:
            log_event("cache_write_failed")
        log_event("poll_completed", upstreamAvailable=True, targetCount=len(target_results))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        with _state_lock:
            _current_payload = apply_target_results(offline_payload(_last_good_payload, checked_at, _last_success_at), target_results, checked_at)
        log_event("poll_completed", upstreamAvailable=False, targetCount=len(target_results), errorType=type(exc).__name__)
    return current_payload()


def current_payload() -> dict[str, Any]:
    with _state_lock:
        return json.loads(json.dumps(_current_payload, ensure_ascii=False))


def poll_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        poll_once()
        stop_event.wait(POLL_INTERVAL_SECONDS)


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def request_host(value: str) -> str:
    candidate = value.strip().lower()
    if candidate.startswith("[") and "]" in candidate:
        return candidate[1:candidate.index("]")].rstrip(".")
    if candidate.count(":") == 1:
        candidate = candidate.rsplit(":", 1)[0]
    return candidate.rstrip(".")


class Handler(BaseHTTPRequestHandler):
    server_version = ""
    sys_version = ""

    def send_response(self, code: int, message: str | None = None) -> None:
        self.log_request(code)
        self.send_response_only(code, message)
        self.send_header("Date", self.date_time_string())

    def security_headers(self, content_type: str, cache_control: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")

    def send_payload(self, status: HTTPStatus, payload: bytes, content_type: str, cache_control: str) -> None:
        self.send_response(status)
        self.security_headers(content_type, cache_control)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def send_json_error(self, status: HTTPStatus, code: str) -> None:
        self.send_payload(status, json_bytes({"error": code}), "application/json; charset=utf-8", "no-store")

    def host_allowed(self) -> bool:
        return request_host(str(self.headers.get("Host") or "")) in ALLOWED_HOSTS

    def serve_static(self, relative_path: str, content_type: str) -> None:
        try:
            payload = (STATIC_ROOT / relative_path).read_bytes()
        except OSError:
            self.send_json_error(HTTPStatus.NOT_FOUND, "not_found")
            return
        cache = "no-store" if relative_path == "index.html" else "public, max-age=3600"
        self.send_payload(HTTPStatus.OK, payload, content_type, cache)

    def handle_get(self) -> None:
        if not self.host_allowed():
            self.send_json_error(HTTPStatus.BAD_REQUEST, "invalid_host")
            return
        path = self.path.split("?", 1)[0]
        static = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/assets/cake-mark.png": ("assets/cake-mark.png", "image/png"),
            "/assets/favicon.png": ("assets/favicon.png", "image/png"),
            "/assets/SpaceGrotesk-Variable.ttf": ("assets/SpaceGrotesk-Variable.ttf", "font/ttf"),
            "/assets/SpaceGrotesk-OFL.txt": ("assets/SpaceGrotesk-OFL.txt", "text/plain; charset=utf-8"),
        }
        if path in static:
            self.serve_static(*static[path])
        elif path == "/robots.txt":
            self.send_payload(HTTPStatus.OK, b"User-agent: *\nDisallow: /\n", "text/plain; charset=utf-8", "public, max-age=3600")
        elif path == "/api/v1/public/status":
            self.send_payload(HTTPStatus.OK, json_bytes(current_payload()), "application/json; charset=utf-8", "no-store")
        elif path in {"/health", "/livez"}:
            self.send_payload(HTTPStatus.OK, json_bytes({"status": "ok"}), "application/json; charset=utf-8", "no-store")
        elif path == "/readyz":
            ready = _initialized and bool(current_payload())
            self.send_payload(
                HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE,
                json_bytes({"status": "ready" if ready else "not_ready"}),
                "application/json; charset=utf-8", "no-store",
            )
        else:
            self.send_json_error(HTTPStatus.NOT_FOUND, "not_found")

    def admin_authorized(self) -> bool:
        supplied = str(self.headers.get("Authorization") or "")
        return bool(ADMIN_TOKEN) and hmac.compare_digest(supplied, f"Bearer {ADMIN_TOKEN}")

    def handle_put(self) -> None:
        if not self.host_allowed():
            self.send_json_error(HTTPStatus.BAD_REQUEST, "invalid_host")
            return
        if self.path.split("?", 1)[0] != "/api/v1/admin/targets":
            self.send_json_error(HTTPStatus.NOT_FOUND, "not_found")
            return
        if not self.admin_authorized():
            self.send_json_error(HTTPStatus.UNAUTHORIZED, "unauthorized")
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if not 0 < length <= MAX_REQUEST_BYTES:
            self.send_json_error(HTTPStatus.BAD_REQUEST, "invalid_request_size")
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            targets = replace_targets(body.get("targets") if isinstance(body, dict) else None)
            payload = poll_once()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self.send_json_error(HTTPStatus.BAD_REQUEST, "invalid_targets")
            return
        self.send_payload(
            HTTPStatus.OK,
            json_bytes({"status": "updated", "targetCount": len(targets), "summaries": payload.get("summaries")}),
            "application/json; charset=utf-8", "no-store",
        )

    def do_GET(self) -> None:
        self.handle_get()

    def do_HEAD(self) -> None:
        self.handle_get()

    def do_PUT(self) -> None:
        self.handle_put()

    def method_not_allowed(self) -> None:
        if not self.host_allowed():
            self.send_json_error(HTTPStatus.BAD_REQUEST, "invalid_host")
            return
        self.send_json_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed")

    def do_POST(self) -> None:
        self.method_not_allowed()

    def do_DELETE(self) -> None:
        self.method_not_allowed()

    def do_PATCH(self) -> None:
        self.method_not_allowed()

    def do_OPTIONS(self) -> None:
        self.method_not_allowed()

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        log_event("http_request", method=self.command, path=self.path.split("?", 1)[0], status=int(code), client=self.client_address[0])

    def log_message(self, message: str, *args: Any) -> None:
        return


def main() -> None:
    global _current_payload, _last_good_payload, _last_success_at, _targets, _initialized
    validate_configuration()
    cached, last_success = load_cache()
    _last_good_payload, _last_success_at, _targets = cached, last_success, load_targets()
    _current_payload = offline_payload(cached, utc_now(), last_success)
    _initialized = True
    stop_event = threading.Event()
    poller = threading.Thread(target=poll_loop, args=(stop_event,), name="status-poller", daemon=True)
    poller.start()
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    log_event("server_started", listenHost=LISTEN_HOST, listenPort=LISTEN_PORT)
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
