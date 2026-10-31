#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoClaw 账号与积分（纯标准库，无第三方依赖）。

三块能力，全部本地读官方桌面客户端已登录的会话，不接触任何他人账号：

1. 凭据读取 —— Electron `safeStorage` 在 Windows 上就是 Chromium os_crypt：
      Local State: os_crypt.encrypted_key = base64("DPAPI" + DPAPI(aes_key))
      account-credentials.enc   = b"v10" + nonce(12) + AES-256-GCM(密文+tag)
   解出的 JSON 里有 accessToken / refreshToken / tokenType。

2. 请求签名 —— 与官方客户端同一个中间件头：
      X-Auth-Sign = md5(f"{appId}&{timestamp}&{appKey}")
      X-Auth-Appid / X-Auth-TimeStamp / X-Product / X-Channel / X-Version / X-Tm

3. 账号与积分 —— 身份网关（默认 = acceleration 域）上的只读接口：
      GET  /userapi/v1/user-info                        账号身份
      GET  /agent-assetmgr/api/v2/wallets?biz_app_id=…   钱包总览（总积分）
      GET  /agent-assetmgr/api/v1/wallet-instances       钱包明细（分桶/到期）
      GET  /agent-assetmgr/api/v1/points/expiring        即将过期积分
      GET  /agent-assetmgr/api/v1/ledgers_std            积分流水

权限边界：这些接口只读；写操作（签到领积分等）不在这里做。
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import glob
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request

# ── 官方客户端常量（从 dist/main.cjs 的 HSe() 与 LDo 取） ────────────────────
APP_ID = "100003"
APP_KEY = "38d2391985e2369a5fb8227d8e6cd5e5"
PRODUCT = "autoclaw"
CLIENT_VERSION = "2.3.0"
CHANNEL = "official"

DEFAULT_BASE = "https://autoglm-acceleration-api.zhipuai.cn"
USER_DATA_DIR = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"), "AutoClaw-official"
)

_CRYPTPROTECT_UI_FORBIDDEN = 0x1


# ── DPAPI ─────────────────────────────────────────────────────────────────────


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def dpapi_unprotect(cipher: bytes) -> bytes:
    """当前用户上下文的 CryptUnprotectData。"""
    in_blob = _blob(cipher)
    out_blob = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError(f"CryptUnprotectData failed: {ctypes.GetLastError()}")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


# ── Chromium os_crypt ─────────────────────────────────────────────────────────


def load_master_key(user_data_dir: str = USER_DATA_DIR) -> bytes:
    """从 Local State 取出并把 DPAPI 保护的 AES-256 主密钥解出来。"""
    state_path = os.path.join(user_data_dir, "Local State")
    with open(state_path, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    encrypted_key = base64.b64decode(state["os_crypt"]["encrypted_key"])
    if encrypted_key[:5] != b"DPAPI":
        raise ValueError(f"unsupported key prefix: {encrypted_key[:5]!r}")
    return dpapi_unprotect(encrypted_key[5:])


def aes_gcm_open(master_key: bytes, blob: bytes) -> bytes:
    """v10 信封：3 字节前缀 + 12 字节 nonce + 密文/标签。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if blob[:3] != b"v10":
        raise ValueError(f"unsupported envelope: {blob[:3]!r}")
    return AESGCM(master_key).decrypt(blob[3:15], blob[15:], None)


def read_credential_json(master_key: bytes, path: str) -> dict:
    with open(path, "rb") as fh:
        return json.loads(aes_gcm_open(master_key, fh.read()).decode("utf-8", "replace"))


# ── 账号发现 ─────────────────────────────────────────────────────────────────


def list_account_dirs(user_data_dir: str = USER_DATA_DIR) -> list[str]:
    root = os.path.join(user_data_dir, "accounts")
    if not os.path.isdir(root):
        return []
    return sorted(
        path for path in glob.glob(os.path.join(root, "*"))
        if os.path.isdir(path)
        and os.path.exists(os.path.join(path, "account-credentials.enc"))
    )


def load_accounts(user_data_dir: str = USER_DATA_DIR) -> list[dict]:
    """把本机所有已登录账号读成 [{account_key, profile, credential}]。

    credential 里含 accessToken —— 调用方不得写日志、不得回响应。
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    except Exception:  # noqa: BLE001
        return []
    try:
        master_key = load_master_key(user_data_dir)
    except Exception:  # noqa: BLE001
        return []
    accounts = []
    for account_dir in list_account_dirs(user_data_dir):
        entry = {"account_key": os.path.basename(account_dir), "dir": account_dir}
        try:
            entry["credential"] = read_credential_json(
                master_key, os.path.join(account_dir, "account-credentials.enc")
            )
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {exc}"
            accounts.append(entry)
            continue
        profile_path = os.path.join(account_dir, "account-profile.json")
        if os.path.exists(profile_path):
            try:
                with open(profile_path, "r", encoding="utf-8") as fh:
                    entry["profile"] = json.load(fh)
            except Exception:  # noqa: BLE001
                pass
        accounts.append(entry)
    return accounts


def bearer_of(credential: dict) -> str:
    token = str((credential or {}).get("accessToken") or "").strip()
    if token == "":
        return ""
    return token if token.lower().startswith("bearer ") else f"Bearer {token}"


# ── 签名与请求 ───────────────────────────────────────────────────────────────


def signed_headers(token: str, now: float | None = None) -> dict:
    """官方中间件头。timestamp 必须是秒。"""
    ts = str(int(now if now is not None else time.time()))
    return {
        "X-Version": CLIENT_VERSION,
        "X-Tm": "win",
        "X-Product": PRODUCT,
        "X-Auth-Appid": APP_ID,
        "X-Auth-TimeStamp": ts,
        "X-Auth-Sign": hashlib.md5(f"{APP_ID}&{ts}&{APP_KEY}".encode()).hexdigest(),
        "X-Lang": "zh-CN",
        "X-Channel": CHANNEL,
        "Authorization": token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _get(base: str, path: str, query: dict, token: str, timeout: float = 20.0) -> dict:
    url = base.rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, headers=signed_headers(token), method="GET")
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# ── 业务读取 ─────────────────────────────────────────────────────────────────


def fetch_identity(token: str, base: str = DEFAULT_BASE, device_id: str = "") -> dict:
    """身份接口是 POST（客户端里写的是 method:"POST"），GET 会 404。"""
    payload = {"source_id": PRODUCT, "device_id": device_id}
    url = base.rstrip("/") + "/userapi/v1/user-info"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=signed_headers(token),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20.0, context=ssl.create_default_context()) as resp:
        body = json.loads(resp.read().decode("utf-8", "replace"))
    if body.get("code") not in (0, None):
        return {}
    return body.get("data") or {}


def fetch_wallets(token: str, base: str = DEFAULT_BASE) -> dict:
    body = _get(base, "/agent-assetmgr/api/v2/wallets", {"biz_app_id": PRODUCT}, token)
    data = body.get("data") or {}
    wallets = [
        {
            "type": w.get("public_wallet_type"),
            "name": w.get("display_name"),
            "balance": w.get("balance", 0),
            "view": w.get("balance_view"),
            "display": w.get("display", False),
        }
        for w in (data.get("wallets") or [])
        if isinstance(w, dict)
    ]
    return {"total": data.get("total_balance", 0), "wallets": wallets}


def fetch_expiring(token: str, base: str = DEFAULT_BASE) -> dict:
    body = _get(base, "/agent-assetmgr/api/v1/points/expiring", {"biz_app_id": PRODUCT}, token)
    data = body.get("data") or {}
    return {
        "points": data.get("expiring_points", 0),
        "total": data.get("total_points", 0),
        "windowValue": data.get("expire_time_value"),
        "windowUnit": data.get("expire_time_type"),
    }


def snapshot(user_data_dir: str = USER_DATA_DIR, base: str = DEFAULT_BASE) -> dict:
    """一次性汇总本机所有账号的账号信息与积分余额。

    返回结构稳定，供 relay /api/account、/api/credits 与插件直接透出。
    任何异常都收成 error 字段，绝不抛到调用方（展示层不该因为一个账号挂掉）。
    """
    accounts = load_accounts(user_data_dir)
    out = []
    for entry in accounts:
        item = {"account": entry["account_key"], "ok": False}
        profile = entry.get("profile") or {}
        inner = profile.get("profile") if isinstance(profile.get("profile"), dict) else profile
        if isinstance(inner, dict):
            if inner.get("accountId"):
                item["uid"] = inner["accountId"]
            if inner.get("phone"):
                item["phone"] = inner["phone"]
            if inner.get("email"):
                item["email"] = inner["email"]
            if inner.get("displayName"):
                item["displayName"] = inner["displayName"]
            if inner.get("numericUserId") is not None:
                item["numericUserId"] = inner["numericUserId"]
            if inner.get("accountType"):
                item["accountType"] = inner["accountType"]
        if entry.get("error"):
            item["error"] = entry["error"]
            out.append(item)
            continue
        token = bearer_of(entry.get("credential") or {})
        if token == "":
            item["error"] = "no access token"
            out.append(item)
            continue
        try:
            identity = fetch_identity(token, base)
            wallets = fetch_wallets(token, base)
            expiring = fetch_expiring(token, base)
            item.update(
                ok=True,
                total=wallets["total"],
                wallets=wallets["wallets"],
                expiring=expiring,
                checkedIn=identity.get("daily_check_in_completed_on_request"),
            )
            if identity.get("uid"):
                item["uid"] = identity["uid"]
        except Exception as exc:  # noqa: BLE001
            item["error"] = f"{type(exc).__name__}: {exc}"
        out.append(item)
    total = sum(a.get("total") or 0 for a in out if a.get("ok"))
    return {"checkedAt": int(time.time()), "total": total, "accounts": out}


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    base = DEFAULT_BASE
    if "--base" in argv:
        base = argv[argv.index("--base") + 1]
    data = snapshot(base=base)
    if "--json" in argv:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print(f"总积分: {data['total']}")
    for acc in data["accounts"]:
        label = acc.get("phone") or acc.get("email") or acc.get("displayName") or (acc.get("uid") or acc["account"])[:12]
        if not acc.get("ok"):
            print(f"  [{label}] 读取失败: {acc.get('error')}")
            continue
        exp = acc.get("expiring") or {}
        print(
            f"  [{label}] 积分 {acc['total']}"
            + (f"，{exp.get('windowValue')}{exp.get('windowUnit') or ''}内到期 {exp.get('points')}" if exp.get("points") else "")
            + f"，今日签到 {'已' if acc.get('checkedIn') else '未'}完成"
        )
        for w in acc.get("wallets") or []:
            if w.get("display"):
                print(f"      {w['name']}: {w['balance']} ({w['type']})")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
