#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多账号管理：加载、增删、切换、健康检查、故障转移。

每个账号有独立的 headers（cookie/authorization）、region、enabled 状态。
relay 按 account_key 路由到对应账号；主账号冷却时可故障转移到下一个可用账号。

账号状态持久化在 guard 的状态文件里（cooldown/disabled/fail_streak），
账号配置（headers/region/enabled）持久化在 accounts 状态文件里。
"""
from __future__ import annotations

import json
import os
import threading
import time

REGIONS = {
    "cn": "https://autoglm-acceleration-api.zhipuai.cn",
    "global": "https://autoglm-api.autoglm.ai",
}

DEFAULT_HEADERS = {
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "accept_language": "zh-CN,zh;q=0.9,en;q=0.8",
    "origin": "https://autoclaw.z.ai",
    "referer": "https://autoclaw.z.ai/",
}


class AccountManager:
    def __init__(self, cfg: dict):
        self._lock = threading.Lock()
        self._accounts: dict[str, dict] = {}
        self._state_path = cfg.get("accounts_state_path") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "data", "accounts.json"
        )
        self._load()

        # 从配置注入
        for acc in cfg.get("accounts", []):
            self.add(
                acc.get("name", "default"),
                headers=acc.get("headers", {}),
                region=acc.get("region", "cn"),
                enabled=acc.get("enabled", True),
                priority=acc.get("priority", 0),
            )

        # 兼容旧配置：单账号模式
        if not self._accounts:
            headers = cfg.get("headers", {})
            self.add(
                "default",
                headers=headers,
                region=cfg.get("region", "cn"),
                enabled=True,
                priority=0,
            )

    # ---------- 持久化 ----------
    def _load(self) -> None:
        try:
            with open(self._state_path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
            self._accounts = {k: {**v} for k, v in blob.get("accounts", {}).items()}
        except Exception:
            self._accounts = {}

    def _save(self) -> None:
        try:
            parent = os.path.dirname(os.path.abspath(self._state_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(
                    {"updated_at": time.time(), "accounts": self._accounts},
                    fh, ensure_ascii=False, indent=2,
                )
            os.replace(tmp, self._state_path)
        except Exception:
            pass

    # ---------- 增删改查 ----------
    def add(self, name: str, headers: dict | None = None, region: str = "cn",
            enabled: bool = True, priority: int = 0) -> dict:
        with self._lock:
            merged = dict(DEFAULT_HEADERS)
            if headers:
                merged.update({k: v for k, v in headers.items() if v})
            self._accounts[name] = {
                "name": name,
                "headers": merged,
                "region": region,
                "enabled": enabled,
                "priority": priority,
                "created_at": self._accounts.get(name, {}).get("created_at", time.time()),
            }
            self._save()
            return dict(self._accounts[name])

    def remove(self, name: str) -> bool:
        with self._lock:
            if name not in self._accounts:
                return False
            del self._accounts[name]
            self._save()
            return True

    def get(self, name: str) -> dict | None:
        with self._lock:
            acc = self._accounts.get(name)
            return dict(acc) if acc else None

    def set_enabled(self, name: str, enabled: bool) -> bool:
        with self._lock:
            acc = self._accounts.get(name)
            if not acc:
                return False
            acc["enabled"] = enabled
            self._save()
            return True

    def set_region(self, name: str, region: str) -> bool:
        if region not in REGIONS:
            return False
        with self._lock:
            acc = self._accounts.get(name)
            if not acc:
                return False
            acc["region"] = region
            self._save()
            return True

    def list_accounts(self) -> list[dict]:
        with self._lock:
            return [dict(v) for v in self._accounts.values()]

    def names(self) -> list[str]:
        with self._lock:
            return list(self._accounts.keys())

    def enabled_names(self) -> list[str]:
        with self._lock:
            return [k for k, v in self._accounts.items() if v.get("enabled", True)]

    def resolve(self, key: str | None) -> dict | None:
        """按 key 取账号；key 为 None/default 时返回优先级最高的可用账号。"""
        with self._lock:
            if key and key in self._accounts:
                return dict(self._accounts[key])
            # 取 enabled 且 priority 最高的
            candidates = [v for v in self._accounts.values() if v.get("enabled", True)]
            if not candidates:
                return None
            candidates.sort(key=lambda v: v.get("priority", 0), reverse=True)
            return dict(candidates[0])

    def region_base(self, region: str) -> str:
        return REGIONS.get(region, REGIONS["cn"])
