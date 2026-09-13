from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy import update_worker


class FakeResponse:
    def __init__(self, url: str, value: dict):
        self.url = url
        self.body = json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.url

    def read(self, size: int):
        return self.body[:size]


class UpdaterTests(unittest.TestCase):
    def manifest(self, version="0.0.2"):
        return {
            "schemaVersion": 1,
            "service": "cake-status",
            "version": version,
            "releaseTag": f"status-v{version}",
            "image": "ghcr.io/example/cake-status",
            "imageDigest": "sha256:" + "a" * 64,
        }

    def test_manifest_accepts_exact_status_release_contract(self):
        url = "https://releases.example/status-v0.0.2/status-v0.0.2-manifest.json"
        with patch("urllib.request.urlopen", return_value=FakeResponse(url, self.manifest())):
            result = update_worker.fetch_manifest("https://releases.example", "0.0.2")
        self.assertEqual("status-v0.0.2", result["releaseTag"])

    def test_manifest_base_must_be_https(self):
        with self.assertRaises(ValueError):
            update_worker.manifest_url("http://releases.example", "0.0.2")

    def test_failed_health_gate_restores_previous_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / "status.env"
            backup_path = Path(directory) / "backup.zip"
            backup_path.write_bytes(b"logical-backup")
            raw = "STATUS_VERSION=0.0.1\nSTATUS_IMAGE_REF=ghcr.io/example/cake-status@sha256:" + "b" * 64 + "\n"
            env_path.write_text(raw, encoding="utf-8")
            environment = {"STATUS_VERSION": "0.0.1", "STATUS_IMAGE_REF": "ghcr.io/example/cake-status@sha256:" + "b" * 64, "STATUS_PORT": "18082"}
            health_calls = 0

            def health(_port):
                nonlocal health_calls
                health_calls += 1
                if health_calls == 1:
                    raise RuntimeError("new image unhealthy")

            with (
                patch.object(update_worker, "ENV_PATH", env_path),
                patch.object(update_worker, "fetch_manifest", return_value=self.manifest()),
                patch.object(update_worker, "preflight"),
                patch.object(update_worker, "create_backup", return_value=backup_path),
                patch.object(update_worker, "run"),
                patch.object(update_worker, "health_check", side_effect=health),
            ):
                with self.assertRaisesRegex(RuntimeError, "rolled back"):
                    update_worker.do_apply("0.0.2", raw, environment)
            self.assertEqual(raw, env_path.read_text(encoding="utf-8"))
            self.assertEqual(2, health_calls)


if __name__ == "__main__":
    unittest.main()
