from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from pathlib import Path
from urllib.parse import urlparse

SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
IMAGE = re.compile(r"^[a-z0-9][a-z0-9./_-]*@[sS][hH][aA]256:[0-9a-fA-F]{64}$")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def https_url(name: str, value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        return f"{name} must be an absolute HTTPS URL without credentials or fragment"
    return None


def validate(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.is_file():
        return [f"environment file does not exist: {path}"]
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        errors.append("environment file must have mode 0600")
    values = read_env(path)
    if not SEMVER.fullmatch(values.get("STATUS_VERSION", "")):
        errors.append("STATUS_VERSION must be an exact semantic version")
    if not IMAGE.fullmatch(values.get("STATUS_IMAGE_REF", "")):
        errors.append("STATUS_IMAGE_REF must use image@sha256:<64 hex>")
    for name in ("STATUS_UPSTREAM_URL", "STATUS_HOME_URL"):
        error = https_url(name, values.get(name, ""))
        if error:
            errors.append(error)
    token = values.get("STATUS_ADMIN_TOKEN", "")
    if len(token) < 32 or token.startswith("replace-"):
        errors.append("STATUS_ADMIN_TOKEN must be a non-placeholder value of at least 32 characters")
    hosts = {item.strip().lower() for item in values.get("STATUS_ALLOWED_HOSTS", "").split(",") if item.strip()}
    if "statuscake.shmoza.net" not in hosts:
        errors.append("STATUS_ALLOWED_HOSTS must include statuscake.shmoza.net")
    secret = Path(values.get("STATUS_VLESS_CANARY_FILE_PATH", ""))
    if not secret.is_absolute() or not secret.is_file():
        errors.append("STATUS_VLESS_CANARY_FILE_PATH must name an existing absolute file")
    elif os.name == "posix" and stat.S_IMODE(secret.stat().st_mode) & 0o037:
        errors.append("VLESS canary secret may be group-readable but not group-writable or accessible by others")
    try:
        secret_gid = int(values.get("STATUS_SECRET_GID", ""))
        if secret_gid <= 0:
            raise ValueError
        if secret.is_file() and os.name == "posix" and secret.stat().st_gid != secret_gid:
            errors.append("VLESS canary secret group must match STATUS_SECRET_GID")
    except ValueError:
        errors.append("STATUS_SECRET_GID must be a positive numeric group id")
    try:
        port = int(values.get("STATUS_PORT", "18082"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        errors.append("STATUS_PORT must be between 1 and 65535")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    errors = validate(args.path)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
    print("configuration is valid")


if __name__ == "__main__":
    main()
