from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from vless_probe import XRAY_BINARY, build_xray_config

DUMMY_URI = (
    "vless://11111111-1111-4111-8111-111111111111@example.com:443"
    "?encryption=none&security=reality&sni=example.com&fp=chrome"
    "&pbk=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA&sid=abcd&type=tcp&flow=xtls-rprx-vision"
)


def main() -> None:
    config, _, _ = build_xray_config(DUMMY_URI, 18090)
    with tempfile.TemporaryDirectory(prefix="status-selftest-") as directory:
        path = Path(directory) / "xray.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        result = subprocess.run(
            [XRAY_BINARY, "run", "-test", "-c", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    if result.returncode:
        raise SystemExit("Xray rejected the generated canary configuration")
    print("Xray canary configuration is valid")


if __name__ == "__main__":
    main()
