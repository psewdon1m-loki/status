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
                {"key": "connection", "status": "operational"},
                {"key": "specific_connections", "status": "operational", "details": [
                    {"label": "Netherlands", "status": "operational", "endpoint": "secret.example:443"}
                ]},
                {"key": "new_connections", "status": "major_outage"},
                {"key": "subscriptions", "status": "major_outage"},
            ],
        }

    def test_sanitizes_upstream_and_computes_two_summaries(self):
        result = app.sanitize_upstream_payload(self.upstream_payload(), "2026-09-10T12:00:01+00:00")
        self.assertEqual(2, result["schemaVersion"])
        self.assertEqual("partial_outage", result["summaries"]["services"]["status"])
        self.assertEqual("unknown", result["summaries"]["connections"]["status"])
        self.assertNotIn("must-not-survive", str(result))
        self.assertNotIn("secret.example", str(result))

    def test_watcher_outage_does_not_erase_connection_probe(self):
        cached = app.sanitize_upstream_payload(self.upstream_payload(), "2026-09-10T12:00:01+00:00")
        offline = app.offline_payload(cached, "2026-09-10T12:01:00+00:00", "2026-09-10T12:00:01+00:00")
        result = app.apply_target_results(offline, [{
            "label": "Netherlands", "status": "operational", "serviceStatus": "operational",
            "checkStatus": "completed", "message": "Работает штатно", "checkedAt": "2026-09-10T12:01:00+00:00",
        }], "2026-09-10T12:01:00+00:00")
        components = {item["key"]: item for item in result["components"]}
        self.assertFalse(result["upstreamAvailable"])
        self.assertEqual("unknown", result["summaries"]["services"]["status"])
        self.assertEqual("operational", result["summaries"]["connections"]["status"])
        self.assertEqual("operational", components["specific_connections"]["details"][0]["status"])
        self.assertEqual("unknown", components["subscriptions"]["status"])
        self.assertEqual("major_outage", components["subscriptions"]["serviceStatus"])
        self.assertEqual("unavailable", components["subscriptions"]["checkStatus"])

    def test_unconfigured_canaries_are_not_reported_as_failures(self):
        payload = app.offline_payload(None, "2026-09-10T12:01:00+00:00", None)
        result = app.apply_target_results(payload, [{
            "label": "Netherlands", "status": "unknown", "serviceStatus": "unknown",
            "checkStatus": "not_configured", "message": "Проверка не настроена", "checkedAt": "2026-09-10T12:01:00+00:00",
        }], "2026-09-10T12:01:00+00:00")
        component = next(item for item in result["components"] if item["key"] == "specific_connections")
        self.assertEqual("not_configured", component["checkStatus"])
        self.assertEqual("unknown", result["summaries"]["connections"]["status"])

    def test_normalize_targets_deduplicates_endpoints(self):
        targets = app.normalize_targets([
            {"configurationKey": "a" * 64, "label": "Primary", "host": "EXAMPLE.COM", "port": 443},
            {"configurationKey": "b" * 64, "label": "Duplicate", "host": "example.com", "port": "443"},
        ])
        self.assertEqual([{"configurationKey": "a" * 64, "label": "Primary", "host": "example.com", "port": 443}], targets)

    def test_normalize_targets_rejects_unsafe_host(self):
        with self.assertRaisesRegex(ValueError, "invalid_target_endpoint"):
            app.normalize_targets([{"configurationKey": "a" * 64, "label": "Target", "host": "user@example.com", "port": 443}])

    def test_static_page_is_noindex_and_contains_no_connection_secrets(self):
        combined = "\n".join((ROOT / "static" / name).read_text(encoding="utf-8") for name in ("index.html", "app.js", "styles.css"))
        self.assertIn("noindex, nofollow, noarchive", combined)
        self.assertNotIn("fonts.googleapis.com", combined)
        self.assertTrue((ROOT / "static/assets/SpaceGrotesk-Variable.ttf").is_file())
        self.assertNotIn("vless://", combined.lower())
        self.assertNotIn("/sub/", combined.lower())


if __name__ == "__main__":
    unittest.main()
