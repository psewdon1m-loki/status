from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from service import vless_probe


class VlessProbeTests(unittest.TestCase):
    URI = (
        "vless://11111111-1111-4111-8111-111111111111@example.com:443"
        "?encryption=none&security=reality&sni=cdn.example.com&fp=chrome"
        "&pbk=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA&sid=abcd&type=tcp&flow=xtls-rprx-vision"
    )

    def test_builds_reality_canary_configuration(self):
        config, host, port = vless_probe.build_xray_config(self.URI, 18090)
        outbound = config["outbounds"][0]
        self.assertEqual(("example.com", 443), (host, port))
        self.assertEqual("raw", outbound["streamSettings"]["network"])
        self.assertEqual("reality", outbound["streamSettings"]["security"])
        self.assertEqual("cdn.example.com", outbound["streamSettings"]["realitySettings"]["serverName"])
        self.assertEqual("xtls-rprx-vision", outbound["settings"]["flow"])

    def test_missing_secret_is_unknown_not_outage(self):
        with tempfile.TemporaryDirectory() as directory:
            result = vless_probe.probe_vless_target("example.com", 443, canary_file=str(Path(directory) / "missing.json"))
        self.assertEqual("unknown", result["status"])
        self.assertEqual("not_configured", result["checkStatus"])

    def test_canary_file_has_bounded_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "canaries.json"
            path.write_text(json.dumps({"schemaVersion": 1, "probes": {"example.com:443": {"uri": self.URI}}}), encoding="utf-8")
            loaded = vless_probe.load_canaries(str(path))
        self.assertIn("example.com:443", loaded)


if __name__ == "__main__":
    unittest.main()
