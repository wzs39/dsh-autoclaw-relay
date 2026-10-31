# dsh-autoclaw-relay

把 **AutoClaw 防封中继网关**（本地 Python relay）注册成 DeepSeek Harness 的模型提供方。

```
DSH (模型选择器: AutoClaw 中继)
  └─ lib/openai-adapter.js   DSH 词表 ↔ OpenAI 线格式，SSE 转流式事件
       └─ relay.py (127.0.0.1:8080)   指纹收敛 / 节流抖动 / 冷却状态机 / 内容哨兵 / 日志脱敏
            └─ 本地 model-proxy (127.0.0.1:7890) → 上游
```

## 安装状态

本机已完成：

- 插件装在 `C:\Users\qwe\.dsh\plugins\dsh-autoclaw-relay`，desktop profile 已挂 `file:` 依赖与 bundle 行；
- 运行配置在 `C:\Users\qwe\.dsh\autoclaw-relay\config.json`（首次自动生成，插件更新不覆盖手工改动）；
- **需要重启 DSH 桌面端生效**。重启后在模型选择器里会出现「AutoClaw 中继（本地防封网关）」分组。

## 前置条件

- Python 3.10+（`python` 在 PATH 上，或用 `pythonPath` 配置指定；relay 是纯标准库，零第三方依赖）；
- 上游按 `config.json` 的 `upstream.base` 就绪（默认本地 model-proxy `127.0.0.1:7890`）；
- 需要会话凭证的模式：把 cookie / authorization 填进 `config.json` 的 `headers` 段。

## 配置项（DSH 设置页 → AutoClaw 中继）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关：关掉后不注册提供方、不拉起 relay |
| `provider` | `autoclaw-relay` | 提供方路由 id |
| `displayName` | `AutoClaw 中继（本地防封网关）` | 模型选择器里的显示名 |
| `pythonPath` | `python` | Python 3.10+ 可执行文件 |
| `relayDir` | 插件自带 `python/` | `relay.py` 所在目录（要跑自己那份时指定） |
| `configPath` | `~/.dsh/autoclaw-relay/config.json` | relay 运行配置路径 |
| `autoStart` | `true` | 插件加载时自动拉起 relay（已有健康实例则复用，不重复占端口） |
| `autoRestart` | `true` | relay 异常退出自动重启（一小时上限 5 次，防崩溃循环） |
| `startTimeoutMs` | `20000` | 等 `/healthz` 就绪的超时 |
| `noRetry` | `true` | **防封默认：冷却/限流期间不自动重试**；关掉后按 `retryPolicy` |
| `retryPolicy` | dsh-llm 默认 | `noRetry=false` 时生效 |
| `models` | `[]` | 静态模型清单；空 = 经 relay `/v1/models` 自动发现（带 5 分钟缓存） |
| `maxConcurrent` | `2` | 同时打到 relay 的最大流数 |
| `defaultContextWindow` | `1000000` | 未声明时的默认上下文窗口 |
| `maxTokens` | `32768` | 默认 max_tokens |
| `idleTimeoutMs` | `300000` | 流空闲超时（连续无数据掐断） |
| `dailyCheckin` | `false` | 每天首次加载跑一次 `checkin.py`（服务端幂等） |
| `checkinRegion` | `cn` | 签到区域 `cn` / `global` |

## 防封机制（十层）

**插件层**（本插件新增）：

1. **session-title 本地短路** — 会话标题不产生任何上游请求（每个新会话省一次）；
2. **模型目录零上游轮询** — `resolveModel` 只读静态配置/缓存，绝不逐请求探测；
3. **冷却期不撞门** — relay 返回 503 冷却时错误原样上抛、默认零重试（`noRetry`）；
4. **并发闸门** — 并行子代理的请求被收成近人节奏（`maxConcurrent`）；
5. **哨兵换装** — `DANGEROUS_ACTIVITY_BLOCKED` 命中后正文换成可读提示并停手，哨兵串不进会话历史、不暗示重试；密钥/cookie 永不落日志（签到 token 走环境变量，不进命令行）。

**网关层**（`python/relay.py`，见 `python/README.md`）：

6. **指纹收敛** — 固定头顺序/取值，剥 hop-by-hop 与代理特征头（`X-Request-Model: zai_*` 等实测触发点）；
7. **节流 + 抖动** — 同账号两次请求强制拉开 1.5–6s 随机间隔；
8. **冷却状态机** — 402→12h、429→60s、连续错误 3 次→10m、401→禁用（人工 `POST /api/guard/reset`）；冷却期内本地直接 503，**绝不打上游**；
9. **内容哨兵** — 上游把封禁提示当 200 正文返回时识别命中、停手、记录；
10. **日志脱敏** — 只留请求行，不落 `Authorization`/`Cookie`/请求体。

## 管理端点（relay 本地）

| 端点 | 说明 |
| --- | --- |
| `GET /healthz` | 健康检查（插件用它做复用判断与就绪探测） |
| `GET /status` | 模式 + 守卫状态 + 指标 |
| `GET /metrics` | 请求计数 |
| `GET /api/guard/status` | 冷却剩余秒数 |
| `POST /api/guard/reset` | 人工解除冷却/禁用（401 禁用后必须走这里） |

## 账号与积分（只读）

网关直接读**本机官方客户端已登录账号**，解 `account-credentials.enc`
（Electron safeStorage = Chromium os_crypt：DPAPI 保护 AES 密钥 + v10 AES-256-GCM 信封），
再按官方中间件头签名（`X-Auth-Sign = md5(appId&ts&appKey)`）调只读接口。

| 端点 | 说明 |
| --- | --- |
| `GET /api/account`、`GET /api/credits` | 账号 + 积分（总积分、各钱包分桶、即将过期、签到状态） |
| `GET /api/usage` | 插件卡片用聚合视图：账号 + 积分 + 守卫 + metrics + 上游可达性 |
| `autoclaw_account`（模型工具） | agent 可调用的同一份数据 |

数据带 5 分钟缓存（`credits_cache_ttl_s`），避免看板轮询把上游打成异常流量。
CLI 直查：`python credits.py` / `python credits.py --json`。

## 防封（对齐历史事故教训）

来自 `deepseek-web-bridge/guard-policy.md` 的真实封禁复盘，逐条落到代码：

| 教训 | 落实 |
| --- | --- |
| **被拒后继续敲门是封禁主因** | 401/auth 类失败 → `disabled` + `disable_kind=auth`，主流程直接 503，**不打上游** |
| **熔断状态必须落盘，重启不能失忆** | `guard-state.json` 原子写；状态结构带 `disable_kind`，重启后仍生效 |
| **`#save()` 静默失败会让熔断形同虚设** | 写盘失败不再吞异常：记 `state_error` 并打到 stderr，`/api/usage` 暴露 `state_writable` |
| **凭据失效只能人工解决** | `reset` **不解 auth 禁用**；必须 `POST /api/guard/auth-recovered`（语义=我重登了） |
| **新号/长期要低负载** | `daily_budget` 日预算闸，超额本地 503 |
| **人类有作息，夜间该低流量** | `quiet_hours` + `quiet_hours_factor`，夜间间隔按倍数放大 |
| **不追加重试** | 插件层 `noRetry` 默认开（`maxRetries=0`） |
| **本地连不上 ≠ 账号异常** | 连接失败归 `transport`，只计数**不扣信誉、不冷却**（否则"客户端没开"会被记成"账号连续失败"） |

### 当前生效参数（`~/.dsh/autoclaw-relay/config.json`）

```json
"guard": {
  "min_interval_ms": 6000,
  "max_interval_ms": 18000,
  "daily_budget": 80,
  "quiet_hours": "0-7",
  "quiet_hours_factor": 4.0,
  "error_threshold": 3,
  "cooldown_repeated_errors": 600,
  "cooldown_auth_failed": null
}
```

## 故障排查

- **模型调用报 `upstream_unreachable`** → 响应里直接带 `upstream.host/port/reachable` 和 `hint`，
  不再是一句裸的 `ConnectionRefusedError`；`GET /healthz` 也会报告上游可达性。
- **报 `HTTP 503: auth_disabled`** → 凭据被上游拒绝。重新登录官方客户端后
  `POST /api/guard/auth-recovered`（`reset` 故意不解这一类）。
- **报 `HTTP 503: daily_budget`** → 日预算用满，等次日或调高 `daily_budget`。
- **积分显示 0 或 `creditsError`** → 客户端未登录 / 凭据解密失败；CLI 跑 `python credits.py` 看具体原因。
- **端口冲突** → 改 `config.json` 的 `listen`，插件每次请求现读端点，即时生效。
- **Python 找不到** → `pythonPath` 填绝对路径。

## 文件布局

```
lib/index.js            入口：配置、进程托管、提供方注册、账号/积分工具、usage 路由
lib/client.js           客户端卡片：积分 / 账号 / 冷却 / 到期提醒 / 日预算用量
lib/openai-adapter.js   DSH ↔ OpenAI 适配器（SSE 解析、工具调用、哨兵拦截、并发闸门）
python/credits.py       账号与积分（DPAPI 解密 + 官方签名 + 只读接口）
python/guard.py         冷却状态机 / 节流 / 日预算 / 作息曲线
python/relay.py         网关主程序
```

