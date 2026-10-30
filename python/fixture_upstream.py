#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地上游夹具：模拟官方 AutoClaw 本地 model-proxy。

用途：在不接触真实账号的前提下，端到端验证反代网关的
指纹收敛、节流、冷却状态机、406 回落、内容哨兵检测。

    listen 127.0.0.1:7890

端点（与 relay.py 的 model-proxy 模式对齐）：
    GET  /internal/model-proxy/v1/models
    POST /internal/model-proxy/v1/chat/completions   (支持 stream)

失败注入：在用户消息里放标记，网关原样透传，夹具据此返回：
    [[fixture:rate]]    -> HTTP 429
    [[fixture:balance]] -> HTTP 402
    [[fixture:auth]]    -> HTTP 401
    [[fixture:waf406]]  -> HTTP 406（模拟阿里 WAF Bot 规则）
    [[fixture:flaky]]   -> HTTP 500
    [[fixture:sentinel]]-> HTTP 200 + 正文恰好是内容哨兵串
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SENTINEL = "DANGEROUS_ACTIVITY_BLOCKED"

MODELS = [
    {"id": "openclaw", "object": "model", "owned_by": "fixture"},
    {"id": "glm-5.3-flash", "object": "model", "owned_by": "fixture"},
    {"id": "zai_auto", "object": "model", "owned_by": "fixture"},
]


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FixtureUpstream"
    sys_version = ""

    def log_message(self, fmt, *args):  # noqa: D102
        pass

    def _send(self, status: int, payload: dict, ctype: str = "application/json") -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Server", "nginx")
        self.end_headers()
        self.wfile.write(body)

    def _marker(self, body: bytes) -> str:
        try:
            text = body.decode("utf-8", "replace")
        except Exception:
            return ""
        for m in ("waf406", "flaky", "sentinel", "balance", "auth", "rate"):
            if f"[[fixture:{m}]]" in text:
                return m
        return ""

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path.endswith("/fixture-ping"):
            self._send(200, {"fixture": True, "ts": time.time()})
        elif path.endswith("/v1/models"):
            self._send(200, {"object": "list", "data": MODELS})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        marker = self._marker(body)

        if marker == "rate":
            self._send(429, {"error": {"message": "rate limited", "type": "rate_limit_error"}})
            return
        if marker == "balance":
            self._send(402, {"error": {"message": "insufficient balance", "type": "balance_error"}})
            return
        if marker == "auth":
            self._send(401, {"error": {"message": "Unauthorized", "code": 410000}})
            return
        if marker == "waf406":
            self._send(406, {"error": {"message": "Not Acceptable", "type": "waf_block"}})
            return
        if marker == "flaky":
            self._send(500, {"error": {"message": "internal error"}})
            return

        try:
            req = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            req = {}
        stream = bool(req.get("stream"))

        if marker == "sentinel":
            payload = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": SENTINEL}, "finish_reason": "stop"}],
            }
            self._send(200, payload)
            return

        prompt = ""
        for m in req.get("messages", []) or []:
            if m.get("role") == "user":
                prompt = str(m.get("content") or "")
                break
        reply = f"fixture echo: {prompt[:200]}"

        if not stream:
            payload = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": len(prompt), "completion_tokens": len(reply), "total_tokens": len(prompt) + len(reply)},
            }
            self._send(200, payload)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        pieces = reply.split(" ")
        for i, piece in enumerate(pieces):
            chunk = {
                "id": cid,
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"content": piece + (" " if i < len(pieces) - 1 else "")}, "finish_reason": None}],
            }
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
            time.sleep(0.05)
        done = {"id": cid, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        self.wfile.write(f"data: {json.dumps(done, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class FixtureServer(ThreadingHTTPServer):
    # Windows 的 SO_REUSEADDR 允许同端口二次绑定（Linux 语义不同）：不关掉它，
    # 第二个夹具/真实上游会静默共存，请求随机落到其中一个——影子回显事故的根源。
    allow_reuse_address = False


def main() -> int:
    port = int(os.environ.get("AC_FIXTURE_PORT", "7890"))
    # 绑定前主动探测：端口上已有任何监听者（真实 model-proxy / 另一个夹具）就拒启。
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            print(f"fixture refused to start: 127.0.0.1:{port} already has a listener", file=sys.stderr)
            print("这个端口默认属于真实 model-proxy；夹具占住它会把真实会话回显成 'fixture echo: ...'。", file=sys.stderr)
            print("确认真实上游没在跑再用本夹具，或用 AC_FIXTURE_PORT 换端口。", file=sys.stderr)
            return 2
    finally:
        probe.close()
    try:
        srv = FixtureServer(("127.0.0.1", port), FixtureHandler)
    except OSError as exc:
        print(f"fixture refused to start: 127.0.0.1:{port} is busy ({exc})", file=sys.stderr)
        print("这个端口默认属于真实 model-proxy；夹具占住它会把真实会话回显成 'fixture echo: ...'。", file=sys.stderr)
        print("确认真实上游没在跑再用本夹具，或用 AC_FIXTURE_PORT 换端口。", file=sys.stderr)
        return 2
    srv.daemon_threads = True
    print(f"fixture upstream on http://127.0.0.1:{port} (ping: /internal/model-proxy/fixture-ping)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
