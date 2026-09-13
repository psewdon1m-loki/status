from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from deploy import validate_env
from scripts import build_release


class ReleaseAndEnvironmentTests(unittest.TestCase):
    def test_release_contract_uses_status_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, manifest = build_release.build(
                "0.0.1", "ghcr.io/example/cake-status@sha256:" + "a" * 64,
                "https://github.com/example/status/releases/download/status-v0.0.1", Path(directory),
            )
            self.assertTrue(archive.is_file())
            self.assertIn('"releaseTag": "status-v0.0.1"', manifest.read_text(encoding="utf-8"))

    def test_validator_accepts_separate_https_upstream(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / "canaries.json"
            secret.write_text('{"schemaVersion":1,"probes":{}}', encoding="utf-8")
            env = root / "status.env"
            env.write_text("\n".join([
                "STATUS_VERSION=0.0.1",
                "STATUS_IMAGE_REF=ghcr.io/example/cake-status@sha256:" + "a" * 64,
                "STATUS_UPSTREAM_URL=https://watcher.example.net/api/v1/public/status",
                "STATUS_HOME_URL=https://cake.example.net/initialize/",
                "STATUS_ADMIN_TOKEN=" + "b" * 64,
                "STATUS_ALLOWED_HOSTS=statuscake.shmoza.net,localhost",
                f"STATUS_VLESS_CANARY_FILE_PATH={secret.resolve()}",
                "STATUS_SECRET_GID=100",
                "STATUS_PORT=18082",
            ]) + "\n", encoding="utf-8")
            self.assertEqual([], validate_env.validate(env))


if __name__ == "__main__":
    unittest.main()
