#!/usr/bin/env python3
"""solver.py -- 路线 C 求解服务（容器内）。

给 VM102 的闲鱼超级管家容器提供「拿 x5sec」的 HTTP 接口：
    POST /solve   {"account": "<cookie_id>", "cookie": "<账号 cookie 串>",
                   "timeout_s": 180, "verify": false}
      -> 200 {"ok": true,  "reason": "pass", "x5sec": "...", "x5_cookies": {...},
              "elapsed_s": 41.2, "moves": 245, "slide_code": 0, "verify": {...},
              "preflight": {...}}
      -> 200 {"ok": false, "reason": "preflight|no_challenge|slide_reject|api_fail|bad_input|error",
              "detail": "...", "elapsed_s": ..., "preflight": {...}}
         （reason="preflight" = 六项体检没过，detail 是卡住的那一步；未发起任何拖动）
      -> 401 缺/错 token；503 后端不可用或忙
    GET  /health  -> 前置体检结论（T3 六项：driver/lock/chrome/cdp/window/page），
                     响应体含 `preflight`；六项全通过才 200，否则 503
    GET  /status  -> 服务统计（累计调用/成功/最近一次）

鉴权：请求头 `X-RouteC-Token` 必须等于 token 文件内容（默认 /opt/routec/token）。
红线：本服务**不记录** cookie 与 x5sec 的任何片段，日志只写账号 id、结果、耗时。
"""
import argparse
import hmac
import json
import os
import pathlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import routec_core

HERE = pathlib.Path(__file__).resolve().parent
LOG_FILE = pathlib.Path(os.environ.get("ROUTEC_LOG", "/opt/routec/logs/solver.log"))

_state = {
    "started_at": time.time(),
    "calls": 0,
    "ok": 0,
    "fail": 0,
    "reasons": {},
    "last": None,
}
_lock = threading.Lock()          # 单飞：一个浏览器只能跑一轮
_busy = threading.Lock()          # 保护 _state


def log(line: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    msg = f"[{ts}] {line}"
    print(msg, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def read_token(path: str) -> str:
    try:
        return pathlib.Path(path).read_text().strip()
    except Exception:
        return ""


class Handler(BaseHTTPRequestHandler):
    server_version = "routec-solver/1.0"
    protocol_version = "HTTP/1.1"

    # 默认 BaseHTTPRequestHandler 会把请求行写进 stderr；保留但去掉查询串
    def log_message(self, fmt, *args):
        return

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        want = self.server.token
        if not want:
            return True
        got = self.headers.get("X-RouteC-Token", "")
        return hmac.compare_digest(got, want)

    def do_GET(self):
        if self.path.startswith("/health"):
            if not self._authed():
                return self._send(401, {"ok": False, "error": "unauthorized"})
            info = routec_core.check_backend()
            return self._send(200 if info.get("ok") else 503, info)
        if self.path.startswith("/status"):
            if not self._authed():
                return self._send(401, {"ok": False, "error": "unauthorized"})
            with _busy:
                return self._send(200, dict(_state))
        return self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/solve"):
            return self._send(404, {"ok": False, "error": "not found"})
        if not self._authed():
            log("solve 拒绝：token 不匹配")
            return self._send(401, {"ok": False, "error": "unauthorized"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send(400, {"ok": False, "error": f"bad json: {type(e).__name__}"})

        account = str(payload.get("account") or "unknown")[:64]
        cookie = payload.get("cookie") or ""
        timeout_s = float(payload.get("timeout_s") or 180)
        verify = bool(payload.get("verify"))

        if not _lock.acquire(timeout=5):
            log(f"solve 拒绝：忙（account={account}）")
            return self._send(503, {"ok": False, "reason": "busy",
                                    "detail": "另一轮正在执行"})
        try:
            with _busy:
                _state["calls"] += 1
            log(f"solve 开始 account={account} verify={verify} timeout={timeout_s:.0f}s")
            res = routec_core.run_round(cookie, account, timeout_s=timeout_s, verify=verify)
        finally:
            _lock.release()

        brief = {
            "account": account,
            "ok": res.get("ok"),
            "reason": res.get("reason"),
            "elapsed_s": res.get("elapsed_s"),
            "challenge": res.get("challenge"),
            "moves": res.get("moves"),
            "slide_code": res.get("slide_code"),
            "x5sec_present": bool(res.get("x5sec")),
            "verify": res.get("verify"),
            "detail": res.get("detail") or "",
            "preflight": res.get("preflight"),
        }
        with _busy:
            _state["last"] = brief
            if res.get("ok"):
                _state["ok"] += 1
            else:
                _state["fail"] += 1
            r = res.get("reason") or "unknown"
            _state["reasons"][r] = _state["reasons"].get(r, 0) + 1
        log("solve 结束 " + json.dumps(brief, ensure_ascii=False))
        return self._send(200, res)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--token-file", default="/opt/routec/token")
    a = ap.parse_args()

    token = read_token(a.token_file)
    if not token:
        log("警告：未读到 token 文件，服务将不鉴权（仅建议本地调试）")

    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    srv.daemon_threads = True
    srv.token = token
    log(f"routec-solver 启动 bind={a.bind}:{a.port} token={'on' if token else 'off'}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
