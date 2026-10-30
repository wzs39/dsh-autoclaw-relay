#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoClaw 本地登录态 OpenAI 兼容反代网关。

只做一件事：把你**自己账号**在官方 AutoClaw 桌面客户端里已经登录的本地
model-proxy 额度，暴露成标准的 OpenAI 兼容接口，供本地工具调用。

    客户端  ->  relay.py (:8080)  ->  127.0.0.1:<port>/internal/model-proxy/v1  (官方登录态)

设计约束（与降低封禁风险直接相关）：
  1. 指纹收敛   —— 所有请求头收敛成固定集合（见 fingerprint.py）
  2. 节流       —— 同账号请求强制拉开间隔并加抖动（见 guard.py）
  3. 冷却状态机 —— 402/429/连续错误/token 失效分别冷却或禁用，不在风控后再猛打
  4. 内容哨兵   —— 命中 DANGEROUS_ACTIVITY_BLOCKED 时停手，绝不自动重试/换号
  5. 日志脱敏   —— 不落任何 token / cookie / 请求体

不做的事：不破解客户端、不绕过付费校验、不做批量账号运营。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import ssl
import sys
import threading
import time
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fingerprint import get_header, normalize_headers, strip_response_headers  # noqa: E402
from guard import CONTENT_BLOCK_SENTINEL, CooldownGuard  # noqa: E402

LOG = logging.getLogger("autoclaw-relay")

MODES = {
    # 复用官方桌面客户端已登录会话，免 client-sign
    "model-proxy": {
        "base": "http://127.0.0.1:7890/internal/model-proxy",
        "path_prefix": "/v1",
        "path_map": {},
    },
    # CN 沙箱 relay：POST /api/electron/agent/send + GET /api/events(SSE)
    "relay": {
        "base": "https://autoclaw-sandbox.example.invalid",
        "path_prefix": "",
        "path_map": {
            "/v1/chat/completions": "/api/electron/agent/send",
            "/v1/models": "/api/electron/agent/models",
        },
    },
    # 标准 OpenAI 兼容上游
    "openai": {
        "base": "https://api.openai.com",
        "path_prefix": "/v1",
        "path_map": {},
    },
}


class UpstreamError(Exception):
    def __init__(self, kind: str, detail: str, status: int = 0):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.status = status


class RelayState:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.guard = CooldownGuard(cfg.get("guard", {}), cfg.get("state_path"))
        self.lock = threading.Lock()
        self.mode = cfg.get("upstream", {}).get("mode", "model-proxy")
        self.models_cache: dict[str, float] = {}
        self.models_cache_ttl = float(cfg.get("models_cache_ttl_s", 300))
        self.metrics = {
            "requests": 0,
            "ok": 0,
            "blocked_rejections": 0,
            "upstream_429": 0,
            "upstream_402": 0,
            "upstream_401": 0,
            "upstream_406": 0,
            "sentinel_hits": 0,
            "pacing_sleep_ms": 0,
        }

    def mode_spec(self) -> dict:
        up = self.cfg.get("upstream", {})
        mode = self.mode
        spec = dict(MODES.get(mode, MODES["model-proxy"]))
        if up.get("base"):
            spec["base"] = up["base"]
        if up.get("path_prefix") is not None:
            spec["path_prefix"] = up["path_prefix"]
        spec["path_map"] = dict(spec.get("path_map", {}))
        if up.get("path_map"):
            spec["path_map"].update(up["path_map"])
        return spec

    def bump(self, name: str, n: int = 1) -> None:
        with self.lock:
            self.metrics[name] = self.metrics.get(name, 0) + n

    def record(self, key: str, kind: str, detail: str = "") -> None:
        self.guard.record(key, kind, detail)

    def bump_pacing(self, ms: float) -> None:
        with self.lock:
            self.metrics["pacing_sleep_ms"] = round(self.metrics.get("pacing_sleep_ms", 0) + ms, 1)

    def models_fresh(self) -> bool:
        with self.lock:
            ts = self.models_cache.get("__t")
            return ts and (time.time() - ts) < self.models_cache_ttl


def _open_conn(host: str, port: int, scheme: str, timeout: float, verify: bool):
    if scheme == "https":
        if verify:
            ctx = ssl.create_default_context()
        else:
            ctx = ssl._create_unverified_context()
        return HTTPSConnection(host, port, timeout=timeout, context=ctx)
    return HTTPConnection(host, port, timeout=timeout)


def _read_body(handler, limit_mb: float) -> bytes:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return b""
    if length > limit_mb * 1024 * 1024:
        raise UpstreamError("error", "body too large", 413)
    return handler.rfile.read(length)


def _json_default(o):
    return str(o)


class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AutoClawRelay"
    sys_version = ""
    state: RelayState = None  # type: ignore[assignment]

    # ---------- 日志脱敏 ----------
    def log_message(self, fmt, *args):  # noqa: D102
        try:
            msg = fmt % args
        except Exception:
            msg = str(fmt)
        LOG.info("%s %s", self.address_string(), msg[:400])

    def version_string(self) -> str:
        return self.state.cfg.get("server_header") or "AutoClawRelay"

    # ---------- 工具 ----------
    def _drain_body(self) -> None:
        """把未读的请求体排空，保住 keep-alive 连接；超限就显式断连。

        冷却/鉴权失败等早退路径若不排空，残余字节会被复用连接的下一条请求
        当成请求行（实测表现：下一条请求 501，方法名是上一条的 JSON 正文）。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return
        limit = float(self.state.cfg.get("max_body_mb", 32)) * 1024 * 1024
        if length > limit:
            self.close_connection = True
            return
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                self.close_connection = True
                return
            remaining -= len(chunk)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _require_key(self) -> bool:
        key = self.state.cfg.get("gateway_key")
        if not key:
            return True
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer ") and auth[7:] == key:
            return True
        return False

    # ---------- 路由 ----------
    def do_GET(self):  # noqa: N802
        self._route("GET")

    def do_POST(self):  # noqa: N802
        self._route("POST")

    def _route(self, method: str) -> None:
        st = self.state
        with st.lock:
            st.metrics["requests"] += 1

        path = self.path.split("?", 1)[0]

        if path == "/healthz":
            self._send_json(200, {"ok": True, "mode": st.mode, "ts": time.time()})
            return
        if path == "/status":
            self._send_json(200, {"mode": st.mode, "guard": st.guard.status(), "metrics": st.metrics})
            return
        if path == "/metrics":
            self._send_json(200, st.metrics)
            return
        if path == "/api/guard/status":
            self._send_json(200, st.guard.status())
            return
        if path == "/api/guard/reset":
            self._send_json(200, st.guard.reset())
            return

        if not self._require_key():
            self._drain_body()
            self._send_json(401, {"error": {"message": "invalid gateway key", "type": "auth_error"}})
            return

        # 冷却闸门：被风控时直接挡在本地，绝不把请求打到上游
        key = st.cfg.get("account_key") or "default"
        blocked, reason = st.guard.is_blocked(key)
        if blocked:
            with st.lock:
                st.metrics["blocked_rejections"] += 1
            self._drain_body()
            self._send_json(503, {"error": {"message": f"local cooldown: {reason}", "type": "cooldown"}})
            return

        try:
            body = _read_body(self, float(st.cfg.get("max_body_mb", 32)))
        except UpstreamError as e:
            self.close_connection = True  # 413：体没读过，这条连接不能复用
            self._send_json(e.status, {"error": {"message": e.detail, "type": "request_error"}})
            return

        stream = False
        parsed = None
        if body:
            try:
                parsed = json.loads(body)
                stream = bool(parsed.get("stream"))
            except Exception:
                parsed = None
        if path == "/v1/chat/completions" and parsed is None:
            self._send_json(400, {"error": {"message": "invalid JSON body", "type": "bad_request"}})
            return

        # 节流（最便宜的降封手段）
        t0 = time.time()
        st.guard.acquire(key)
        st.bump_pacing((time.time() - t0) * 1000.0)

        # /v1/models 走内存缓存，避免无谓的上游轮询
        if path == "/v1/models":
            if st.models_fresh():
                with st.lock:
                    cached = st.models_cache.get("payload")
                self._send_bytes(200, json.dumps(cached, ensure_ascii=False, default=_json_default).encode("utf-8"))
                return

        try:
            status, resp_headers, reader = self._forward(method, path, body, stream)
        except UpstreamError as e:
            if e.status in (429, 402, 401, 406):
                kind = {429: "rate", 402: "balance", 401: "auth", 406: "error"}[e.status]
                st.record(key, kind, e.detail)
            self._send_json(e.status or 502, {"error": {"message": e.detail, "type": "upstream_error"}})
            return
        except Exception as e:  # noqa: BLE001
            st.record(key, "error", f"upstream transport: {type(e).__name__}")
            self._send_json(502, {"error": {"message": f"upstream transport error: {type(e).__name__}", "type": "upstream_error"}})
            return

        ctype = get_header(resp_headers, "Content-Type", "application/json")
        self.send_response(status)
        for k, v in strip_response_headers(resp_headers).items():
            if k.lower() == "content-type":
                ctype = v
        if ctype.startswith("text/event-stream"):
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                hit = self._stream(reader, key)
            except Exception:  # noqa: BLE001
                hit = False
            finally:
                try:
                    reader.close()
                except Exception:  # noqa: BLE001
                    pass
            if hit:
                # 哨兵命中已在 _stream 内记 content_blocked；这里不能记 ok
                # （guard 的 ok 会清掉 cooldown_until，等于白记）。
                return
            st.record(key, "ok" if status == 200 else "error", f"HTTP {status}")
            return

        data = reader.read()
        try:
            reader.close()
        except Exception:  # noqa: BLE001
            pass
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

        if status == 200:
            st.record(key, "ok")
            self._maybe_cache_models(path, data)
            if self._detect_sentinel(data):
                st.record(key, "content_blocked")
                with st.lock:
                    st.metrics["sentinel_hits"] += 1
                LOG.warning("content sentinel hit for account %s; stopped (no retry, no account switch)", key)
        else:
            kind = {429: "rate", 402: "balance", 401: "auth", 406: "error"}.get(status, "error")
            st.record(key, kind, f"HTTP {status}")
            if status == 429:
                with st.lock:
                    st.metrics["upstream_429"] += 1
            elif status == 402:
                with st.lock:
                    st.metrics["upstream_402"] += 1
            elif status == 401:
                with st.lock:
                    st.metrics["upstream_401"] += 1
            elif status == 406:
                with st.lock:
                    st.metrics["upstream_406"] += 1
                self._maybe_fallback_on_406()

    # ---------- 上游转发 ----------
    def _maybe_fallback_on_406(self) -> None:
        """406 = 上游 WAF Bot 规则（实测 X-Request-Model: zai_* 触发）。

        这不是账号问题，是链路问题：切到 relay 通道重试。仅在配置开启时做。
        """
        st = self.state
        up = st.cfg.get("upstream", {})
        if not up.get("fallback_on_406"):
            return
        if st.mode != up.get("direct_mode", "model-proxy"):
            return
        fallback = up.get("fallback_mode")
        if not fallback:
            return
        LOG.warning("406 WAF block detected -> switching %s -> %s", st.mode, fallback)
        with st.lock:
            st.mode = fallback

    def _maybe_cache_models(self, path: str, data: bytes) -> None:
        st = self.state
        if path != "/v1/models":
            return
        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception:
            return
        with st.lock:
            st.models_cache["payload"] = payload
            st.models_cache["__t"] = time.time()

    def _detect_sentinel(self, data: bytes) -> bool:
        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception:
            return False
        choices = payload.get("choices") or []
        if not choices:
            return False
        msg = choices[0].get("message") or {}
        return str(msg.get("content") or "").strip() == CONTENT_BLOCK_SENTINEL

    def _stream(self, reader, key: str) -> bool:
        """原样透传 SSE 字节，同时扫描内容哨兵。

        响应已声明 Connection: close 且无 Content-Length，客户端按 EOF 结束读取，
        因此这里写裸字节，不做 chunked 编码（否则客户端会把分块长度行当成正文）。

        哨兵检测两条互补路径（上游可能整帧返回哨兵，也可能把哨兵拆进多个
        delta 分片逐帧下发）：
        1. 原始字节连续匹配（跨 8K 分块边界拼接尾块）；
        2. 逐行解析 SSE 帧，累加 choices[0].delta.content，全等比对哨兵。
        命中后记 content_blocked（本地冷却，不重试、不换号），当前流继续透传完
        （adapter 侧已有文本兜底拦截），返回命中标记，调用方据此跳过 ok 记录。
        """
        st = self.state
        sentinel = CONTENT_BLOCK_SENTINEL
        sentinel_b = sentinel.encode("utf-8")
        hit = False
        tail = b""
        line_buf = b""
        content = ""
        while True:
            chunk = reader.read(8192)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()
            if hit:
                continue
            # 路径 1：原始字节连续匹配
            probe = tail + chunk
            tail = probe[-(len(sentinel_b) - 1):] if len(sentinel_b) > 1 else b""
            if sentinel_b in probe:
                hit = True
                st.record(key, "content_blocked", "stream sentinel (raw bytes)")
                with st.lock:
                    st.metrics["sentinel_hits"] = st.metrics.get("sentinel_hits", 0) + 1
                LOG.warning("content sentinel hit in stream for account %s; stopped (no retry, no account switch)", key)
                continue
            # 路径 2：SSE 帧解析（行缓冲，处理跨块截断）
            line_buf += chunk
            while b"\n" in line_buf:
                line, line_buf = line_buf.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    continue
                try:
                    obj = json.loads(payload.decode("utf-8", "replace"))
                except Exception:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = (choices[0].get("delta") or {}).get("content") or ""
                if not delta:
                    continue
                content += delta
                if len(content) > 4096:
                    content = content[-4096:]
                if content.strip() == sentinel:
                    hit = True
                    st.record(key, "content_blocked", "stream sentinel (delta content)")
                    with st.lock:
                        st.metrics["sentinel_hits"] = st.metrics.get("sentinel_hits", 0) + 1
                    LOG.warning("content sentinel hit in stream for account %s; stopped (no retry, no account switch)", key)
                    break
        return hit

    def _forward(self, method: str, path: str, body: bytes, stream: bool):
        st = self.state
        spec = st.mode_spec()
        split = urlsplit(spec["base"])
        host = split.hostname or "127.0.0.1"
        port = split.port or (443 if split.scheme == "https" else 80)
        scheme = split.scheme or "http"

        target = spec["path_prefix"] + spec["path_map"].get(path, path)
        timeout = float(st.cfg.get("upstream_timeout_s", 300))

        headers = normalize_headers(self.headers, st.cfg, stream=stream)
        if stream:
            headers["Accept"] = "text/event-stream"

        conn = _open_conn(host, port, scheme, timeout, bool(st.cfg.get("verify_upstream", False)))
        try:
            conn.request(method, target, body=body or None, headers=headers)
            resp = conn.getresponse()
        except Exception as e:  # noqa: BLE001
            conn.close()
            raise UpstreamError("error", f"upstream connect failed: {type(e).__name__}") from e

        if resp.status >= 400:
            snippet = resp.read(2048).decode("utf-8", "replace")
            conn.close()
            raise UpstreamError(self._classify(resp.status, snippet), snippet[:300], resp.status)

        return resp.status, resp.headers.items(), resp

    @staticmethod
    def _classify(status: int, snippet: str) -> str:
        if status == 429:
            return "rate"
        if status == 402:
            return "balance"
        if status in (401, 403) or "410000" in snippet or "Unauthorized" in snippet:
            return "auth"
        return "error"


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def show_headers(cfg: dict, stream: bool = False) -> None:
    """把将要发给上游的头集合原样打印出来 —— 用于人工核对指纹一致性。"""
    class _H:
        items = [("User-Agent", "some-client/1.0"), ("Accept", "application/json")]

    hdrs = normalize_headers(_H(), cfg, stream=stream)
    print("# 上游将看到的请求头（顺序固定）")
    for k, v in hdrs.items():
        print(f"{k}: {v}")


def main() -> int:
    ap = argparse.ArgumentParser(description="AutoClaw 本地登录态反代网关")
    ap.add_argument("-c", "--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    ap.add_argument("--show-headers", action="store_true", help="打印收敛后的上游请求头")
    ap.add_argument("--show-headers-stream", action="store_true", help="同上，按 SSE 流式场景")
    ap.add_argument("--check", action="store_true", help="只校验配置并打印摘要")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_config(args.config)

    if args.show_headers:
        show_headers(cfg, stream=False)
        return 0
    if args.show_headers_stream:
        show_headers(cfg, stream=True)
        return 0
    if args.check:
        state = RelayState(cfg)
        spec = state.mode_spec()
        print("config OK")
        print("upstream mode :", state.mode)
        print("upstream base :", spec["base"])
        print("path prefix   :", spec["path_prefix"])
        print("listen        :", cfg.get("listen"))
        print("gateway key   :", "enabled" if cfg.get("gateway_key") else "disabled")
        print("account key   :", cfg.get("account_key") or "default")
        print("cooldown rules:", {k: v for k, v in state.guard.rules.items() if k.startswith("cooldown") or k == "error_threshold"})
        return 0

    host, _, port = (cfg.get("listen") or "127.0.0.1:8080").rpartition(":")
    state = RelayState(cfg)
    RelayHandler.state = state
    srv = ThreadingHTTPServer((host or "127.0.0.1", int(port)), RelayHandler)
    srv.daemon_threads = True
    LOG.info("AutoClaw relay listening on %s:%s -> %s%s (mode=%s)", host or "127.0.0.1", port,
             state.mode_spec()["base"], state.mode_spec()["path_prefix"], state.mode)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutting down")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
