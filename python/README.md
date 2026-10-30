# AutoClaw 反代网关

把你**自己账号**在官方 AutoClaw 桌面客户端里已登录的本地 `model-proxy` 额度，
暴露成标准的 OpenAI 兼容接口（`/v1/chat/completions`、`/v1/models`），
同时内置一整套**降低封禁风险**的收敛与冷却机制。

> 前提：你只在自己的机器上、用自己的账号、复用自己已经登录的官方客户端会话。
> 不破解客户端、不绕过付费校验、不做批量账号运营。

---

## 1. 架构

```
本地工具 / IDE 插件
      │  OpenAI 兼容
      ▼
┌─────────────────────────────────────────────┐
│ relay.py  (127.0.0.1:8080)                  │
│  ├─ 指纹收敛  fingerprint.py                │
│  ├─ 冷却状态机  guard.py                     │
│  ├─ 节流 / 抖动                               │
│  └─ 内容哨兵检测                               │
└─────────────────────────────────────────────┘
      │  本地登录态（免 client-sign）
      ▼
127.0.0.1:<port>/internal/model-proxy/v1  ← 官方 AutoClaw 桌面客户端
```

## 2. 快速开始

```bash
# 1) 起本地上游夹具（模拟官方 model-proxy，不接触真实账号）
python fixture_upstream.py

# 2) 起网关
cp config.example.json config.json
python relay.py -c config.json

# 3) 端到端自测
python client_test.py
```

Windows 一键：`run.bat`；Linux/macOS：`./run.sh`。

## 3. 接入真实账号

1. 保持 AutoClaw 桌面客户端处于登录状态。
2. 找到客户端本地 `model-proxy` 的监听端口（默认常见为 `7890`）。
3. 编辑 `config.json`：

```json
{
  "upstream": {
    "mode": "model-proxy",
    "base": "http://127.0.0.1:7890/internal/model-proxy",
    "path_prefix": "/v1"
  },
  "headers": {
    "cookie": "<你的会话 cookie>",
    "authorization": "<你的 token>"
  }
}
```

4. 客户端接入：

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="sk-local-change-me")
resp = client.chat.completions.create(
    model="openclaw",
    messages=[{"role": "user", "content": "你好"}],
)
```

## 4. 降低封禁风险的 5 个机制

### 4.1 指纹收敛（`fingerprint.py`）

所有请求头收敛成**固定顺序、固定取值**的集合，上游长期看到同一份指纹：

- 丢弃 `X-Request-Model` / `X-Client-Sign` / `X-Forwarded-For` / `Via` 等代理特征头
- `User-Agent` / `Accept` / `Accept-Language` / `Origin` / `Referer` 统一
- 剥掉 hop-by-hop 头（`Connection`、`TE`、`Transfer-Encoding`…）

> 实测：2026-09-23 起 `zai-proxy` 直连因 `X-Request-Model: zai_*` 命中阿里 WAF 的 Bot 规则，直接 `406`。
> 网关已默认丢弃该头，并在 `fallback_on_406` 开启时自动切到 `relay` 通道。

### 4.2 节流 + 抖动（`guard.py`）

同账号两次请求强制拉开 `1500–6000ms` 的随机间隔，把机器式匀速请求变成接近人的节奏。
这是最便宜、最有效的降封手段。

### 4.3 冷却状态机（`guard.py`）

| 上游表现 | 处理 | 默认时长 |
| --- | --- | --- |
| HTTP 402 / 余额不足 | 长冷却 | 12h |
| HTTP 429 | 短冷却 | 60s |
| 连续错误达阈值（默认 3 次） | 中冷却 | 10m |
| 401 / 410000 / Unauthorized | **禁用**，需人工 `POST /api/guard/reset` | 永久 |

冷却期内请求**直接在本地返回 503**，绝不打到上游 —— 避免在账号已被风控后继续猛打。

### 4.4 内容哨兵（`guard.py`）

上游的内容风控不是返回 HTTP 错误码，而是把固定哨兵串
`DANGEROUS_ACTIVITY_BLOCKED` 作为正常回复正文返回（HTTP 200、`finish_reason: stop`、
正文长度恰好等于该串）。

网关检测到后：**停手、记录、不自动重试、不换号、不换区域**。
内容层面的判定换任何手段都不会改变结果，唯一正确做法是回到内容本身。

### 4.5 tools 上限截断

上游（Z.AI）限制 `tools` 数组最多 64 条。网关在转发前检测到超出时
自动截断到前 64 条并记录日志，避免上游直接返回 `HTTP 413: tools must contain at most 64 entries`。

### 4.6 日志脱敏

`relay.py` 的 `log_message` 只保留请求行，不落 `Authorization` / `Cookie` / 请求体。
凭证只存在于 `config.json`（本地文件，别提交到仓库）。

## 5. 管理接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 存活 + 当前模式 |
| GET | `/status` | 冷却状态 + 指标 |
| GET | `/metrics` | 计数器 |
| GET | `/api/guard/status` | 冷却状态 |
| POST | `/api/guard/reset` | 解除冷却/禁用（人工操作） |

## 6. 每日签到

```bash
python checkin.py --dry-run
python checkin.py --region cn --token "<会话 cookie>"
python checkin.py --region global --token "<token>"
```

上游端点幂等（`already_completed=true` 表示今日已签），重复执行安全。
**不要**用签到去刷额度或跨账号批量执行。

## 7. 文件清单

| 文件 | 作用 |
| --- | --- |
| `relay.py` | 网关主程序（HTTP 反代 + 缓存 + 路由） |
| `fingerprint.py` | 请求/响应头收敛与去特征化 |
| `guard.py` | 冷却状态机 + 节流 + 内容哨兵 |
| `fixture_upstream.py` | 本地上游夹具，用于离线自测 |
| `client_test.py` | 端到端自测（含 429/402/401/哨兵场景） |
| `checkin.py` | 每日签到（幂等） |
| `config.example.json` | 配置模板 |
| `run.bat` / `run.sh` | 一键起夹具 + 网关 + 自测 |

## 8. 风险边界

- 本网关**不做**任何内容安全判定，也不提供绕过手段；判定发生在上游模型侧，网关只做原样透传。
- 本网关**不做**提示词注入、验证码绕过、批量注册、买卖账号、滥用免费额度。
- 上游接口随时可能变更，网关可能随时失效。
- 一切使用风险（含账号被风控、封禁）由使用者自行承担。
