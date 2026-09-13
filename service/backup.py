"""Logical backup/restore for status-owned persistent data.

The VLESS canary secret and environment are intentionally outside this format.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

BACKUP_SCHEMA = 1
MAX_BACKUP_BYTES = 5 * 1024 * 1024
TARGETS_PATH = Path(os.environ.get("STATUS_TARGETS_PATH", "/data/status-targets.json"))
CACHE_PATH = Path(os.environ.get("STATUS_CACHE_PATH", "/data/status-cache.json"))
MEMBERS = {"data/status-targets.json", "data/status-cache.json", "README.txt", "manifest.json"}


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def safe_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return fallback


def validate_targets(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 50:
        raise ValueError("invalid targets")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("invalid target")
        allowed = {key: item.get(key) for key in ("configurationKey", "label", "host", "port")}
        if not isinstance(allowed["configurationKey"], str) or len(allowed["configurationKey"]) != 64:
            raise ValueError("invalid target key")
        if not isinstance(allowed["label"], str) or not 1 <= len(allowed["label"]) <= 96:
            raise ValueError("invalid target label")
        if not isinstance(allowed["host"], str) or not 1 <= len(allowed["host"]) <= 253:
            raise ValueError("invalid target host")
        if not isinstance(allowed["port"], int) or not 1 <= allowed["port"] <= 65535:
            raise ValueError("invalid target port")
        endpoint = f"{allowed['host'].lower().rstrip('.')}:{allowed['port']}"
        if endpoint in seen:
            continue
        seen.add(endpoint)
        result.append(allowed)
    return result


def validate_cache(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("payload"), dict):
        raise ValueError("invalid cache")
    components = value["payload"].get("components")
    if not isinstance(components, list) or len(components) > 50:
        raise ValueError("invalid cache components")
    return value


def record_count(name: str, value: Any) -> int:
    if name.endswith("status-targets.json"):
        return len(value) if isinstance(value, list) else 0
    if name.endswith("status-cache.json"):
        components = value.get("payload", {}).get("components", []) if isinstance(value, dict) else []
        return len(components) if isinstance(components, list) else 0
    return 0


def create_backup() -> bytes:
    values = {
        "data/status-targets.json": validate_targets(safe_json(TARGETS_PATH, [])),
        "data/status-cache.json": safe_json(CACHE_PATH, {"payload": {"components": []}, "lastSuccessAt": None}),
    }
    if values["data/status-cache.json"] != {"payload": {"components": []}, "lastSuccessAt": None}:
        validate_cache(values["data/status-cache.json"])
    encoded = {name: canonical_json(value) for name, value in values.items()}
    readme = (
        "Cake Status logical backup\n\n"
        "Contains public target metadata and the sanitized status cache.\n"
        "Runtime environment, admin tokens, and VLESS canary secrets/URIs are deliberately excluded.\n"
    ).encode("utf-8")
    manifest = {
        "schemaVersion": BACKUP_SCHEMA,
        "service": "cake-status",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "files": [
            {
                "path": name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
                "recordCount": record_count(name, values[name]),
            }
            for name, payload in sorted(encoded.items())
        ],
        "excluded": ["environment", "admin tokens", "VLESS canary secrets"],
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, payload in sorted(encoded.items()):
            archive.writestr(name, payload)
        archive.writestr("README.txt", readme)
        archive.writestr("manifest.json", canonical_json(manifest))
    result = output.getvalue()
    if len(result) > MAX_BACKUP_BYTES:
        raise ValueError("backup too large")
    return result


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".restore")
    temporary.write_bytes(payload)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def restore_backup(payload: bytes) -> None:
    if not payload or len(payload) > MAX_BACKUP_BYTES:
        raise ValueError("invalid backup size")
    with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != MEMBERS:
            raise ValueError("invalid backup members")
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("unsafe backup path")
            info = archive.getinfo(name)
            if info.file_size > MAX_BACKUP_BYTES or info.compress_size == 0 and info.file_size > 0:
                raise ValueError("invalid backup member")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("schemaVersion") != BACKUP_SCHEMA or manifest.get("service") != "cake-status":
            raise ValueError("unsupported backup")
        expected = {item.get("path"): item for item in manifest.get("files", []) if isinstance(item, dict)}
        raw_targets = archive.read("data/status-targets.json")
        raw_cache = archive.read("data/status-cache.json")
        for name, member in (("data/status-targets.json", raw_targets), ("data/status-cache.json", raw_cache)):
            metadata = expected.get(name)
            if not metadata or metadata.get("size") != len(member) or metadata.get("sha256") != hashlib.sha256(member).hexdigest():
                raise ValueError("backup checksum mismatch")
        targets = validate_targets(json.loads(raw_targets))
        cache = validate_cache(json.loads(raw_cache))
        _atomic_write(TARGETS_PATH, canonical_json(targets))
        _atomic_write(CACHE_PATH, canonical_json(cache))


def main() -> None:
    parser = argparse.ArgumentParser(description="Cake Status logical backup tool")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup")
    backup.add_argument("--output", default="-")
    restore = subparsers.add_parser("restore")
    restore.add_argument("--input", default="-")
    args = parser.parse_args()
    if args.command == "backup":
        payload = create_backup()
        if args.output == "-":
            sys.stdout.buffer.write(payload)
        else:
            Path(args.output).write_bytes(payload)
    else:
        payload = sys.stdin.buffer.read(MAX_BACKUP_BYTES + 1) if args.input == "-" else Path(args.input).read_bytes()
        restore_backup(payload)


if __name__ == "__main__":
    main()
