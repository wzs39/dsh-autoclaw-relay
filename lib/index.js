/**
 * dsh-autoclaw-relay — 插件入口（Host 半身）。
 *
 * 把本地 Python 防封中继网关（autoclaw-relay）接进 DeepSeek Harness：
 *   1. 进程托管：DSH 启动时自动拉起 `python relay.py -c <config>`（已有健康
 *      实例则复用，不重复占端口）；异常退出按上限自动重启；插件停用即杀。
 *   2. 模型接入：注册 provider `autoclaw-relay`，适配器把 DSH 词表转成
 *      OpenAI 线格式打给本地网关（见 lib/openai-adapter.js）。
 *   3. 运行配置：`~/.dsh/autoclaw-relay/config.json`（首次自动从模板生成，
 *      用户在里面填上游地址与会话凭证；插件更新不覆盖手工改动）。
 *   4. 防封（插件层）：session-title 本地短路、静态模型目录零上游轮询、
 *      冷却期不重试（noRetry 默认开）、并发闸门、密钥/cookie 永不落日志。
 *      网关侧五层（指纹收敛/节流抖动/冷却状态机/内容哨兵/日志脱敏）见
 *      python/README.md。
 *   5. 可选每日签到：dailyCheckin 开启后每天首次加载跑一次 checkin.py
 *      （服务端幂等），token 从 relay config 的 headers.cookie 读取、
 *      经环境变量传入，不进命令行参数（防进程列表泄露）。
 *
 * 依赖只有 harness 自带的 peer 包，没有第三方运行时依赖。
 *
 * @module index.js
 */

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'

import z from '@deepseek-ai/schemastery'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { RetryPolicySchema, resolveRetryPolicy } from '@deepseek-ai/dsh-llm'

import { AutoClawRelayAdapter } from './openai-adapter.js'

export const name = 'dsh-autoclaw-relay'

/** `llm` 承载模型提供方注册。`tools` 是可选的：用 ctx.inject 挂载，缺失时只少一个工具。 */
export const inject = ['llm']

/** 插件包目录（lib/ 的上一级）。 */
const PLUGIN_DIR = fileURLToPath(new URL('..', import.meta.url))

/** 运行数据目录：relay 的 config.json / relay.log / 签到状态都放这里，插件更新不覆盖。 */
const DEFAULT_RUNTIME_DIR = path.join(os.homedir(), '.dsh', 'autoclaw-relay')

const MODEL_MODALITIES = ['text', 'image']

const catalogModel = z.object({
  id: z.string().required(),
  name: z.string(),
  contextWindow: z.number().step(1).min(1),
  maxTokens: z.number().step(1).min(1),
  inputModalities: z.array(z.union(MODEL_MODALITIES)).min(1),
})

export const Config = z.object({
  enabled: z.boolean().default(true).description('总开关：关掉后不注册模型提供方、不拉起 relay 进程'),
  provider: z.string().default('autoclaw-relay').description('提供方路由 id（模型选择器里的分组键）'),
  displayName: z.string().default('AutoClaw 中继（本地防封网关）').description('提供方显示名'),
  pythonPath: z.string().default('python').description('Python 3.10+ 可执行文件（relay 是纯标准库，无需装包）'),
  relayDir: z.string().default('').description('relay.py 所在目录；留空 = 插件自带的 python/'),
  configPath: z.string().default('').description('relay 运行配置路径；留空 = ~/.dsh/autoclaw-relay/config.json'),
  autoStart: z.boolean().default(true).description('插件加载时自动拉起本地 relay（检测到健康实例则复用）'),
  autoRestart: z.boolean().default(true).description('relay 进程异常退出后自动重启（一小时最多 5 次，防崩溃循环）'),
  startTimeoutMs: z.number().step(1).min(3000).default(20000).description('等待网关 /healthz 就绪的超时（毫秒）'),
  noRetry: z.boolean().default(true).description('防封默认：不自动重试（冷却/限流期间避免撞门）；关掉后按 retryPolicy 重试'),
  retryPolicy: RetryPolicySchema,
  models: z.array(catalogModel).default([]).description('静态模型清单；留空 = 通过 relay /v1/models 自动发现（带缓存，不逐请求探测）'),
  maxConcurrent: z.number().step(1).min(1).max(4).default(2).description('同时打到本地网关的最大流数（防封：把并行请求收成近人节奏）'),
  defaultContextWindow: z.number().step(1).min(1).default(1000000).description('未在静态清单里声明 contextWindow 时的默认值'),
  maxTokens: z.number().step(1).min(1).default(32768).description('默认 max_tokens'),
  idleTimeoutMs: z.number().step(1).min(15000).default(300000).description('流空闲超时：连续无数据多少毫秒后掐断（毫秒）'),
  dailyCheckin: z.boolean().default(false).description('每天首次加载跑一次 checkin.py（幂等）；token 取 relay config 的 headers.cookie'),
  checkinRegion: z.union(['cn', 'global']).default('cn').description('签到区域'),
  glmproxyEnabled: z.boolean().default(true).description('托管 glmproxy（AutoClaw GLM 反代）：随 DSH 自动拉起/回收/崩溃重启，relay 的上游就是它'),
  glmproxyPort: z.number().step(1).min(1024).max(65535).default(18791).description('glmproxy 监听端口；必须与 relay config 的 upstream.base 端口一致'),
  glmproxyEntry: z.string().default('').description('glmproxy 的 cli.js 入口路径；留空 = 自动探测（AutoClaw-official 工具链 / npm 全局）'),
  glmproxyNode: z.string().default('').description('运行 glmproxy 的 node 可执行文件；留空 = 自动探测（AutoClaw2 工具链 node）'),
  glmproxyIdentityVersion: z.string().default('42.6.1').description('发给上游的 X-Version（对齐本机 AutoClaw2 版本）'),
  glmproxyUpstreamHost: z.string().default('autoglm-acceleration-api.zhipuai.cn').description('glmproxy 的 UPSTREAM_HOST（国内 zhipuai 基建）'),
  glmproxyUpstreamPath: z.string().default('/autoclaw-proxy/proxy/autoclaw/chat/completions').description('glmproxy 的 UPSTREAM_PATH'),
  glmproxyModelConfigPath: z.string().default('/autoclaw-proxy/proxy/autoclaw-model-config').description('glmproxy 的 MODEL_CONFIG_PATH'),
  glmproxyKey: z.string().default('mewmew').description('glmproxy 的 PROXY_KEY（默认即可，仅回环）'),
  glmproxyUserDataRoot: z.string().default('').description('AUTOCLAW_USER_DATA_ROOT；留空 = %APPDATA%\AutoClaw-official'),
  glmproxyStartTimeoutMs: z.number().step(1).min(3000).default(15000).description('等待 glmproxy 就绪的超时（毫秒）'),
})

/** 校验并规范化静态模型清单（与 dsh-zcode-cli-proxy 同强度）。 */
function resolveModels(models) {
  const seen = new Set()
  return (Array.isArray(models) ? models : []).map((model) => {
    if (typeof model.id !== 'string' || model.id === '') throw new Error('autoclaw-relay: 模型 id 不能为空')
    if (model.name !== undefined && model.name === '') throw new Error(`autoclaw-relay: 模型 "${model.id}" 的 name 不能为空`)
    if (seen.has(model.id)) throw new Error(`autoclaw-relay: 模型 "${model.id}" 重复`)
    seen.add(model.id)
    const inputModalities = Array.isArray(model.inputModalities) && model.inputModalities.length > 0 ? model.inputModalities : ['text']
    return {
      id: model.id,
      ...(model.name === undefined ? {} : { name: model.name }),
      contextWindow: model.contextWindow ?? undefined,
      maxTokens: model.maxTokens ?? undefined,
      inputModalities: [...inputModalities],
    }
  })
}

function clampInt(value, min, max, fallback) {
  const parsed = Number(value)
  if (!Number.isSafeInteger(parsed)) return fallback
  return Math.min(max, Math.max(min, parsed))
}

/** 把 Config 解析成运行期只读配置；非法值直接抛错（apply 侧保留上一份可用配置）。 */
export function resolveOptions(config) {
  const noRetry = config.noRetry !== false
  return {
    enabled: config.enabled !== false,
    provider: (typeof config.provider === 'string' && config.provider.trim() !== '') ? config.provider.trim() : 'autoclaw-relay',
    displayName: (typeof config.displayName === 'string' && config.displayName.trim() !== '') ? config.displayName.trim() : 'AutoClaw 中继（本地防封网关）',
    pythonPath: (typeof config.pythonPath === 'string' && config.pythonPath.trim() !== '') ? config.pythonPath.trim() : 'python',
    relayDir: (typeof config.relayDir === 'string' ? config.relayDir.trim() : ''),
    configPath: (typeof config.configPath === 'string' ? config.configPath.trim() : ''),
    autoStart: config.autoStart !== false,
    autoRestart: config.autoRestart !== false,
    startTimeoutMs: config.startTimeoutMs ?? 20000,
    noRetry,
    // 防封默认压成 0 次重试；用户显式关掉 noRetry 才按自己的 retryPolicy 来
    retryPolicy: noRetry === true
      ? resolveRetryPolicy({ mode: 'normal', maxRetries: 0 }, 'autoclaw-relay: retryPolicy')
      : resolveRetryPolicy(config.retryPolicy, 'autoclaw-relay: retryPolicy'),
    models: resolveModels(config.models),
    maxConcurrent: clampInt(config.maxConcurrent, 1, 4, 2),
    defaultContextWindow: config.defaultContextWindow ?? 1000000,
    maxTokens: config.maxTokens ?? 32768,
    idleTimeoutMs: config.idleTimeoutMs ?? 300000,
    dailyCheckin: config.dailyCheckin === true,
    checkinRegion: config.checkinRegion === 'global' ? 'global' : 'cn',
    glmproxyEnabled: config.glmproxyEnabled !== false,
    glmproxyPort: clampInt(config.glmproxyPort, 1024, 65535, 18791),
    glmproxyEntry: (typeof config.glmproxyEntry === 'string' ? config.glmproxyEntry.trim() : ''),
    glmproxyNode: (typeof config.glmproxyNode === 'string' ? config.glmproxyNode.trim() : ''),
    glmproxyIdentityVersion: (typeof config.glmproxyIdentityVersion === 'string' && config.glmproxyIdentityVersion.trim() !== '') ? config.glmproxyIdentityVersion.trim() : '42.6.1',
    glmproxyUpstreamHost: (typeof config.glmproxyUpstreamHost === 'string' && config.glmproxyUpstreamHost.trim() !== '') ? config.glmproxyUpstreamHost.trim() : 'autoglm-acceleration-api.zhipuai.cn',
    glmproxyUpstreamPath: (typeof config.glmproxyUpstreamPath === 'string' && config.glmproxyUpstreamPath.trim() !== '') ? config.glmproxyUpstreamPath.trim() : '/autoclaw-proxy/proxy/autoclaw/chat/completions',
    glmproxyModelConfigPath: (typeof config.glmproxyModelConfigPath === 'string' && config.glmproxyModelConfigPath.trim() !== '') ? config.glmproxyModelConfigPath.trim() : '/autoclaw-proxy/proxy/autoclaw-model-config',
    glmproxyKey: (typeof config.glmproxyKey === 'string' && config.glmproxyKey.trim() !== '') ? config.glmproxyKey.trim() : 'mewmew',
    glmproxyUserDataRoot: (typeof config.glmproxyUserDataRoot === 'string' ? config.glmproxyUserDataRoot.trim() : ''),
    glmproxyStartTimeoutMs: config.glmproxyStartTimeoutMs ?? 15000,
  }
}

function readRelayConfig(configPath) {
  try {
    return JSON.parse(fs.readFileSync(configPath, 'utf8'))
  } catch {
    return undefined
  }
}

/** 从 relay 运行配置解析网关端点与网关密钥（每次调用现读，用户改配置即时生效）。 */
function endpointOf(options) {
  const configPath = options().configPath !== '' ? options().configPath : path.join(DEFAULT_RUNTIME_DIR, 'config.json')
  const relay = readRelayConfig(configPath)
  const listen = String(relay?.listen ?? '127.0.0.1:8080')
  const at = listen.lastIndexOf(':')
  let host = '127.0.0.1'
  let port = '8080'
  if (at > 0) {
    host = listen.slice(0, at) || '127.0.0.1'
    port = listen.slice(at + 1) || '8080'
  }
  const key = String(relay?.gateway_key ?? '')
  return { baseURL: `http://${host}:${port}/v1`, key, configPath }
}

// ── apply ─────────────────────────────────────────────────────────────────────

export function apply(ctx, config) {
  const logger = ctx.logger ?? console

  // 配置热更新容错：一次非法改动不打挂正在跑的插件，保留上一份可用配置。
  let current = () => config
  let lastRaw
  let lastGood
  const options = () => {
    const raw = current()
    if (raw === lastRaw && lastGood !== undefined) return lastGood
    try {
      const next = resolveOptions(raw)
      lastRaw = raw
      lastGood = next
      return next
    } catch (error) {
      if (lastGood === undefined) throw error
      lastRaw = raw
      logger.error?.('autoclaw-relay: 配置非法，继续使用上一份可用配置')
      logger.error?.(error)
      return lastGood
    }
  }
  options()

  const entryNs = ctx.fiber?.entry?.options?.id ?? name
  let stopping = false
  let child = undefined
  let restartTimestamps = []
  let glmChild = undefined
  let glmRestartTimestamps = []

  const relayDirOf = () => {
    const configured = options().relayDir
    return configured !== '' ? configured : path.join(PLUGIN_DIR, 'python')
  }
  const configPathOf = () => {
    const configured = options().configPath
    return configured !== '' ? configured : path.join(DEFAULT_RUNTIME_DIR, 'config.json')
  }

  /** 首次生成运行配置（模板来自插件 python/，绝不覆盖已有手工配置）。 */
  function seedRuntimeConfig() {
    const configPath = configPathOf()
    if (fs.existsSync(configPath)) return configPath
    try {
      fs.mkdirSync(path.dirname(configPath), { recursive: true })
      const example = path.join(relayDirOf(), 'config.example.json')
      const source = fs.existsSync(example) ? example : path.join(PLUGIN_DIR, 'python', 'config.example.json')
      fs.copyFileSync(source, configPath)
      logger.info?.(`autoclaw-relay: 已从模板生成运行配置 ${configPath}（上游与凭证在 headers/upstream 段填）`)
    } catch (error) {
      logger.warn?.(`autoclaw-relay: 运行配置生成失败（${error?.message ?? error}）`)
    }
    return configPath
  }

  async function healthy(base) {
    try {
      const resp = await fetch(`${base}/healthz`, { signal: AbortSignal.timeout(1500) })
      if (resp.ok !== true) return false
      const body = await resp.json().catch(() => undefined)
      return body?.ok === true
    } catch {
      return false
    }
  }

  /** relay 日志文件句柄（5MB 轮转一次，只留一代 .old）。 */
  function openLog() {
    const dir = path.dirname(configPathOf())
    try {
      fs.mkdirSync(dir, { recursive: true })
    } catch { /* 目录已存在 */ }
    const logPath = path.join(dir, 'relay.log')
    try {
      const stat = fs.statSync(logPath)
      if (stat.size > 5 * 1024 * 1024) {
        try {
          fs.rmSync(`${logPath}.old`, { force: true })
        } catch { /* 无旧文件 */ }
        fs.renameSync(logPath, `${logPath}.old`)
      }
    } catch { /* 文件还不存在 */ }
    return fs.openSync(logPath, 'a')
  }

  function scheduleRestart() {
    const config = options()
    if (stopping === true || config.autoStart !== true || config.autoRestart !== true) return
    const now = Date.now()
    restartTimestamps = restartTimestamps.filter((at) => now - at < 3_600_000)
    if (restartTimestamps.length >= 5) {
      logger.warn?.('autoclaw-relay: 一小时内已自动重启 5 次，停止重启；排查见 relay.log')
      return
    }
    restartTimestamps.push(now)
    setTimeout(() => {
      if (stopping === false) void ensureRelay().catch((error) => logger.warn?.(`autoclaw-relay: 自动重启失败（${error?.message ?? error}）`))
    }, 5000)
  }

  // ── glmproxy 托管（AutoClaw GLM 反代，relay 的上游）────────────────────────
  // 机器重启后 18791 无人拉起是 502 ConnectionRefused 的头号来源：
  // DSH 启动 → 先拉 glmproxy → 再拉 relay，全链自愈；崩溃各走各的自动重启。
  /** 任何 HTTP 响应（含 401/404）都证明服务在；只有连接拒绝/超时才算死。 */
  async function glmHealthy(base) {
    try {
      await fetch(`${base}/healthz`, { signal: AbortSignal.timeout(1500) })
      return true
    } catch {
      return false
    }
  }

  function resolveGlmEntry() {
    const config = options()
    if (config.glmproxyEntry !== '') {
      return fs.existsSync(config.glmproxyEntry) ? config.glmproxyEntry : null
    }
    const appdata = process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming')
    const candidates = [
      path.join(appdata, 'AutoClaw-official', 'toolchain', 'node', 'prefix', 'node_modules', 'glmproxy', 'bin', 'cli.js'),
      path.join(appdata, 'npm', 'node_modules', 'glmproxy', 'bin', 'cli.js'),
    ]
    for (const entry of candidates) {
      if (fs.existsSync(entry)) return entry
    }
    return null
  }

  function resolveGlmNode() {
    const config = options()
    if (config.glmproxyNode !== '') return config.glmproxyNode
    const appdata = process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming')
    const localappdata = process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local')
    // 注意：不能用 process.execPath —— DSH 桌面端里它是 Electron 外壳，
    // 拿它 spawn cli.js 会再开一个应用窗口。
    const candidates = [
      path.join(appdata, 'AutoClaw-official', 'toolchain', 'node', 'node.exe'),
      path.join(localappdata, 'Programs', 'AutoClaw2', 'resources', 'vendor', 'node', 'node.exe'),
    ]
    for (const candidate of candidates) {
      if (fs.existsSync(candidate)) return candidate
    }
    return 'node'
  }

  function scheduleGlmproxyRestart() {
    const config = options()
    if (stopping === true || config.autoStart !== true || config.autoRestart !== true || config.glmproxyEnabled !== true) return
    const now = Date.now()
    glmRestartTimestamps = glmRestartTimestamps.filter((at) => now - at < 3_600_000)
    if (glmRestartTimestamps.length >= 5) {
      logger.warn?.('autoclaw-relay: glmproxy 一小时内已自动重启 5 次，停止重启；排查见 relay.log')
      return
    }
    glmRestartTimestamps.push(now)
    setTimeout(() => {
      if (stopping === false) void ensureGlmproxy().catch((error) => logger.warn?.(`autoclaw-relay: glmproxy 自动重启失败（${error?.message ?? error}）`))
    }, 5000)
  }

  /** 拉起（或复用）glmproxy；relay 的上游就靠它。 */
  async function ensureGlmproxy() {
    const config = options()
    if (config.glmproxyEnabled !== true || config.autoStart !== true || stopping === true) return
    if (glmChild !== undefined) return
    const base = `http://127.0.0.1:${config.glmproxyPort}`
    if (await glmHealthy(base)) {
      logger.info?.(`autoclaw-relay: glmproxy 已在运行，复用 ${base}`)
      return
    }
    const entry = resolveGlmEntry()
    if (entry === null) {
      logger.warn?.('autoclaw-relay: 没找到 glmproxy 入口（npm i -g glmproxy，或用 glmproxyEntry 指定 cli.js），glmproxy 未拉起')
      return
    }
    const nodeBin = resolveGlmNode()
    const userDataRoot = config.glmproxyUserDataRoot !== ''
      ? config.glmproxyUserDataRoot
      : path.join(process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming'), 'AutoClaw-official')
    const out = openLog()
    let proc
    try {
      proc = spawn(nodeBin, [entry], {
        cwd: path.dirname(entry),
        env: {
          ...process.env,
          AUTOCLAW_USER_DATA_ROOT: userDataRoot,
          GLMP_IDENTITY_VERSION: config.glmproxyIdentityVersion,
          UPSTREAM_HOST: config.glmproxyUpstreamHost,
          UPSTREAM_PATH: config.glmproxyUpstreamPath,
          MODEL_CONFIG_PATH: config.glmproxyModelConfigPath,
          PROXY_KEY: config.glmproxyKey,
          PORT: String(config.glmproxyPort),
          HOST: '127.0.0.1',
        },
        stdio: ['ignore', out, out],
        windowsHide: true,
      })
    } catch (error) {
      logger.warn?.(`autoclaw-relay: glmproxy 拉起失败（${error?.message ?? error}）`)
      try { fs.closeSync?.(out) } catch { /* 已关闭 */ }
      return
    }
    glmChild = proc
    proc.on('error', (error) => logger.warn?.(`autoclaw-relay: glmproxy 进程异常（${error?.message ?? error}）`))
    proc.on('exit', (code, signalName) => {
      if (glmChild === proc) glmChild = undefined
      try { fs.closeSync?.(out) } catch { /* 已关闭 */ }
      logger.info?.(`autoclaw-relay: glmproxy 进程退出（code=${code ?? '?'} signal=${signalName ?? '?'}），详见 relay.log`)
      scheduleGlmproxyRestart()
    })
    const deadline = Date.now() + config.glmproxyStartTimeoutMs
    while (Date.now() < deadline) {
      if (await glmHealthy(base)) {
        logger.info?.(`autoclaw-relay: glmproxy 就绪 ${base}（pid=${proc.pid ?? '?'}，上游=${config.glmproxyUpstreamHost}）`)
        return
      }
      if (glmChild === undefined) return
      await new Promise((resolve) => setTimeout(resolve, 400))
    }
    if (glmChild !== undefined) {
      logger.warn?.(`autoclaw-relay: 等待 glmproxy 就绪超时（${config.glmproxyStartTimeoutMs}ms），进程仍在后台启动中`)
    }
  }

  /** 拉起（或复用）本地 relay 进程；就绪探测不阻塞 apply。 */
  async function ensureRelay() {
    const config = options()
    if (config.autoStart !== true || stopping === true) return
    if (child !== undefined) return
    const endpoint = endpointOf(options)
    const base = endpoint.baseURL.replace(/\/v1$/, '')
    // 已有健康实例（用户手动起的、或上一次 DSH 会话残留）→ 复用，不重复占端口
    if (await healthy(base)) {
      logger.info?.(`autoclaw-relay: 本地网关已在运行，直接复用 ${base}`)
      return
    }
    const relayDir = relayDirOf()
    const relayPy = path.join(relayDir, 'relay.py')
    if (fs.existsSync(relayPy) === false) {
      logger.warn?.(`autoclaw-relay: 找不到 ${relayPy}，进程未拉起（可用 relayDir 配置指定目录）`)
      return
    }
    const configPath = seedRuntimeConfig()
    const out = openLog()
    let proc
    try {
      proc = spawn(config.pythonPath, [relayPy, '-c', configPath], {
        cwd: relayDir,
        stdio: ['ignore', out, out],
        windowsHide: true,
      })
    } catch (error) {
      logger.warn?.(`autoclaw-relay: relay 进程拉起失败（${error?.message ?? error}）；确认 pythonPath 指向 Python 3.10+`)
      fs.closeSync?.(out)
      return
    }
    child = proc
    proc.on('error', (error) => {
      logger.warn?.(`autoclaw-relay: relay 进程异常（${error?.message ?? error}）`)
    })
    proc.on('exit', (code, signalName) => {
      if (child === proc) child = undefined
      try {
        fs.closeSync?.(out)
      } catch { /* 已关闭 */ }
      logger.info?.(`autoclaw-relay: relay 进程退出（code=${code ?? '?'} signal=${signalName ?? '?'}），详见 relay.log`)
      scheduleRestart()
    })
    const deadline = Date.now() + config.startTimeoutMs
    while (Date.now() < deadline) {
      if (await healthy(base)) {
        logger.info?.(`autoclaw-relay: 本地网关就绪 ${base}（pid=${proc.pid ?? '?'}）`)
        return
      }
      if (child === undefined) {
        logger.warn?.('autoclaw-relay: relay 进程在就绪前退出，排查见 relay.log')
        return
      }
      await new Promise((resolve) => setTimeout(resolve, 400))
    }
    if (child !== undefined) {
      logger.warn?.(`autoclaw-relay: 等待网关就绪超时（${config.startTimeoutMs}ms），进程仍在后台启动中；模型调用若失败请稍后重试`)
    }
  }

  /** 可选每日签到：一天最多一次；token 走环境变量，不进命令行、不进日志。 */
  function runDailyCheckin() {
    const config = options()
    if (config.dailyCheckin !== true || stopping === true) return
    const statePath = path.join(DEFAULT_RUNTIME_DIR, 'plugin-checkin-state.json')
    const today = new Date().toISOString().slice(0, 10)
    let state = {}
    try {
      state = JSON.parse(fs.readFileSync(statePath, 'utf8'))
    } catch { /* 首次运行 */ }
    if (state?.lastRunDate === today) return
    const relay = readRelayConfig(configPathOf())
    const token = String(relay?.headers?.cookie ?? '')
    if (token === '') {
      logger.warn?.('autoclaw-relay: dailyCheckin 已开启，但 relay config 里没有 headers.cookie，今日签到跳过')
      return
    }
    try {
      fs.mkdirSync(path.dirname(statePath), { recursive: true })
      fs.writeFileSync(statePath, JSON.stringify({ ...state, lastRunDate: today }, null, 2))
    } catch (error) {
      logger.warn?.(`autoclaw-relay: 签到状态写入失败（${error?.message ?? error}），今日跳过`)
      return
    }
    const out = openLog()
    try {
      const proc = spawn(
        config.pythonPath,
        [path.join(relayDirOf(), 'checkin.py'), '--region', config.checkinRegion, '--state', path.join(DEFAULT_RUNTIME_DIR, 'checkin-state.json')],
        {
          cwd: relayDirOf(),
          env: { ...process.env, AC_CHECKIN_TOKEN: token },
          stdio: ['ignore', out, out],
          windowsHide: true,
        },
      )
      proc.on('exit', (code) => logger.info?.(`autoclaw-relay: 每日签到结束（exit=${code ?? '?'}）`))
      proc.on('error', (error) => logger.warn?.(`autoclaw-relay: 签到进程异常（${error?.message ?? error}）`))
    } catch (error) {
      logger.warn?.(`autoclaw-relay: 签到拉起失败（${error?.message ?? error}）`)
    }
  }

  // ── 账号与积分 ──────────────────────────────────────────────────────────────
  /** 读本地网关的账号/积分只读端点（网关带缓存，不打上游风暴）。 */
  async function fetchAccountSummary() {
    const endpoint = endpointOf(options)
    const base = endpoint.baseURL.replace(/\/v1$/, '')
    const headers = { accept: 'application/json' }
    if (endpoint.key !== '') headers.authorization = `Bearer ${endpoint.key}`
    const resp = await fetch(`${base}/api/account`, { headers, signal: AbortSignal.timeout(20_000) })
    if (resp.ok !== true) throw new Error(`HTTP ${resp.status}`)
    return await resp.json()
  }

  // tools 是可选依赖：宿主有 tools 服务时挂上查询工具，没有就只保留 HTTP 端点。
  // 必须用 ctx.inject 而不是 ctx.get —— 后者对未声明 inject 的服务恒返回 undefined。
  ctx.inject(['tools'], (toolCtx) => {
    if (options().enabled !== true) return
    toolCtx.effect(() => toolCtx.tools.register(defineTool({
      name: 'autoclaw_account',
      description:
        '查询本机 AutoClaw 已登录账号的积分余额：总积分、各钱包分桶、即将过期积分、今日签到状态。'
        + '只读，不消耗积分、不触发签到；数据经本地防封网关缓存 5 分钟。',
      parameters: {
        refresh: {
          type: 'boolean',
          description: '要求刷新（网关缓存仍会兜底，避免频繁打上游）',
        },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: true,
          properties: {
            total: { type: 'number', required: true },
            checkedAt: { type: 'number', required: true },
            accounts: { type: 'array', required: true },
          },
        },
        render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }],
      },
      async execute(args) {
        const summary = await fetchAccountSummary()
        const accounts = (summary.accounts ?? []).map((item) => ({
          account: item.account,
          uid: item.uid,
          phone: item.phone,
          total: item.total,
          expiring: item.expiring,
          checkedIn: item.checkedIn,
          wallets: (item.wallets ?? []).filter((w) => w.display === true),
          ...(item.error === undefined ? {} : { error: item.error }),
        }))
        return {
          total: summary.total,
          checkedAt: summary.checkedAt,
          accounts,
          ...(args?.refresh === true ? { refreshRequested: true } : {}),
        }
      },
      presentCall: () => ({ card: 'generic', title: 'autoclaw_account' }),
    })))
  })

  // ── 模型提供方 ──────────────────────────────────────────────────────────────
  const adapter = new AutoClawRelayAdapter({
    options,
    endpoint: () => endpointOf(options),
    resolveAttachments: () => (typeof ctx.get === 'function' ? ctx.get('attachments') : undefined),
    logger,
  })

  ctx.effect(() => () => {
    stopping = true
    try {
      child?.kill()
    } catch { /* 进程已退出 */ }
    try {
      glmChild?.kill()
    } catch { /* 进程已退出 */ }
  }, 'autoclaw-relay: relay + glmproxy 进程')

  if (options().enabled) {
    const providerId = options().provider
    ctx.llm.registerConfigurableProviders?.([{
      provider: providerId,
      displayName: options().displayName,
      settingsNs: entryNs,
      settingsPath: [],
    }])
    ctx.llm.registerAdapter([providerId], adapter)
    // 「检测模型」按钮：清缓存重新拉一次目录（用户显式动作才打 /v1/models）
    ctx.llm.registerModelDiscovery?.(entryNs, async () => {
      await adapter.refreshModels()
      const config = options()
      const list = await adapter.listModels(providerId)
      return list.map((model) => {
        const entry = (config.models ?? []).find((item) => item.id === model.id)
        return {
          id: model.id,
          name: model.name,
          contextWindow: entry?.contextWindow ?? config.defaultContextWindow,
          maxTokens: entry?.maxTokens ?? config.maxTokens,
          inputModalities: model.inputModalities,
        }
      })
    })
    const staticCount = (options().models ?? []).length
    logger.info?.(`autoclaw-relay: provider "${providerId}" 已注册（本地防封网关），模型目录=${staticCount > 0 ? `静态 ${staticCount} 条` : '自动发现（经网关缓存）'}`)
  } else {
    logger.info?.('autoclaw-relay: enabled=false，模型提供方未注册')
  }

  // ── Usage 路由（供客户端 half 轮询账号/积分状态）──────────────────────────
  // 路径 /plugins/dsh-autoclaw-relay/usage 转发到 relay 的 /api/usage
  function isLoopbackReq(req) {
    const addr = req.socket?.localAddress ?? ''
    return addr === '127.0.0.1' || addr === '::1' || addr === '0.0.0.0'
  }
  function sendJson(res, status, body) {
    res.writeHead(status, { 'content-type': 'application/json' })
    res.end(JSON.stringify(body))
  }
  if (typeof ctx.inject === 'function') {
    ctx.inject(['webServer'], (webCtx) => {
      webCtx.effect(() => {
        const disposer = webCtx.webServer.register({
          kind: 'exact',
          path: '/plugins/dsh-autoclaw-relay/usage',
          handler: async (req, res) => {
            if (!isLoopbackReq(req)) {
              sendJson(res, 403, { error: 'host-not-trusted' })
              return
            }
            try {
              const endpoint = endpointOf(options())
              const base = endpoint.baseURL.replace(/\/v1$/, '')
              const resp = await fetch(`${base}/api/usage`, { signal: AbortSignal.timeout(8000) })
              const body = await resp.text()
              res.writeHead(resp.status, { 'content-type': 'application/json' })
              res.end(body)
            } catch (error) {
              sendJson(res, 502, { error: `usage fetch failed: ${error?.message ?? error}` })
            }
          },
        })
        return disposer
      }, 'autoclaw-relay: usage route')
    })
  }

  // 进程托管与签到都不阻塞插件加载：模型调用若早于网关就绪，适配器会给出
  // 明确的 TRANSPORT 错误，重试即可。
  void ensureGlmproxy().catch((error) => logger.warn?.(`autoclaw-relay: glmproxy 启动流程异常（${error?.message ?? error}）`))
  void ensureRelay().catch((error) => logger.warn?.(`autoclaw-relay: 启动流程异常（${error?.message ?? error}）`))
  runDailyCheckin()

  logger.info?.(`autoclaw-relay: 已加载，provider="${options().provider}"，防封默认：noRetry=${options().noRetry} maxConcurrent=${options().maxConcurrent}`)
}

export { AutoClawRelayAdapter }
export default { name, inject, Config, apply }
