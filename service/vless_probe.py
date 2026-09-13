"""Bounded VLESS canary checks executed through Xray.

Canary credentials are runtime secrets. This module never returns a URI or any
credential-bearing Xray configuration to callers.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


XRAY_BINARY = os.getenv("STATUS_XRAY_BINARY", "/usr/local/bin/xray")
DEFAULT_CANARY_FILE = os.getenv("STATUS_VLESS_CANARY_FILE", "/run/secrets/status-vless-canaries.json")
MAX_CANARY_FILE_BYTES = 256 * 1024
DEFAULT_TIMEOUT_SECONDS = 12.0


class CanaryConfigurationError(ValueError):
    """Invalid canary configuration, represented by a safe reason code."""


def _single(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    return values[0] if values else default


def endpoint_key(host: str, port: int) -> str:
    return f"{host.strip().lower().rstrip('.')}:{port}"


def load_canaries(path: str = DEFAULT_CANARY_FILE) -> dict[str, dict[str, Any]]:
    candidate = Path(path)
    try:
        size = candidate.stat().st_size
    except FileNotFoundError:
        return {}
    if size > MAX_CANARY_FILE_BYTES:
        raise CanaryConfigurationError("canary_file_too_large")
    try:
        document = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CanaryConfigurationError("canary_file_invalid") from exc
    if not isinstance(document, dict) or document.get("schemaVersion") != 1:
        raise CanaryConfigurationError("canary_schema_unsupported")
    probes = document.get("probes")
    if not isinstance(probes, dict) or len(probes) > 100:
        raise CanaryConfigurationError("canary_probes_invalid")
    return {str(key).lower(): value for key, value in probes.items() if isinstance(value, dict)}


def _stream_settings(query: dict[str, list[str]]) -> dict[str, Any]:
    network = _single(query, "type", "tcp").lower()
    network_map = {"tcp": "raw", "raw": "raw", "ws": "ws", "grpc": "grpc", "xhttp": "xhttp"}
    if network not in network_map:
        raise CanaryConfigurationError("transport_unsupported")
    security = _single(query, "security", "none").lower()
    stream: dict[str, Any] = {"network": network_map[network], "security": security}
    host_header = _single(query, "host")
    path = urllib.parse.unquote(_single(query, "path", "/"))
    if network == "ws":
        stream["wsSettings"] = {"path": path, "headers": {"Host": host_header} if host_header else {}}
    elif network == "grpc":
        stream["grpcSettings"] = {
            "serviceName": urllib.parse.unquote(_single(query, "serviceName")),
            "multiMode": _single(query, "mode").lower() == "multi",
        }
    elif network == "xhttp":
        xhttp: dict[str, Any] = {"path": path}
        if host_header:
            xhttp["host"] = host_header
        mode = _single(query, "mode")
        if mode:
            xhttp["mode"] = mode
        stream["xhttpSettings"] = xhttp
    if security == "reality":
        password = _single(query, "pbk") or _single(query, "password")
        server_name = _single(query, "sni")
        if not password or not server_name:
            raise CanaryConfigurationError("reality_parameters_missing")
        reality: dict[str, Any] = {
            "serverName": server_name,
            "fingerprint": _single(query, "fp", "chrome"),
            "password": password,
            "shortId": _single(query, "sid"),
        }
        spider_x = _single(query, "spx")
        if spider_x:
            reality["spiderX"] = urllib.parse.unquote(spider_x)
        stream["realitySettings"] = reality
    elif security == "tls":
        tls: dict[str, Any] = {"allowInsecure": False}
        server_name = _single(query, "sni")
        if server_name:
            tls["serverName"] = server_name
        fingerprint = _single(query, "fp")
        if fingerprint:
            tls["fingerprint"] = fingerprint
        stream["tlsSettings"] = tls
    elif security != "none":
        raise CanaryConfigurationError("security_unsupported")
    return stream


def build_xray_config(uri_value: str, local_port: int) -> tuple[dict[str, Any], str, int]:
    try:
        uri = urllib.parse.urlsplit(uri_value)
        port = uri.port
    except ValueError as exc:
        raise CanaryConfigurationError("uri_invalid") from exc
    if uri.scheme.lower() != "vless" or not uri.username or not uri.hostname or not port:
        raise CanaryConfigurationError("uri_invalid")
    query = urllib.parse.parse_qs(uri.query, keep_blank_values=True)
    outbound: dict[str, Any] = {
        "address": uri.hostname,
        "port": port,
        "id": urllib.parse.unquote(uri.username),
        "encryption": _single(query, "encryption", "none"),
    }
    flow = _single(query, "flow")
    if flow:
        outbound["flow"] = flow
    config = {
        "log": {"loglevel": "none"},
        "inbounds": [{
            "tag": "canary-http", "listen": "127.0.0.1", "port": local_port,
            "protocol": "http", "settings": {"auth": "noauth", "udp": False},
        }],
        "outbounds": [{
            "tag": "canary-vless", "protocol": "vless", "settings": outbound,
            "streamSettings": _stream_settings(query),
        }, {"tag": "blocked", "protocol": "blackhole"}],
        "routing": {"domainStrategy": "AsIs", "rules": [{
            "type": "field", "inboundTag": ["canary-http"], "outboundTag": "canary-vless",
        }]},
    }
    return config, uri.hostname.lower().rstrip("."), port


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_listener(port: int, process: subprocess.Popen[bytes], deadline: float) -> bool:
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.15):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1.5)


def probe_vless_target(
    host: str,
    port: int,
    *,
    canary_file: str = DEFAULT_CANARY_FILE,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        probes = load_canaries(canary_file)
    except CanaryConfigurationError as exc:
        return {"status": "unknown", "checkStatus": "unavailable", "reasonCode": str(exc)}
    probe = probes.get(endpoint_key(host, port))
    if not probe:
        return {"status": "unknown", "checkStatus": "not_configured", "reasonCode": "canary_not_configured"}
    uri_value = probe.get("uri")
    test_url = probe.get("testUrl", "https://www.gstatic.com/generate_204")
    expected_status = probe.get("expectedStatus", 204)
    if (
        not isinstance(uri_value, str) or len(uri_value) > 8192
        or not isinstance(test_url, str) or not test_url.startswith("https://")
        or not isinstance(expected_status, int)
    ):
        return {"status": "unknown", "checkStatus": "unavailable", "reasonCode": "canary_invalid"}
    if not os.path.isfile(XRAY_BINARY) or not os.access(XRAY_BINARY, os.X_OK):
        return {"status": "unknown", "checkStatus": "unavailable", "reasonCode": "xray_unavailable"}
    local_port = _reserve_port()
    try:
        config, uri_host, uri_port = build_xray_config(uri_value, local_port)
    except CanaryConfigurationError as exc:
        return {"status": "unknown", "checkStatus": "unavailable", "reasonCode": str(exc)}
    if endpoint_key(uri_host, uri_port) != endpoint_key(host, port):
        return {"status": "unknown", "checkStatus": "unavailable", "reasonCode": "canary_endpoint_mismatch"}
    deadline = time.monotonic() + max(2.0, min(timeout_seconds, 30.0))
    process: subprocess.Popen[bytes] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="status-canary-") as directory:
            config_path = Path(directory, "config.json")
            config_path.write_text(json.dumps(config, ensure_ascii=True), encoding="utf-8")
            os.chmod(config_path, 0o600)
            process = subprocess.Popen(
                [XRAY_BINARY, "run", "-c", str(config_path)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            )
            if not _wait_for_listener(local_port, process, min(deadline, time.monotonic() + 3.0)):
                return {"status": "unknown", "checkStatus": "unavailable", "reasonCode": "xray_start_failed"}
            proxy = urllib.request.ProxyHandler({
                "http": f"http://127.0.0.1:{local_port}", "https": f"http://127.0.0.1:{local_port}"
            })
            opener = urllib.request.build_opener(proxy, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
            request = urllib.request.Request(test_url, method="GET", headers={"User-Agent": "cake-status-canary/1"})
            try:
                with opener.open(request, timeout=max(0.5, deadline - time.monotonic())) as response:
                    actual_status = response.status
                    response.read(4096)
            except urllib.error.HTTPError as exc:
                actual_status = exc.code
            latency_ms = round((time.monotonic() - started) * 1000)
            if actual_status == expected_status:
                return {"status": "operational", "checkStatus": "completed", "latencyMs": latency_ms, "reasonCode": "ok"}
            return {"status": "major_outage", "checkStatus": "completed", "latencyMs": latency_ms, "reasonCode": "unexpected_response"}
    except (OSError, subprocess.SubprocessError, urllib.error.URLError, TimeoutError, socket.timeout):
        return {"status": "major_outage", "checkStatus": "completed", "latencyMs": round((time.monotonic() - started) * 1000), "reasonCode": "probe_failed"}
    finally:
        if process is not None:
            _stop_process(process)
