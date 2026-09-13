from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from updater_common import (
        COMPOSE_PATH, ENV_PATH, IMAGE_DIGEST, LOCK_PATH, STATE_DIR, atomic_write,
        load_job, read_env, replace_env_values, save_job, validate_version,
    )
except ModuleNotFoundError:
    from deploy.updater_common import (
        COMPOSE_PATH, ENV_PATH, IMAGE_DIGEST, LOCK_PATH, STATE_DIR, atomic_write,
        load_job, read_env, replace_env_values, save_job, validate_version,
    )

MAX_MANIFEST_BYTES = 256 * 1024
BACKUP_DIR = Path(os.environ.get("CAKE_STATUS_BACKUP_DIR", "/var/lib/cake-status/backups"))


def run(
    command: list[str], *, stdin: Any = subprocess.DEVNULL,
    stdout: Any = subprocess.PIPE, timeout: int = 300,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(command, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        detail = (result.stderr or b"").decode("utf-8", "replace")[-1200:]
        raise RuntimeError(f"command failed ({command[0]}): {detail}")
    return result


def manifest_url(base_url: str, version: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("STATUS_RELEASE_BASE_URL must be an absolute HTTPS URL")
    return f"{base_url.rstrip('/')}/status-v{version}/status-v{version}-manifest.json"


def fetch_manifest(base_url: str, version: str) -> dict[str, Any]:
    url = manifest_url(base_url, version)
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "cake-status-updater"})
    with urllib.request.urlopen(request, timeout=20) as response:
        final = urllib.parse.urlparse(response.geturl())
        initial = urllib.parse.urlparse(url)
        allowed_redirect = (
            initial.hostname == "github.com"
            and isinstance(final.hostname, str)
            and (final.hostname == "objects.githubusercontent.com" or final.hostname.endswith(".githubusercontent.com"))
        )
        if final.scheme != "https" or (final.hostname != initial.hostname and not allowed_redirect):
            raise ValueError("cross-origin manifest redirect is not allowed")
        body = response.read(MAX_MANIFEST_BYTES + 1)
    if len(body) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest too large")
    value = json.loads(body)
    if not isinstance(value, dict) or value.get("schemaVersion") != 1 or value.get("service") != "cake-status":
        raise ValueError("unsupported manifest")
    if value.get("version") != version or value.get("releaseTag") != f"status-v{version}":
        raise ValueError("manifest version mismatch")
    image, digest = value.get("image"), value.get("imageDigest")
    if not isinstance(image, str) or not image.startswith("ghcr.io/") or not IMAGE_DIGEST.fullmatch(str(digest)):
        raise ValueError("invalid image contract")
    return value


def compose_command() -> list[str]:
    return ["docker", "compose", "--env-file", str(ENV_PATH), "-f", str(COMPOSE_PATH)]


def health_check(port: str) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(45):
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{int(port)}/readyz", headers={"Host": "statuscake.shmoza.net"}
            )
            with opener.open(request, timeout=3) as response:
                ready = json.loads(response.read(4096))
            request = urllib.request.Request(
                f"http://127.0.0.1:{int(port)}/api/v1/public/status", headers={"Host": "statuscake.shmoza.net"}
            )
            with opener.open(request, timeout=3) as response:
                status = json.loads(response.read(MAX_MANIFEST_BYTES))
            if ready.get("status") == "ready" and status.get("schemaVersion") == 2:
                return
        except Exception:
            time.sleep(2)
    raise RuntimeError("status service did not become ready")


def create_backup(version: str) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"before-{version}-{int(time.time())}.zip"
    with backup.open("wb") as output:
        run(compose_command() + ["exec", "-T", "status", "python", "/app/service/backup.py", "backup", "--output", "-"], stdout=output)
    os.chmod(backup, 0o600)
    if not backup.stat().st_size:
        raise RuntimeError("logical backup is empty")
    return backup


def preflight(image_ref: str) -> None:
    if shutil.disk_usage(STATE_DIR).free < 512 * 1024 * 1024:
        raise RuntimeError("less than 512 MiB free for update")
    run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=30)
    run(["docker", "manifest", "inspect", image_ref], timeout=120)
    run(compose_command() + ["config", "--quiet"], timeout=30)


def do_check(version: str, env: dict[str, str]) -> dict[str, Any]:
    manifest = fetch_manifest(env.get("STATUS_RELEASE_BASE_URL", ""), version)
    image_ref = f"{manifest['image']}@{manifest['imageDigest']}"
    preflight(image_ref)
    return {"imageRef": image_ref, "availableVersion": version}


def do_apply(version: str, raw_env: str, env: dict[str, str]) -> dict[str, Any]:
    manifest = fetch_manifest(env.get("STATUS_RELEASE_BASE_URL", ""), version)
    image_ref = f"{manifest['image']}@{manifest['imageDigest']}"
    preflight(image_ref)
    previous_ref = env.get("STATUS_IMAGE_REF", "")
    previous_version = env.get("STATUS_VERSION", "")
    backup = create_backup(version)
    updated = replace_env_values(raw_env, {"STATUS_IMAGE_REF": image_ref, "STATUS_VERSION": version})
    try:
        run(["docker", "pull", image_ref], timeout=600)
        atomic_write(ENV_PATH, updated.encode("utf-8"))
        run(compose_command() + ["up", "-d", "--no-build", "--remove-orphans"], timeout=300)
        health_check(env.get("STATUS_PORT", "18082"))
    except Exception as update_error:
        atomic_write(ENV_PATH, raw_env.encode("utf-8"))
        try:
            if previous_ref:
                run(compose_command() + ["up", "-d", "--no-build", "--remove-orphans"], timeout=300)
                with backup.open("rb") as backup_input:
                    run(
                        compose_command() + ["exec", "-T", "status", "python", "/app/service/backup.py", "restore", "--input", "-"],
                        stdin=backup_input,
                    )
                run(compose_command() + ["restart", "status"], timeout=120)
                health_check(env.get("STATUS_PORT", "18082"))
        except Exception as rollback_error:
            raise RuntimeError(f"update failed and rollback failed: {type(update_error).__name__}; {type(rollback_error).__name__}") from update_error
        raise RuntimeError(f"update failed; rolled back to {previous_version or 'previous version'}") from update_error
    return {"imageRef": image_ref, "installedVersion": version, "backup": str(backup)}


def run_job(job_id: str, action: str, requested_version: str) -> None:
    import fcntl

    job = load_job(job_id)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.touch(mode=0o600, exist_ok=True)
    try:
        with LOCK_PATH.open("r+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("another update job is active") from exc
            job.update({"status": "running", "phase": "preflight"})
            save_job(job)
            version = validate_version(requested_version)
            raw_env, env = read_env()
            if action == "check":
                result = do_check(version, env)
            elif action == "apply":
                job["phase"] = "backup-and-deploy"
                save_job(job)
                result = do_apply(version, raw_env, env)
            else:
                raise ValueError("unsupported action")
            job.update({"status": "succeeded", "phase": "complete", "result": result})
    except Exception as exc:
        job.update({"status": "failed", "phase": "complete", "error": str(exc)[:500]})
    save_job(job)
