from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from service import backup


class BackupTests(unittest.TestCase):
    def test_round_trip_excludes_secrets_and_verifies_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            targets = root / "targets.json"
            cache = root / "cache.json"
            backup.TARGETS_PATH, backup.CACHE_PATH = targets, cache
            targets.write_text(json.dumps([{
                "configurationKey": "a" * 64, "label": "NL", "host": "nl.example", "port": 443,
            }]), encoding="utf-8")
            cache.write_text(json.dumps({"payload": {"components": []}, "lastSuccessAt": None}), encoding="utf-8")
            archive = backup.create_backup()
            targets.write_text("[]", encoding="utf-8")
            backup.restore_backup(archive)
            self.assertEqual("NL", json.loads(targets.read_text(encoding="utf-8"))[0]["label"])
            with zipfile.ZipFile(BytesIO(archive)) as value:
                self.assertNotIn("secret", " ".join(value.namelist()).lower())
                self.assertIn("VLESS canary secrets", value.read("README.txt").decode("utf-8"))

    def test_rejects_unexpected_zip_member(self):
        output = BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("../../escape", b"bad")
        with self.assertRaises(ValueError):
            backup.restore_backup(output.getvalue())


if __name__ == "__main__":
    unittest.main()
