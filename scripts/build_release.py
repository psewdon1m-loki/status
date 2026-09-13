from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
from pathlib import Path

SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
IMAGE = re.compile(r"^(?P<name>[a-z0-9][a-z0-9./_-]*)@(?P<digest>sha256:[0-9a-f]{64})$")
ROOT = Path(__file__).resolve().parents[1]
BUNDLE_FILES = (
    "VERSION",
    ".env.example",
    "README.md",
    "deploy/REVERSE_PROXY.md",
    "deploy/docker-compose.release.yml",
    "deploy/install.sh",
    "deploy/statusctl",
    "deploy/validate_env.py",
    "deploy/updater_common.py",
    "deploy/updater_daemon.py",
    "deploy/updater_client.py",
    "deploy/update_worker.py",
    "deploy/cake-status-updater.service",
    "secrets/status-vless-canaries.example.json",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(version: str, image: str, base_url: str, output: Path) -> tuple[Path, Path]:
    if not SEMVER.fullmatch(version):
        raise ValueError("version must be exact semantic version")
    if (ROOT / "VERSION").read_text(encoding="utf-8").strip() != version:
        raise ValueError("VERSION file does not match the requested release")
    match = IMAGE.fullmatch(image)
    if not match:
        raise ValueError("image must be immutable image@sha256 reference")
    if not base_url.startswith("https://"):
        raise ValueError("base URL must use HTTPS")
    missing = [name for name in BUNDLE_FILES if not (ROOT / name).is_file()]
    if missing:
        raise ValueError(f"missing bundle files: {', '.join(missing)}")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"cake-status-{version}.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
        for name in BUNDLE_FILES:
            bundle.add(ROOT / name, arcname=f"cake-status-{version}/{name}", recursive=False)
    manifest = {
        "schemaVersion": 1,
        "service": "cake-status",
        "version": version,
        "releaseTag": f"status-v{version}",
        "image": match.group("name"),
        "imageDigest": match.group("digest"),
        "archive": {
            "url": f"{base_url.rstrip('/')}/{archive.name}",
            "sha256": digest(archive),
            "size": archive.stat().st_size,
        },
    }
    manifest_path = output / f"status-v{version}-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sums = output / "SHA256SUMS"
    sums.write_text(f"{digest(archive)}  {archive.name}\n{digest(manifest_path)}  {manifest_path.name}\n", encoding="utf-8")
    return archive, manifest_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    build(args.version, args.image, args.base_url, args.output)


if __name__ == "__main__":
    main()
