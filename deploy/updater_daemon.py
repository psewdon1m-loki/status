from __future__ import annotations

import json
import os
import grp
import socketserver
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler

try:
    from update_worker import run_job
    from updater_common import JOBS_DIR, SOCKET_PATH, load_job, now, save_job, validate_version
except ModuleNotFoundError:
    from deploy.update_worker import run_job
    from deploy.updater_common import JOBS_DIR, SOCKET_PATH, load_job, now, save_job, validate_version

MAX_BODY = 4096


def recover_interrupted_jobs() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    for path in JOBS_DIR.glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            if job.get("status") in {"queued", "running"}:
                job.update({"status": "failed", "phase": "complete", "error": "update controller restarted"})
                save_job(job)
        except (OSError, ValueError, json.JSONDecodeError):
            continue


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status: HTTPStatus, value: object) -> None:
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/v1/jobs":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ValueError
            value = json.loads(self.rfile.read(length))
            action = str(value.get("action"))
            version = validate_version(value.get("version"))
            if action not in {"check", "apply"}:
                raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
            return
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id, "service": "cake-status", "action": action, "requestedVersion": version,
            "status": "queued", "phase": "queued", "createdAt": now(),
        }
        save_job(job)
        threading.Thread(target=run_job, args=(job_id, action, version), daemon=True).start()
        self.send_json(HTTPStatus.ACCEPTED, job)

    def do_GET(self) -> None:
        prefix = "/v1/jobs/"
        if not self.path.startswith(prefix):
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            job = load_job(self.path[len(prefix):])
        except (OSError, ValueError, json.JSONDecodeError):
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        self.send_json(HTTPStatus.OK, job)

    def log_message(self, message: str, *args: object) -> None:
        return


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("updater daemon must run as root")
    recover_interrupted_jobs()
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    with UnixHTTPServer(str(SOCKET_PATH), Handler) as server:
        os.chmod(SOCKET_PATH, 0o660)
        group_name = os.environ.get("CAKE_STATUS_UPDATER_GROUP", "cake-status-admin")
        os.chown(SOCKET_PATH, 0, grp.getgrnam(group_name).gr_gid)
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            SOCKET_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
