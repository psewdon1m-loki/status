from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("status_app", ROOT / "service" / "app.py")
assert SPEC and SPEC.loader
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class StatusServiceTests(unittest.TestCase):
    def upstream_payload(self):
        return {
            "schemaVersion": 1,
            "overallStatus": "operational",
            "checkedAt": "2026-09-10T12:00:00+00:00",
            "secret": "must-not-survive",
            "components": [
                {"key": "connection", "title": "Подключение к Loki", "status": "operational"},
                {
                    "key": "specific_connections",
                    "title": "Работоспособность конкретных подключений",
                    "status": "operational",
                    "details": [{"label": "Netherlands", "status": "operational", "endpoint": "secret.example:443"}],
                },
                {"key": "new_connections", "title": "Создание новых подключений", "status": "major_outage"},
                {"key": "subscriptions", "title": "Получение подписок", "status": "major_outage"},
            ],
        }

    def test_sanitizes_upstream_and_recomputes_partial_outage(self):
        result = app.sanitize_upstream_payload(self.upstream_payload(), "2026-09-10T12:00:01+00:00")
        self.assertEqual("partial_outage", result["overallStatus"])
        serialized = str(result)
        self.assertNotIn("must-not-survive", serialized)
        self.assertNotIn("secret.example", serialized)

    def test_offline_snapshot_marks_watcher_down_and_targets_unknown(self):
        cached = app.sanitize_upstream_payload(self.upstream_payload(), "2026-09-10T12:00:01+00:00")
        result = app.offline_payload(cached, "2026-09-10T12:01:00+00:00", "2026-09-10T12:00:01+00:00")
        components = {item["key"]: item for item in result["components"]}
        self.assertEqual("major_outage", result["overallStatus"])
        self.assertFalse(result["upstreamAvailable"])
        self.assertEqual("major_outage", components["connection"]["status"])
        self.assertEqual("unknown", components["specific_connections"]["status"])
        self.assertEqual("unknown", components["specific_connections"]["details"][0]["status"])

    def test_static_page_contains_no_connection_secrets(self):
        combined = "\n".join((ROOT / "static" / name).read_text(encoding="utf-8") for name in ("index.html", "app.js", "styles.css"))
        self.assertNotIn("vless://", combined.lower())
        self.assertNotIn("/sub/", combined.lower())


if __name__ == "__main__":
    unittest.main()
