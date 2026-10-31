#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账号冷却状态机 + 请求节流。

规则对齐公开实现（autoclaw2api 的账号池状态机）的实测表现：

    HTTP 402 / 余额不足      -> 长冷却（默认 12h）
    HTTP 429                 -> 短冷却（默认 60s）
    连续错误达阈值（默认 3）  -> 中冷却（默认 10m）
    401 / 410000 / Unauthorized -> 禁用，需人工重登

核心目的：**不要在账号已经被打风控之后继续猛打**。猛打只会把一次普通失败
放大成"异常活动"，这是封禁风险里最容易自控的一项。

本模块不做任何绕过：冷却期内直接拒绝发请求，不换号、不重试绕过。
"""
from __future__ import annotations

import json
import os
import random
import threading
import time

# 上游内容风控哨兵：HTTP 200 + finish_reason=stop，正文恰好等于该串。
# 语义层判定，换账号/换区域/换模型/改头都不会改变结果，因此绝不自动重试。
CONTENT_BLOCK_SENTINEL = "DANGEROUS_ACTIVITY_BLOCKED"

DEFAULT_RULES = {
    "cooldown_balance_exhausted": 12 * 3600,   # 402 / 余额不足
    "cooldown_rate_limited": 60,                # 429
    "cooldown_repeated_errors": 600,            # 连续错误
    "error_threshold": 3,                       # 连续错误多少次进入中冷却
    "cooldown_content_blocked": 900,            # 命中内容哨兵后的停手时间
    "cooldown_auth_failed": None,               # None = 永久禁用，直到人工 reset
    "min_interval_ms": 1500,                    # 同账号两次请求最小间隔
    "max_interval_ms": 6000,                    # 节流上限（加抖动，模拟人）
    "daily_budget": 0,                          # >0 = 每账号每日请求上限；0 = 不限制
    "quiet_hours": "0-7",                       # 夜间安静时段（本地小时，如 "0-7"）
    "quiet_hours_factor": 4.0,                  # 安静时段间隔放大倍数；0 = 不启用
}


def _now() -> float:
    return time.time()


class CooldownGuard:
    def __init__(self, rules: dict | None = None, state_path: str | None = None):
        self.rules = dict(DEFAULT_RULES)
        if rules:
            self.rules.update(rules)
        self._lock = threading.Lock()
        self._state: dict[str, dict] = {}
        self._last_request_ts: dict[str, float] = {}
        self._save_error = ""
        self.state_path = state_path
        if state_path and os.path.exists(state_path):
            self._load()

    # ---------- 持久化（原子写，避免写坏状态文件） ----------
    def _load(self) -> None:
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
            self._state = blob.get("accounts", {})
        except Exception:
            self._state = {}

    def _save(self) -> None:
        """原子落盘。写失败必须可观测 —— 静默吞异常正是历史上熔断失效的根因。"""
        if not self.state_path:
            return
        try:
            parent = os.path.dirname(os.path.abspath(self.state_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"updated_at": _now(), "accounts": self._state}, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)
            self._save_error = ""
        except Exception as exc:  # noqa: BLE001
            # 不抛（不能因为状态文件写不了就拒服务），但一定要留下痕迹，
            # 否则"重启即失忆"会再次悄悄发生。
            self._save_error = f"{type(exc).__name__}: {exc}"
            try:
                import sys

                print(f"[guard] state write failed: {self._save_error}", file=sys.stderr, flush=True)
            except Exception:  # noqa: BLE001
                pass

    # ---------- 内部 ----------
    def _entry(self, key: str) -> dict:
        return self._state.setdefault(
            key,
            {
                "cooldown_until": 0.0,
                "fail_streak": 0,
                "disabled": False,
                "disable_kind": "",
                "content_blocked_until": 0.0,
                "last_error": "",
                "ok": 0,
                "err": 0,
                "total": 0,
                "last_ok_ts": 0.0,
                "day": "",
                "day_total": 0,
                "transport_err": 0,
            },
        )

    # ---------- 对外 ----------
    def status(self) -> dict:
        with self._lock:
            out = {}
            now = _now()
            for key, e in self._state.items():
                remaining = max(0.0, e["cooldown_until"] - now)
                blocked_remaining = max(0.0, e["content_blocked_until"] - now)
                out[key] = {
                    "cooldown_remaining_s": round(remaining, 1),
                    "content_blocked_remaining_s": round(blocked_remaining, 1),
                    "disabled": bool(e["disabled"]),
                    "disable_kind": e.get("disable_kind", ""),
                    "fail_streak": e["fail_streak"],
                    "last_error": e["last_error"],
                    "ok": e["ok"],
                    "err": e["err"],
                    "total": e["total"],
                    "day_total": e.get("day_total", 0),
                    "transport_err": e.get("transport_err", 0),
                }
            return {
                "accounts": out,
                "rules": {
                    "daily_budget": self.rules.get("daily_budget", 0),
                    "quiet_hours": self.rules.get("quiet_hours", ""),
                    "quiet_hours_factor": self.rules.get("quiet_hours_factor", 1),
                    "min_interval_ms": self.rules.get("min_interval_ms", 0),
                    "max_interval_ms": self.rules.get("max_interval_ms", 0),
                },
                "quiet_now": self._quiet_factor() > 1,
                "state_writable": self._save_error == "",
                "state_error": self._save_error,
            }

    def is_blocked(self, key: str) -> tuple[bool, str]:
        with self._lock:
            e = self._entry(key)
            if e["disabled"]:
                return True, "auth_failed_disabled"
            now = _now()
            if e["cooldown_until"] > now:
                return True, "cooldown"
            if e["content_blocked_until"] > now:
                return True, "content_blocked"
            return False, ""

    def reset(self, key: str | None = None) -> dict:
        """解除冷却。

        **auth 类禁用不在这里解**：凭据失效在人工重新登录前不会自愈，
        顺手 reset 会把"已被拒绝还在敲门"的循环又打开。要解 auth 用
        `auth_recovered()`，语义是"我重新登录了、换了新凭据"。
        """
        with self._lock:
            if key is None:
                kept = []
                for k in self._state:
                    if self._state[k].get("disabled") and self._state[k].get("disable_kind") == "auth":
                        kept.append(k)
                        continue
                    self._state[k] = {
                        "cooldown_until": 0.0,
                        "fail_streak": 0,
                        "disabled": False,
                        "disable_kind": "",
                        "content_blocked_until": 0.0,
                        "last_error": "",
                        "ok": self._state[k].get("ok", 0),
                        "err": self._state[k].get("err", 0),
                        "total": self._state[k].get("total", 0),
                        "last_ok_ts": self._state[k].get("last_ok_ts", 0.0),
                    }
                self._save()
                return {"reset": "all", "accounts": list(self._state.keys()), "auth_disabled_kept": kept}
            e = self._entry(key)
            if e.get("disabled") and e.get("disable_kind") == "auth":
                return {
                    "reset": key,
                    "kept": "auth_disabled",
                    "hint": "凭据失效需人工重新登录，然后用 POST /api/guard/auth-recovered 或重新读凭据",
                }
            e.update(
                {
                    "cooldown_until": 0.0,
                    "fail_streak": 0,
                    "disabled": False,
                    "disable_kind": "",
                    "content_blocked_until": 0.0,
                    "last_error": "",
                }
            )
            self._save()
            return {"reset": key}

    def auth_recovered(self, key: str | None = None) -> dict:
        """凭据已人工刷新 —— 只解 auth 类禁用，不碰 403/429/内容类冷却。"""
        with self._lock:
            released = []
            targets = [key] if key else list(self._state.keys())
            for k in targets:
                e = self._state.get(k)
                if not e:
                    continue
                if e.get("disabled") and e.get("disable_kind") == "auth":
                    e["disabled"] = False
                    e["disable_kind"] = ""
                    e["fail_streak"] = 0
                    e["last_error"] = ""
                    released.append(k)
            self._save()
            return {"released": released}

    def acquire(self, key: str) -> None:
        """节流：同账号两次请求之间强制拉开间隔，并加随机抖动。

        这是最便宜的降封手段——把机器式的匀速请求变成接近人的不规则节奏。
        夜间（quiet_hours）再把间隔按倍数放大：人睡觉时不该有稳定流量。
        """
        min_ms = float(self.rules.get("min_interval_ms", 1500))
        max_ms = float(self.rules.get("max_interval_ms", 6000))
        factor = self._quiet_factor()
        min_ms *= factor
        max_ms *= factor
        span = max(0.0, max_ms - min_ms)
        jitter = random.uniform(0.0, span) if span > 0 else 0.0
        wait_ms = min_ms + jitter

        with self._lock:
            last = self._last_request_ts.get(key, 0.0)
            elapsed = (_now() - last) * 1000.0
            if elapsed < wait_ms:
                need = wait_ms - elapsed
            else:
                need = 0.0

        if need > 0:
            time.sleep(need / 1000.0)
        with self._lock:
            self._last_request_ts[key] = _now()

    def _quiet_factor(self) -> float:
        """当前是否处于安静时段；返回间隔放大倍数。"""
        spec = str(self.rules.get("quiet_hours") or "").strip()
        factor = float(self.rules.get("quiet_hours_factor", 4.0) or 0)
        if not spec or factor <= 1:
            return 1.0
        try:
            start_s, _, end_s = spec.partition("-")
            start_h, end_h = int(start_s), int(end_s or start_s)
        except (TypeError, ValueError):
            return 1.0
        hour = time.localtime().tm_hour
        inside = (start_h <= hour < end_h) if start_h <= end_h else (hour >= start_h or hour < end_h)
        return factor if inside else 1.0

    def budget_exceeded(self, key: str) -> tuple[bool, str]:
        """日请求预算。0 = 不限制。跨天自动清零。"""
        limit = int(self.rules.get("daily_budget", 0) or 0)
        if limit <= 0:
            return False, ""
        with self._lock:
            e = self._entry(key)
            today = time.strftime("%Y-%m-%d", time.localtime())
            if e.get("day") != today:
                e["day"] = today
                e["day_total"] = 0
            if e.get("day_total", 0) >= limit:
                return True, f"daily budget {limit} reached"
            return False, ""

    def record(self, key: str, kind: str, detail: str = "") -> None:
        """kind: ok | rate | balance | error | auth | content_blocked"""
        with self._lock:
            e = self._entry(key)
            e["total"] += 1
            today = time.strftime("%Y-%m-%d", time.localtime())
            if e.get("day") != today:
                e["day"] = today
                e["day_total"] = 0
            e["day_total"] = e.get("day_total", 0) + 1

            if kind == "ok":
                e["ok"] += 1
                e["fail_streak"] = 0
                e["cooldown_until"] = 0.0
                e["last_ok_ts"] = _now()
                e["last_error"] = ""
            elif kind == "rate":
                e["err"] += 1
                e["fail_streak"] += 1
                e["cooldown_until"] = _now() + float(self.rules.get("cooldown_rate_limited", 60))
                e["last_error"] = detail or "HTTP 429"
            elif kind == "balance":
                e["err"] += 1
                e["fail_streak"] += 1
                e["cooldown_until"] = _now() + float(self.rules.get("cooldown_balance_exhausted", 12 * 3600))
                e["last_error"] = detail or "HTTP 402 / 余额不足"
            elif kind == "transport":
                # 本地/网络层失败：只计数，不算账号信誉分。
                # "客户端没开"和"账号被风控"是两件事，混在一起会导致误冷却。
                e["transport_err"] = e.get("transport_err", 0) + 1
                e["last_error"] = detail or "upstream unreachable"
                # 不 += fail_streak，不设 cooldown_until
            elif kind == "error":
                e["err"] += 1
                e["fail_streak"] += 1
                e["last_error"] = detail or "upstream error"
                threshold = int(self.rules.get("error_threshold", 3))
                if e["fail_streak"] >= threshold:
                    e["cooldown_until"] = _now() + float(self.rules.get("cooldown_repeated_errors", 600))
                    e["last_error"] = (detail or "repeated errors") + " -> cooldown"
            elif kind == "auth":
                e["err"] += 1
                e["disabled"] = True
                # 记录禁用原因：auth 类禁用不能被普通 reset 解开，
                # 否则"凭据失效还在敲门"会原地复活。
                e["disable_kind"] = "auth"
                e["fail_streak"] = 0
                e["cooldown_until"] = 0.0
                e["last_error"] = detail or "HTTP 401 / 410000 token 失效"
            elif kind == "content_blocked":
                # 语义层判定：不重试、不换号、不换区域。只是停手一段时间。
                e["cooldown_until"] = _now() + float(self.rules.get("cooldown_content_blocked", 900))
                e["content_blocked_until"] = e["cooldown_until"]
                e["last_error"] = "content_blocked_sentinel"
            self._save()
