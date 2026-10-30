#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoClaw 每日签到（幂等）+ 可导入模块。

上游端点：POST {region基址}/autoclaw-proxy/proxy/autoclaw-task-complete
body: {"task_id": "daily_signin"}
服务端幂等：already_completed=true 表示今日已签，重复执行安全。

作为模块导入：
    from checkin import checkin_account, checkin_all, load_state, save_state

CLI 用法：
    python checkin.py --dry-run
    python checkin.py --region cn
    python checkin.py --status
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

REGIONS = {
    "cn": "https://autoglm-acceleration-api.zhipuai.cn",
    "global": "https://autoglm-api.autoglm.ai",
}


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(path: str, blob: dict) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def post(url: str, payload: dict, headers: dict, timeout: float = 30):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def checkin_account(region: str, token: str, base: str | None = None,
                    timeout: float = 30) -> dict:
    """对单个账号执行签到。返回结果字典。"""
    base = base or REGIONS.get(region, REGIONS["cn"])
    url = base.rstrip("/") + "/autoclaw-proxy/proxy/autoclaw-task-complete"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-AutoClaw-Region": region,
        "Cookie": token,
    }
    try:
        status, body = post(url, {"task_id": "daily_signin"}, headers, timeout)
        return {"ok": 200 <= status < 300, "status": status, "body": body[:300], "ts": time.time()}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "body": e.read().decode("utf-8", "replace")[:300], "ts": time.time()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": 0, "body": f"{type(e).__name__}: {e}", "ts": time.time()}


def checkin_all(accounts: list[dict], state_path: str) -> dict:
    """对所有启用账号执行签到。返回 {account_name: result}。"""
    blob = load_state(state_path)
    results = {}
    for acc in accounts:
        if not acc.get("enabled", True):
            continue
        name = acc.get("name", "unknown")
        region = acc.get("region", "cn")
        # token 从 headers 的 cookie 或 authorization 里取
        headers = acc.get("headers", {})
        token = headers.get("cookie") or headers.get("authorization", "")
        if not token:
            results[name] = {"ok": False, "status": 0, "body": "no token", "ts": time.time()}
            continue
        result = checkin_account(region, token)
        results[name] = result
        blob[name] = {
            "last_checkin": int(result["ts"]),
            "http_status": result["status"],
            "ok": result["ok"],
        }
    save_state(state_path, blob)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="AutoClaw 每日签到")
    ap.add_argument("--region", choices=sorted(REGIONS), default="cn")
    ap.add_argument("--base", default=None, help="覆盖 region 基址")
    ap.add_argument("--token", default="", help="会话 token / cookie（也可用 AC_CHECKIN_TOKEN 环境变量）")
    ap.add_argument("--state", default="./data/checkin-state.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    base = args.base or REGIONS[args.region]
    url = base.rstrip("/") + "/autoclaw-proxy/proxy/autoclaw-task-complete"
    token = args.token or os.environ.get("AC_CHECKIN_TOKEN", "")

    if args.status:
        blob = load_state(args.state)
        print(json.dumps(blob, ensure_ascii=False, indent=2))
        return 0

    if args.dry_run:
        print("dry-run: would POST", url)
        print("body:", json.dumps({"task_id": "daily_signin"}, ensure_ascii=False))
        print("headers:", {"Content-Type": "application/json", "X-AutoClaw-Region": args.region})
        return 0

    if not token:
        print("missing token: pass --token or set AC_CHECKIN_TOKEN", file=sys.stderr)
        return 2

    result = checkin_account(args.region, token, base)
    print(f"HTTP {result['status']}: {result['body'][:200]}")
    blob = load_state(args.state)
    blob[args.region] = {"last_checkin": int(result["ts"]), "http_status": result["status"], "ok": result["ok"]}
    save_state(args.state, blob)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
