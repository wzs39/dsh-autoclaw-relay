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

## 故障排查

- **模型调用报 `TRANSPORT` 连接失败** → relay 没起来：看 `~/.dsh/autoclaw-relay/relay.log`；手动验证 `python relay.py -c <config> --check`。
- **报 `HTTP 503: local cooldown`** → 正常防封行为，等冷却或 `POST /api/guard/reset`（401 禁用必须人工 reset）。
- **模型目录为空** → relay 没就绪或上游 `/v1/models` 失败；在设置页点「检测模型」重拉，或在 `models` 配置里写静态清单。
- **端口冲突** → 改 `config.json` 的 `listen`，插件每次请求现读端点，即时生效。
- **Python 找不到** → `pythonPath` 填绝对路径。

## 文件布局

```
lib/index.js            入口：配置、进程托管、提供方注册、每日签到
lib/openai-adapter.js   DSH ↔ OpenAI 适配器（SSE 解析、工具调用、哨兵拦截、并发闸门）
python/                 AutoClaw 中继网关（纯 Python 3.10+ 标准库，10 个文件原样打包）
```
