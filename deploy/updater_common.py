from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
STATE_DIR = Path(os.environ.get("CAKE_STATUS_UPDATER_STATE_DIR", "/var/lib/cake-status/updater"))
JOBS_DIR = STATE_DIR / "jobs"
SOCKET_PATH = Path(os.environ.get("CAKE_STATUS_UPDATER_SOCKET", "/run/cake-status/updater.sock"))
ENV_PATH = Path(os.environ.get("CAKE_STATUS_ENV", "/etc/cake-status/status.env"))
INSTALL_DIR = Path(os.environ.get("CAKE_STATUS_INSTALL_DIR", "/opt/cake-status"))
COMPOSE_PATH = INSTALL_DIR / "deploy/docker-compose.release.yml"
LOCK_PATH = STATE_DIR / "update.lock"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_version(value: Any) -> str:
    version = str(value or "")
    if not SEMVER.fullmatch(version):
        raise ValueError("version must be an exact semantic version")
    return version


def read_env(path: Path = ENV_PATH) -> tuple[str, dict[str, str]]:
    raw = path.read_text(encoding="utf-8")
    values: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return raw, values


def replace_env_values(raw: str, updates: dict[str, str]) -> str:
    remaining = dict(updates)
    output: list[str] = []
    for line in raw.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
                continue
        output.append(line)
    output.extend(f"{key}={value}" for key, value in remaining.items())
    return "\n".join(output) + "\n"


def atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def job_path(job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ValueError("invalid job id")
    return JOBS_DIR / f"{job_id}.json"


def save_job(job: dict[str, Any]) -> None:
    job["updatedAt"] = now()
    atomic_write(job_path(str(job["id"])), json.dumps(job, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def load_job(job_id: str) -> dict[str, Any]:
    return json.loads(job_path(job_id).read_text(encoding="utf-8"))
