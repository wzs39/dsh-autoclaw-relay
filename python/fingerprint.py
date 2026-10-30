#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""请求指纹一致性层。

目标只有一个：让上游长期看到**同一套**头集合、同一批取值，避免因指纹漂移
（UA 变、头缺失、出现 `X-Request-Model: zai_*` 这类脚本特征头）被识别为异常客户端。

这里做的是"收敛 + 去特征化"，不做任何越权伪装：不伪造他人会话、不冒充别人身份、
不绕过付费校验。
"""
from __future__ import annotations

# RFC 7230 hop-by-hop：每一跳都该剥掉，绝不能透传
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
}

# 官方桌面客户端长期稳定的浏览器特征值（对齐 Chrome 系内核版本）
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 暴露"脚本/代理/中转"身份的头，一律丢弃
# 其中 x-request-model 是实测触发点：2026-09-23 起 zai-proxy 直连因
# `X-Request-Model: zai_*` 命中阿里 WAF 的 Bot 规则，直接 406。
LEAK_HEADERS = {
    "x-request-model",
    "x-client-sign",
    "x-client-version",
    "x-client-id",
    "x-forwarded-for",
    "x-real-ip",
    "x-proxy-id",
    "x-proxy-server",
    "via",
    "forwarded",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-forwarded-port",
    "cf-connecting-ip",
    "cf-ipcountry",
    "cf-ray",
    "true-client-ip",
    "x-request-id",
    "x-trace-id",
    "x-b3-traceid",
    "x-api-key",
    "x-ide-version",
    "x-editor-version",
}

LEAK_PREFIXES = ("x-acs-", "x-b3-", "x-amz-", "x-original-url")

# 由本层统一生成，不接收外部传入（避免被上游做关联）
REGENERATED = {"x-request-id", "x-traceparent", "x-amzn-trace-id"}


def _drop(name: str) -> bool:
    n = name.lower()
    if n in HOP_BY_HOP or n in LEAK_HEADERS:
        return True
    return any(n.startswith(p) for p in LEAK_PREFIXES)


def normalize_headers(incoming, cfg: dict, stream: bool = False) -> dict:
    """把任意来源的请求头收敛成一份稳定、可预期的头集合。

    顺序固定（Python dict 保序），上游看到的键序列长期一致，
    这是降低"指纹漂移"最关键的一点。
    """
    out: dict[str, str] = {}

    # 头设置统一在配置的 headers 段下（兼容旧的顶层写法）
    hdrs = cfg.get("headers") if isinstance(cfg.get("headers"), dict) else cfg

    out["User-Agent"] = hdrs.get("user_agent") or cfg.get("user_agent") or DEFAULT_UA
    out["Accept"] = "text/event-stream" if stream else "application/json"
    out["Accept-Language"] = hdrs.get("accept_language") or cfg.get("accept_language") or "zh-CN,zh;q=0.9,en;q=0.8"
    out["Accept-Encoding"] = "identity"
    out["Content-Type"] = "application/json"
    out["Connection"] = "close"

    origin = hdrs.get("origin") or cfg.get("origin")
    if origin:
        out["Origin"] = origin
        out["Referer"] = hdrs.get("referer") or cfg.get("referer") or (origin.rstrip("/") + "/")

    for k, v in incoming.items():
        if _drop(k):
            continue
        lk = k.lower()
        if lk in (
            "user-agent",
            "accept",
            "accept-language",
            "accept-encoding",
            "content-type",
            "connection",
            "origin",
            "referer",
        ):
            continue
        if lk.startswith("sec-fetch-") or lk.startswith("sec-ch-"):
            continue
        if lk in REGENERATED:
            continue
        # 只保留上游真正需要的一类头：认证/会话/自定义标识
        if lk in ("authorization", "cookie", "x-openclaw-model", "x-autoclaw-region", "region"):
            out[lk] = v

    # 配置里的凭证最后写入并清掉小写变体，避免同键双写（http.client 会重复发）
    cfg_cookie = hdrs.get("cookie") or cfg.get("cookie")
    cfg_auth = hdrs.get("authorization") or cfg.get("authorization")
    if cfg_cookie:
        out.pop("cookie", None)
        out["Cookie"] = cfg_cookie
    if cfg_auth:
        out.pop("authorization", None)
        out["Authorization"] = cfg_auth

    return out


def strip_response_headers(resp_headers) -> dict:
    """剥离响应头里的代理特征，避免把自身身份透传给下游。

    上游可能给 list[tuple]（http.client.getheaders）也可能给 dict，两种都吃。
    """
    out: dict[str, str] = {}
    items = resp_headers.items() if isinstance(resp_headers, dict) else resp_headers
    for k, v in items:
        lk = k.lower()
        if _drop(lk) or lk in ("server", "date"):
            continue
        out[k] = v
    return out


def get_header(resp_headers, name: str, default: str = "") -> str:
    """从 list[tuple] 或 dict 里取单个头，大小写不敏感。"""
    lk = name.lower()
    items = resp_headers.items() if isinstance(resp_headers, dict) else resp_headers
    for k, v in items:
        if k.lower() == lk:
            return v
    return default
