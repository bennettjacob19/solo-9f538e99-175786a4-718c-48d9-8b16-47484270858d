"""两个本地应答服务 + 网关分流。

- stable/candidate 各是一个真实的本地 HTTP 服务（独立端口、独立线程），
  页面上的“试请求”由网关按当前已提交的分流比例转发到其中之一。
- 档位回退（active=0）后比例视为 0，新请求只走稳定版，直到重新升档。
"""
from __future__ import annotations

import json
import os
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlrequest

STABLE_VERSION = "stable-v1"
CANDIDATE_VERSION = "candidate-v2"
DEFAULT_STABLE_PORT = 18081
DEFAULT_CANDIDATE_PORT = 18082

_servers: dict[str, ThreadingHTTPServer] = {}
_lock = threading.Lock()


def _make_handler(version: str, label: str):
    class AnswerHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps({
                "service": label,
                "version": version,
                "path": self.path,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # 静默，避免污染页面日志
            return

    return AnswerHandler


def start_answer_services() -> dict:
    """启动两个本地应答服务（进程内只启动一次，测试/重建 app 时复用）。"""
    with _lock:
        if _servers:
            return service_endpoints()
        stable_port = int(os.environ.get("GRAY_STABLE_PORT", DEFAULT_STABLE_PORT))
        candidate_port = int(os.environ.get("GRAY_CANDIDATE_PORT",
                                            DEFAULT_CANDIDATE_PORT))
        stable = ThreadingHTTPServer(("127.0.0.1", stable_port),
                                     _make_handler(STABLE_VERSION, "stable"))
        candidate = ThreadingHTTPServer(("127.0.0.1", candidate_port),
                                        _make_handler(CANDIDATE_VERSION,
                                                      "candidate"))
        threading.Thread(target=stable.serve_forever, daemon=True,
                         name="stable-answer").start()
        threading.Thread(target=candidate.serve_forever, daemon=True,
                         name="candidate-answer").start()
        _servers["stable"] = stable
        _servers["candidate"] = candidate
        return service_endpoints()


def service_endpoints() -> dict:
    if not _servers:
        stable_port = int(os.environ.get("GRAY_STABLE_PORT", DEFAULT_STABLE_PORT))
        candidate_port = int(os.environ.get("GRAY_CANDIDATE_PORT",
                                            DEFAULT_CANDIDATE_PORT))
        return {
            "stable": f"http://127.0.0.1:{stable_port}",
            "candidate": f"http://127.0.0.1:{candidate_port}",
        }
    return {
        "stable": f"http://127.0.0.1:{_servers['stable'].server_address[1]}",
        "candidate": f"http://127.0.0.1:{_servers['candidate'].server_address[1]}",
    }


def choose_target(active: bool, ratio_bp: int | None,
                  rng: random.Random | None = None) -> str:
    """按已提交比例选目标：active=0 时必走 stable。"""
    if not active or not ratio_bp:
        return "stable"
    roll = (rng or random).randint(1, 10000)
    return "candidate" if roll <= ratio_bp else "stable"


def trial_request(active: bool, ratio_bp: int | None,
                  rng: random.Random | None = None) -> dict:
    target = choose_target(active, ratio_bp, rng)
    url = service_endpoints()[target] + "/answer"
    with urlrequest.urlopen(url, timeout=5) as resp:
        payload = json.loads(resp.read().decode())
    payload["target"] = target
    payload["active"] = bool(active)
    payload["applied_ratio_bp"] = ratio_bp if active else 0
    return payload
