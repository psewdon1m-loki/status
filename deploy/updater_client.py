from __future__ import annotations

import argparse
import http.client
import json
import socket
from typing import Any

try:
    from updater_common import SOCKET_PATH, validate_version
except ModuleNotFoundError:
    from deploy.updater_common import SOCKET_PATH, validate_version


class UnixConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(SOCKET_PATH))


def request(method: str, path: str, value: Any | None = None) -> dict[str, Any]:
    connection = UnixConnection("localhost", timeout=10)
    body = json.dumps(value, separators=(",", ":")) if value is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    payload = json.loads(response.read())
    if response.status >= 400:
        raise SystemExit(f"updater error: {payload.get('error', response.status)}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "apply"):
        command = subparsers.add_parser(name)
        command.add_argument("version")
    status = subparsers.add_parser("status")
    status.add_argument("job_id")
    args = parser.parse_args()
    if args.command == "status":
        result = request("GET", f"/v1/jobs/{args.job_id}")
    else:
        try:
            version = validate_version(args.version)
        except ValueError as exc:
            parser.error(str(exc))
        result = request("POST", "/v1/jobs", {"action": args.command, "version": version})
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
