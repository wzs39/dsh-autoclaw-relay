#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端自测：夹具上游 + 反代网关。

用法（两个进程，或用 run.bat / run.sh 一键起）：

    python fixture_upstream.py
    python relay.py -c config.json
    python client_test.py

验证点：
  1. /healthz /status 正常
  2. /v1/models 走内存缓存
  3. 正常对话（非流式 / 流式）
  4. 429 -> 本地冷却 -> 冷却期内直接 503，不打到上游
  5. 402 -> 长冷却
  6. 401 -> 禁用，必须人工 reset
  7. 内容哨兵 -> 命中后停手
"""
from __future__ import annotations

import http.client
import json
import sys
import time
import urllib.request

HOST = "127.0.0.1"
PORT = 8080
KEY = "sk-local-change-me"


def upstream_is_fixture() -> bool:
    """自测安全预检：从本目录 config.json 读上游基址，验明它真是夹具。

    事故背景：夹具默认端口与真实 model-proxy 相同，夹具抢占后真实会话会被
    回显成 'fixture echo: ...'。同理，真实上游在跑时也绝不能把 [[fixture:*]]
    测试提示打过去。验明身份才开跑，否则拒绝。
    """
    try:
        with open("config.json", "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        print("preflight: 读不到本目录 config.json，无法验证上游身份；拒绝自测（请在 relay 目录里跑）", file=sys.stderr)
        return False
    base = str(cfg.get("upstream", {}).get("base", "")).rstrip("/")
    if not base:
        print("preflight: config.json 没有 upstream.base；拒绝自测", file=sys.stderr)
        return False
    try:
        with urllib.request.urlopen(base + "/fixture-ping", timeout=5) as resp:
            body = resp.read()
            return resp.status == 200 and b"fixture" in body
    except Exception as exc:
        print(f"preflight: 上游 {base} 没有应答夹具身份（{exc}）；它可能是真实 model-proxy，拒绝自测", file=sys.stderr)
        return False


def call(method: str, path: str, body=None, stream: bool = False, key: str = KEY):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=60)
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def main() -> int:
    if not upstream_is_fixture():
        print("client_test aborted: 上游身份不是夹具", file=sys.stderr)
        return 3

    ok = True

    status, data = call("GET", "/healthz")
    print("[1] healthz:", status, data.decode("utf-8", "replace")[:120])
    ok &= status == 200

    status, data = call("GET", "/v1/models")
    print("[2] models:", status, data.decode("utf-8", "replace")[:120])
    ok &= status == 200

    status, data = call("POST", "/v1/chat/completions",
                        {"model": "openclaw", "messages": [{"role": "user", "content": "hello"}]})
    print("[3] chat non-stream:", status, data.decode("utf-8", "replace")[:160])
    ok &= status == 200

    status, data = call("POST", "/v1/chat/completions",
                        {"model": "openclaw", "messages": [{"role": "user", "content": "hello stream"}], "stream": True})
    print("[4] chat stream:", status, len(data), "bytes")
    ok &= status == 200 and b"data:" in data

    # 429 -> 冷却
    status, data = call("POST", "/v1/chat/completions",
                        {"model": "openclaw", "messages": [{"role": "user", "content": "[[fixture:rate]]"}]})
    print("[5] rate-limit upstream:", status, data.decode("utf-8", "replace")[:100])
    ok &= status == 429

    time.sleep(1.0)
    status, data = call("POST", "/v1/chat/completions",
                        {"model": "openclaw", "messages": [{"role": "user", "content": "should be blocked"}]})
    print("[6] within cooldown -> local 503:", status, data.decode("utf-8", "replace")[:120])
    ok &= status == 503

    # 冷却结束 -> 恢复（60s 太久，这里直接 reset）
    call("POST", "/api/guard/reset")
    status, data = call("POST", "/v1/chat/completions",
                        {"model": "openclaw", "messages": [{"role": "user", "content": "[[fixture:balance]]"}]})
    print("[7] balance 402:", status, data.decode("utf-8", "replace")[:100])
    ok &= status == 402

    call("POST", "/api/guard/reset")
    status, data = call("POST", "/v1/chat/completions",
                        {"model": "openclaw", "messages": [{"role": "user", "content": "[[fixture:auth]]"}]})
    print("[8] auth 401:", status, data.decode("utf-8", "replace")[:100])
    ok &= status == 401

    status, data = call("POST", "/v1/chat/completions",
                        {"model": "openclaw", "messages": [{"role": "user", "content": "after auth fail"}]})
    print("[9] disabled -> local 503:", status, data.decode("utf-8", "replace")[:120])
    ok &= status == 503

    call("POST", "/api/guard/reset")
    status, data = call("POST", "/v1/chat/completions",
                        {"model": "openclaw", "messages": [{"role": "user", "content": "[[fixture:sentinel]]"}]})
    print("[10] sentinel:", status, data.decode("utf-8", "replace")[:120])
    ok &= status == 200 and b"DANGEROUS_ACTIVITY_BLOCKED" in data

    time.sleep(1.0)
    status, data = call("POST", "/v1/chat/completions",
                        {"model": "openclaw", "messages": [{"role": "user", "content": "after sentinel"}]})
    print("[11] sentinel cooldown -> local 503:", status, data.decode("utf-8", "replace")[:120])
    ok &= status == 503

    status, data = call("GET", "/status")
    print("[12] status:", status, data.decode("utf-8", "replace")[:200])

    print("\nRESULT:", "ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
