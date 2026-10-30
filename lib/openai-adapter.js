/**
 * AutoClaw Relay 的 OpenAI 兼容适配器（Host 半身）。
 *
 * 职责：把 DSH 的消息/工具词表转成 OpenAI `/v1/chat/completions`，把上游 SSE
 * 流转回 DSH 的流式事件词表（block-start / text-delta / reasoning-delta /
 * tool-call-delta / block-end / usage / finish）。
 *
 * 防封要点（与网关侧五层机制互补，都在插件层落实）：
 *   1. 全部请求只打本地 relay（127.0.0.1 回环），绝不直连上游；
 *   2. `purpose === 'session-title'` 本地短路：会话标题不产生任何上游请求；
 *   3. 模型目录默认走 relay 的 `/v1/models`（relay 自带 5 分钟内存缓存），
 *      resolveModel 只读缓存，绝不逐请求探测上游；
 *   4. 内容哨兵 `DANGEROUS_ACTIVITY_BLOCKED` 命中 → 掐断正文、换人话提示、
 *      finish stop：不让哨兵文本混进会话历史，也不给用户"重试"的暗示；
 *   5. 冷却期（relay 503）错误原样上抛、默认不重试（retryPolicy 由 index.js
 *      按 noRetry 配置压成 maxRetries=0），避免 DSH 在冷却窗口里反复撞门；
 *   6. 并发闸门：同时打到 relay 的流默认不超过 2 条，把 DSH 并行子代理的
 *      请求收成接近人的节奏；
 *   7. attribution 头照 DSH 契约发给 relay（user-agent: deepseek-harness/…），
 *      relay 指纹层会在上游侧剥掉换装——两份契约互不冲突。
 *
 * @module lib/openai-adapter.js
 */

import fs from 'node:fs'

import { LlmAdapter, LlmError, attributionHeaders, offloadedImageText } from '@deepseek-ai/dsh-llm'

/** 上游把封禁提示当 200 正文返回的哨兵串（与 relay/guard.py 保持一致）。 */
const SENTINEL = 'DANGEROUS_ACTIVITY_BLOCKED'

/** 模型目录缓存 TTL：与 relay 的 models_cache_ttl_s 对齐，多余拉取一律不做。 */
const MODEL_CACHE_TTL_MS = 300_000

/**
 * 计数信号量：限制同时打到本地 relay 的流数。
 * 排队期间响应 options.signal 的中止（以 ABORTED 拒绝，不留幽灵占位）。
 */
class Semaphore {
  constructor(limit) {
    this._limit = Math.max(1, limit)
    this._active = 0
    this._queue = []
  }

  /** 占一个槽位；返回释放函数。 */
  async acquire(signal) {
    if (this._active < this._limit) {
      this._active += 1
      return () => this._release()
    }
    const entry = { onAbort: undefined, resolve: undefined, reject: undefined }
    const promise = new Promise((resolve, reject) => {
      entry.resolve = () => {
        if (signal !== undefined && entry.onAbort !== undefined) signal.removeEventListener('abort', entry.onAbort)
        this._active += 1
        resolve()
      }
      entry.reject = reject
      entry.onAbort = () => {
        const at = this._queue.indexOf(entry)
        if (at >= 0) this._queue.splice(at, 1)
        reject(new LlmError('请求被调用方中止（并发排队时）', 'ABORTED'))
      }
    })
    this._queue.push(entry)
    if (signal !== undefined) {
      if (signal.aborted === true) entry.onAbort()
      else signal.addEventListener('abort', entry.onAbort, { once: true })
    }
    await promise
    return () => this._release()
  }

  _release() {
    this._active = Math.max(0, this._active - 1)
    const next = this._queue.shift()
    if (next !== undefined) next.resolve()
  }
}

/** content 既可能是字符串也可能是块数组；取全部 text 块的拼接。 */
function textOf(content) {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content
    .filter((block) => block !== null && typeof block === 'object' && block.type === 'text')
    .map((block) => block.text ?? '')
    .join('')
}

/** 取最后一条有正文的用户消息（session-title 本地短路用）。 */
function lastUserText(messages) {
  const list = Array.isArray(messages) ? messages : []
  for (let index = list.length - 1; index >= 0; index -= 1) {
    if (list[index]?.role === 'user') {
      const text = textOf(list[index].content).trim()
      if (text !== '') return text
    }
  }
  return ''
}

export class AutoClawRelayAdapter extends LlmAdapter {
  /**
   * @param options () => resolveOptions() 的产物（热更新容错在 index.js）
   * @param endpoint () => { baseURL, key }（读 relay 运行配置，用户改端口/密钥即时生效）
   * @param resolveAttachments () => attachments 服务（图片块字节来源）
   * @param logger 结构化日志（绝不打印 gateway_key / cookie）
   */
  constructor({ options, endpoint, resolveAttachments, logger }) {
    super()
    this._options = options
    this._endpoint = endpoint
    this._resolveAttachments = resolveAttachments
    this._logger = logger ?? console
    this._gate = new Semaphore(2)
    this._modelCache = undefined
    this._gateLimit = -1
  }

  /** 并发上限跟随配置（只在数值变化时重建闸门，避免丢排队状态）。 */
  _gateFor(limit) {
    if (this._gateLimit !== limit) {
      this._gate = new Semaphore(limit)
      this._gateLimit = limit
    }
    return this._gate
  }

  providerInfo(provider) {
    return { id: provider, name: this._options().displayName ?? provider }
  }

  /** 防封默认：index.js 已按 noRetry 把策略压成 maxRetries=0，这里只做透传。 */
  providerRetryPolicy() {
    return this._options().retryPolicy
  }

  /** 静态清单优先；否则走 relay 的 /v1/models（带 5 分钟缓存 + 失败静默）。 */
  async listModels(provider) {
    const config = this._options()
    const configured = Array.isArray(config.models) ? config.models : []
    if (configured.length > 0) {
      return configured.map((model) => ({
        provider,
        id: model.id,
        name: model.name ?? model.id,
        inputModalities: [...(model.inputModalities ?? ['text'])],
      }))
    }
    if (this._modelCache !== undefined && Date.now() - this._modelCache.at < MODEL_CACHE_TTL_MS) {
      return this._modelCache.list
    }
    const { baseURL, key, configPath } = this._endpoint()
    if (baseURL === '') return []
    try {
      const headers = { accept: 'application/json' }
      if (key !== '') headers.authorization = `Bearer ${key}`
      const resp = await fetch(`${baseURL}/models`, { headers, signal: AbortSignal.timeout(8000) })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const body = await resp.json()
      const list = Array.isArray(body?.data)
        ? body.data
            .filter((entry) => entry !== null && typeof entry === 'object' && typeof entry.id === 'string' && entry.id !== '')
            .map((entry) => ({
              provider,
              id: entry.id,
              name: typeof entry.name === 'string' && entry.name !== '' ? entry.name : entry.id,
              inputModalities: ['text'],
            }))
        : []
      this._modelCache = { at: Date.now(), list }
      return list
    } catch (error) {
      // 上游没有 /v1/models 端点（如 workbuddy-proxy 只收 POST）时，退回 relay
      // 运行配置里的静态清单（models 字段，与网关同文件、改了即时生效）。
      const fallback = this._modelsFromRelayConfig(provider, configPath)
      if (fallback.length > 0) {
        this._modelCache = { at: Date.now(), list: fallback }
        return fallback
      }
      this._logger.warn?.(`autoclaw-relay: 拉取模型目录失败（${error?.message ?? error}）；本轮返回空目录，不影响已配置模型`)
      return []
    }
  }

  /** 从 relay 运行配置的 models 字段读静态清单（agent 侧零上游流量）。 */
  _modelsFromRelayConfig(provider, configPath) {
    try {
      const relay = JSON.parse(fs.readFileSync(configPath, 'utf8'))
      const models = Array.isArray(relay?.models) ? relay.models : []
      return models
        .filter((entry) => entry !== null && typeof entry === 'object' && typeof entry.id === 'string' && entry.id !== '')
        .map((entry) => ({
          provider,
          id: entry.id,
          name: typeof entry.name === 'string' && entry.name !== '' ? entry.name : entry.id,
          inputModalities: Array.isArray(entry.inputModalities) && entry.inputModalities.length > 0 ? [...entry.inputModalities] : ['text'],
        }))
    } catch {
      return []
    }
  }

  /** 「检测模型」按钮入口：清缓存后重新拉一次。 */
  async refreshModels() {
    this._modelCache = undefined
  }

  /** 只读缓存与静态配置，绝不发起网络请求（防封：模型解析零上游流量）。 */
  async resolveModel(provider, model) {
    const config = this._options()
    const list = this._modelCache?.list ?? []
    const hit = list.find((entry) => entry.id === model)
    const entry = (Array.isArray(config.models) ? config.models : []).find((item) => item.id === model)
    return {
      provider,
      id: model,
      name: hit?.name ?? entry?.name ?? model,
      inputModalities: [...(hit?.inputModalities ?? entry?.inputModalities ?? ['text'])],
      context: { contextWindow: entry?.contextWindow ?? config.defaultContextWindow },
      defaultMaxTokens: entry?.maxTokens ?? config.maxTokens,
    }
  }

  async *stream(options) {
    const config = this._options()
    // 防封②：会话标题本地生成。每个新会话省一次上游请求，标题质量无损。
    if (options.purpose === 'session-title') {
      const title = (lastUserText(options.messages) || '新会话').replace(/\s+/g, ' ').slice(0, 24)
      yield { type: 'block-start', index: 0, blockType: 'text' }
      yield { type: 'text-delta', index: 0, text: title }
      yield { type: 'block-end', index: 0, block: { type: 'text', text: title } }
      yield { type: 'usage', usage: { inputTokens: 0, outputTokens: 0 } }
      yield { type: 'finish', reason: { kind: 'stop' } }
      return
    }
    const release = await this._gateFor(config.maxConcurrent).acquire(options.signal)
    try {
      yield* this._streamLocked(options, config)
    } finally {
      release()
    }
  }

  async *_streamLocked(options, config) {
    this._finishReason = undefined // 上一请求的 finish_reason 不允许漏进这一轮
    const { baseURL, key } = this._endpoint()
    if (baseURL === '') throw new LlmError('AutoClaw Relay 端点未配置（relay config 缺失或不可读）', 'PROVIDER')

    const messages = await this._toOpenAiMessages(options)
    const body = { model: options.model, messages, stream: true, stream_options: { include_usage: true } }
    if (Number.isSafeInteger(options.maxTokens) && options.maxTokens > 0) body.max_tokens = options.maxTokens
    if (typeof options.temperature === 'number' && Number.isFinite(options.temperature)) body.temperature = options.temperature
    const tools = (Array.isArray(options.tools) ? options.tools : [])
      .filter((tool) => tool !== null && typeof tool === 'object' && typeof tool.name === 'string' && tool.name !== '' && tool.deferLoading !== true)
      .map((tool) => ({
        type: 'function',
        function: {
          name: tool.name,
          description: typeof tool.description === 'string' ? tool.description : '',
          parameters: tool.parameters !== null && typeof tool.parameters === 'object' ? tool.parameters : { type: 'object', properties: {} },
        },
      }))
    if (tools.length > 0) body.tools = tools

    const headers = { 'content-type': 'application/json', accept: 'text/event-stream', ...attributionHeadersSafe() }
    if (key !== '') headers.authorization = `Bearer ${key}`

    // 中止链 + 空闲看门狗：options.signal 或空闲超时都会掐断 fetch，最后统一归因。
    const controller = new AbortController()
    let idleFired = false
    const onOuterAbort = () => controller.abort()
    options.signal?.addEventListener('abort', onOuterAbort, { once: true })
    let lastData = Date.now()
    const watchdog = setInterval(() => {
      if (Date.now() - lastData > config.idleTimeoutMs) {
        idleFired = true
        controller.abort()
      }
    }, 5000)

    let resp
    try {
      resp = await fetch(`${baseURL}/chat/completions`, {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
        signal: controller.signal,
      })
    } catch (error) {
      if (options.signal?.aborted === true) throw new LlmError('请求被调用方中止', 'ABORTED')
      if (idleFired) throw new LlmError(`AutoClaw Relay 连接空闲超时（${config.idleTimeoutMs}ms 无数据）`, 'TIMEOUT')
      throw new LlmError(`AutoClaw Relay 连接失败（${error?.cause?.code ?? error?.message ?? error}）；请确认本地网关在跑（GET ${baseURL.replace(/\/v1$/, '')}/healthz）`, 'TRANSPORT')
    }

    if (!resp.ok) {
      const raw = await resp.text().catch(() => '')
      let detail = raw
      try {
        const parsed = JSON.parse(raw)
        if (typeof parsed?.error?.message === 'string' && parsed.error.message !== '') detail = parsed.error.message
      } catch { /* 非 JSON 错误体，原样展示 */ }
      // 冷却期 503 / 402 / 401 等全部原样上抛；重试与否交给 providerRetryPolicy
      //（防封默认 maxRetries=0，冷却窗口内不让 DSH 反复撞门）。
      throw new LlmError(`AutoClaw Relay HTTP ${resp.status}：${detail !== '' ? detail : resp.statusText}`, 'PROVIDER')
    }

    // 流式状态与哨兵拦截器：SSE 与非流式回退两条路共用。
    const toolCalls = new Map()
    let textIndex = -1
    let reasoningIndex = -1
    let blockCounter = 0
    let textRaw = ''
    let textEmitted = ''
    let sentinel = false

    /** 防封④：哨兵串一旦在累计正文里出现，之后所有正文一律不进 DSH。 */
    const emitTextChunk = function* (chunk) {
      textRaw += chunk
      if (sentinel === false && textRaw.includes(SENTINEL)) sentinel = true
      if (sentinel === true) return
      if (textIndex < 0) {
        textIndex = blockCounter
        blockCounter += 1
        yield { type: 'block-start', index: textIndex, blockType: 'text' }
      }
      textEmitted += chunk
      yield { type: 'text-delta', index: textIndex, text: chunk }
    }

    const ctype = String(resp.headers?.get?.('content-type') ?? '')
    if (ctype.includes('text/event-stream') === false) {
      // 上游没按 stream 回（实测：封禁提示就以普通 200 JSON 正文返回）。按非流式
      // chat completion 解析，合成同样的事件序列；哨兵拦截照常生效。
      const raw = await resp.text().catch(() => '')
      clearInterval(watchdog)
      options.signal?.removeEventListener('abort', onOuterAbort)
      let parsed
      try {
        parsed = JSON.parse(raw)
      } catch {
        throw new LlmError(`AutoClaw Relay 返回了非流式、非 JSON 的 200 响应（${raw.slice(0, 120)}）`, 'PROVIDER')
      }
      const message = parsed?.choices?.[0]?.message ?? {}
      const reasoning = typeof message.reasoning_content === 'string' && message.reasoning_content !== '' ? message.reasoning_content : ''
      if (reasoning !== '') {
        reasoningIndex = blockCounter
        blockCounter += 1
        yield { type: 'block-start', index: reasoningIndex, blockType: 'reasoning' }
        yield { type: 'reasoning-delta', index: reasoningIndex, text: reasoning }
      }
      if (typeof message.content === 'string' && message.content !== '') {
        for (const event of emitTextChunk(message.content)) yield event
      }
      for (const call of Array.isArray(message.tool_calls) ? message.tool_calls : []) {
        const entry = { id: typeof call?.id === 'string' && call.id !== '' ? call.id : `call_${blockCounter}`, name: call?.function?.name ?? '', args: typeof call?.function?.arguments === 'string' ? call.function.arguments : '', ourIndex: blockCounter }
        blockCounter += 1
        toolCalls.set(entry.ourIndex, entry)
        yield { type: 'block-start', index: entry.ourIndex, blockType: 'tool-call' }
        yield { type: 'block-end', index: entry.ourIndex, block: { type: 'tool-call', id: entry.id, name: entry.name, arguments: entry.args } }
      }
      for (const closing of this._finishEvents({ toolCalls, textIndex, textEmitted, sentinel, usage: parsed?.usage })) {
        yield closing
      }
      return
    }

    let usage = undefined
    let finishReason = undefined

    /** 一条 SSE data 行 → 0..n 个 DSH 事件（纯同步，靠生成器逐个 yield）。 */
    const handleSseLine = (line) => {
      const events = []
      if (line === '' || line.startsWith(':')) return events
      if (line.startsWith('data:') === false) return events
      const payload = line.slice(5).trim()
      if (payload === '' || payload === '[DONE]') return events
      let parsed
      try {
        parsed = JSON.parse(payload)
      } catch {
        return events
      }
      if (parsed !== null && typeof parsed === 'object' && parsed.usage !== undefined) {
        usage = {
          inputTokens: parsed.usage.prompt_tokens ?? 0,
          outputTokens: parsed.usage.completion_tokens ?? 0,
          ...(parsed.usage.prompt_tokens_details?.cached_tokens > 0
            ? { cacheReadTokens: parsed.usage.prompt_tokens_details.cached_tokens }
            : {}),
        }
      }
      for (const choice of Array.isArray(parsed?.choices) ? parsed.choices : []) {
        const delta = choice?.delta ?? {}
        const reasoning = typeof delta.reasoning_content === 'string' && delta.reasoning_content !== ''
          ? delta.reasoning_content
          : (typeof delta.reasoning === 'string' && delta.reasoning !== '' ? delta.reasoning : '')
        if (reasoning !== '') {
          if (reasoningIndex < 0) {
            reasoningIndex = blockCounter
            blockCounter += 1
            events.push({ type: 'block-start', index: reasoningIndex, blockType: 'reasoning' })
          }
          events.push({ type: 'reasoning-delta', index: reasoningIndex, text: reasoning })
        }
        if (typeof delta.content === 'string' && delta.content !== '') {
          for (const event of emitTextChunk(delta.content)) events.push(event)
        }
        if (Array.isArray(delta.tool_calls)) {
          for (const call of delta.tool_calls) {
            const upstreamIndex = Number.isFinite(call?.index) ? call.index : 0
            let entry = toolCalls.get(upstreamIndex)
            if (entry === undefined) {
              entry = { id: '', name: '', args: '', ourIndex: blockCounter }
              blockCounter += 1
              toolCalls.set(upstreamIndex, entry)
              events.push({ type: 'block-start', index: entry.ourIndex, blockType: 'tool-call' })
            }
            if (typeof call?.id === 'string' && call.id !== '' && entry.id === '') entry.id = call.id
            if (typeof call?.function?.name === 'string' && call.function.name !== '') {
              if (entry.name === '') entry.name = call.function.name
              else if (entry.name !== call.function.name) entry.name += call.function.name
            }
            const argumentsDelta = typeof call?.function?.arguments === 'string' ? call.function.arguments : ''
            if (argumentsDelta !== '') {
              entry.args += argumentsDelta
              events.push({
                type: 'tool-call-delta',
                index: entry.ourIndex,
                id: entry.id !== '' ? entry.id : `call-${upstreamIndex}`,
                ...(entry.name !== '' ? { name: entry.name } : {}),
                argumentsDelta,
              })
            }
          }
        }
        if (typeof choice?.finish_reason === 'string' && choice.finish_reason !== '') {
        finishReason = choice.finish_reason
        this._finishReason = finishReason
      }
      }
      return events
    }

    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done === true) break
        lastData = Date.now()
        buffer += decoder.decode(value, { stream: true })
        let newlineAt = buffer.indexOf('\n')
        while (newlineAt >= 0) {
          const line = buffer.slice(0, newlineAt).replace(/\r$/, '')
          buffer = buffer.slice(newlineAt + 1)
          for (const event of handleSseLine(line)) yield event
          newlineAt = buffer.indexOf('\n')
        }
      }
      buffer += decoder.decode()
      for (const line of buffer.split('\n')) {
        for (const event of handleSseLine(line.replace(/\r$/, ''))) yield event
      }
    } catch (error) {
      if (options.signal?.aborted === true) throw new LlmError('请求被调用方中止', 'ABORTED')
      if (idleFired === true) throw new LlmError(`AutoClaw Relay 流空闲超时（${config.idleTimeoutMs}ms 无数据），本轮中止`, 'TIMEOUT')
      throw new LlmError(`AutoClaw Relay 流中断（${error?.message ?? error}）`, 'TRANSPORT')
    } finally {
      clearInterval(watchdog)
      options.signal?.removeEventListener('abort', onOuterAbort)
      if (reader !== undefined) reader.cancel().catch(() => {})
    }

    for (const event of this._finishEvents({ toolCalls, textIndex, textEmitted, sentinel, usage })) {
      yield event
    }
  }

  /** 收尾事件：哨兵提示替换、块终结、usage 与 finish。SSE 与非流式两路共用。 */
  _finishEvents({ toolCalls, textIndex, textEmitted, sentinel, usage }) {
    const events = []
    let counter = (toolCalls instanceof Map && toolCalls.size > 0)
      ? Math.max(...[...toolCalls.values()].map((entry) => entry.ourIndex)) + 1
      : (textIndex >= 0 ? textIndex + 1 : 0)
    // 哨兵收尾：正文换成一句可读提示（已流出去的干净前缀保留），finish stop。
    if (sentinel === true) {
      const notice = '〔AutoClaw Relay〕内容哨兵命中：上游风控拦截了本轮请求，守卫已进入冷却。请勿手动重试，冷却结束后自动恢复。'
      if (textIndex < 0) {
        textIndex = counter
        counter += 1
        events.push({ type: 'block-start', index: textIndex, blockType: 'text' })
      }
      const prefix = textEmitted === '' ? '' : '\n\n'
      textEmitted += prefix + notice
      events.push({ type: 'text-delta', index: textIndex, text: prefix + notice })
    }
    if (textIndex >= 0) {
      events.push({ type: 'block-end', index: textIndex, block: { type: 'text', text: textEmitted } })
    }
    for (const [upstreamIndex, entry] of toolCalls) {
      events.push({
        type: 'block-end',
        index: entry.ourIndex,
        block: {
          type: 'tool-call',
          id: entry.id !== '' ? entry.id : `call-${upstreamIndex}`,
          name: entry.name,
          arguments: entry.args,
        },
      })
    }
    const resolvedUsage = usage !== undefined
      ? {
          inputTokens: usage.prompt_tokens ?? 0,
          outputTokens: usage.completion_tokens ?? 0,
          ...(usage.prompt_tokens_details?.cached_tokens > 0 ? { cacheReadTokens: usage.prompt_tokens_details.cached_tokens } : {}),
        }
      : { inputTokens: 0, outputTokens: 0 }
    events.push({ type: 'usage', usage: resolvedUsage })
    if (this._finishReason === 'length') {
      events.push({ type: 'finish', reason: { kind: 'max-tokens' } })
    } else {
      events.push({ type: 'finish', reason: { kind: 'stop' } })
    }
    return events
  }

  /** DSH 消息词表 → OpenAI chat messages。 */
  async _toOpenAiMessages(options) {
    const config = this._options()
    const attachments = typeof this._resolveAttachments === 'function' ? this._resolveAttachments() : undefined
    const configured = Array.isArray(config.models) ? config.models : []
    const modality = configured.find((entry) => entry.id === options.model)?.inputModalities
      ?? this._modelCache?.list?.find((entry) => entry.id === options.model)?.inputModalities
      ?? ['text']
    const supportsImages = modality.includes('image')

    const out = []
    const systemParts = []
    if (typeof options.system === 'string' && options.system.trim() !== '') systemParts.push(options.system)
    const list = Array.isArray(options.messages) ? options.messages : []
    let index = 0
    // 开头的 system/developer 消息折进 system 提示（与 pi-ai 的 splitSystemPrompt 同语义）
    while (index < list.length && (list[index]?.role === 'system' || list[index]?.role === 'developer')) {
      const text = textOf(list[index]?.content)
      if (text.trim() !== '') systemParts.push(text)
      index += 1
    }
    if (systemParts.length > 0) out.push({ role: 'system', content: systemParts.join('\n\n') })

    for (; index < list.length; index += 1) {
      const message = list[index]
      const role = message?.role
      const content = message?.content
      if (role === 'system' || role === 'developer') {
        const text = textOf(content)
        if (text.trim() !== '') out.push({ role: 'system', content: text })
        continue
      }
      if (role === 'tool') {
        out.push({
          role: 'tool',
          tool_call_id: typeof message.toolCallId === 'string' ? message.toolCallId : '',
          content: textOf(content),
        })
        continue
      }
      if (role === 'assistant') {
        const blocks = Array.isArray(content) ? content : []
        const text = typeof content === 'string' ? content : textOf(blocks)
        const calls = blocks
          .filter((block) => block !== null && typeof block === 'object' && block.type === 'tool-call')
          .map((block, position) => ({
            id: typeof block.id === 'string' && block.id !== '' ? block.id : `call_hist_${index}_${position}`,
            type: 'function',
            function: {
              name: block.name ?? '',
              arguments: typeof block.arguments === 'string' ? block.arguments : JSON.stringify(block.arguments ?? {}),
            },
          }))
        if (calls.length > 0) out.push({ role: 'assistant', content: text !== '' ? text : null, tool_calls: calls })
        else if (text !== '') out.push({ role: 'assistant', content: text })
        continue
      }
      if (role === 'user') {
        const parts = []
        const toolResults = []
        const blocks = Array.isArray(content) ? content : (typeof content === 'string' ? [{ type: 'text', text: content }] : [])
        for (const block of blocks) {
          if (block === null || typeof block !== 'object') continue
          if (block.type === 'text') {
            if ((block.text ?? '') !== '') parts.push({ type: 'text', text: block.text })
            continue
          }
          if (block.type === 'image') {
            if (supportsImages === true && attachments !== undefined) parts.push(await this._imagePart(block, attachments))
            else parts.push({ type: 'text', text: block.offloaded === true ? offloadedImageText(block.attachment) : '[图片]' })
            continue
          }
          if (block.type === 'tool-result') {
            toolResults.push(block)
            continue
          }
          // reasoning / tool-call 等块不出现在用户消息里，出现则忽略
        }
        // 工具结果先于正文（OpenAI 要求 role:'tool' 紧跟对应 assistant.tool_calls）
        for (const result of toolResults) {
          out.push({
            role: 'tool',
            tool_call_id: typeof result.toolCallId === 'string' && result.toolCallId !== ''
              ? result.toolCallId
              : (typeof result.callId === 'string' ? result.callId : ''),
            content: textOf(result.content),
          })
        }
        if (parts.length === 1 && parts[0].type === 'text') out.push({ role: 'user', content: parts[0].text })
        else if (parts.length > 1) out.push({ role: 'user', content: parts })
        else if (toolResults.length === 0) out.push({ role: 'user', content: '' })
        continue
      }
      const text = textOf(content)
      if (text !== '') out.push({ role: 'user', content: text })
    }
    return out
  }

  async _imagePart(block, attachments) {
    try {
      if (block.offloaded === true) return { type: 'text', text: offloadedImageText(block.attachment) }
      const stored = await attachments.readImage(block.attachment)
      const mediaType = stored?.ref?.mediaType ?? 'image/png'
      const base64 = Buffer.from(stored.data).toString('base64')
      return { type: 'image_url', image_url: { url: `data:${mediaType};base64,${base64}` } }
    } catch (error) {
      this._logger.warn?.(`autoclaw-relay: 图片附件读取失败（${error?.message ?? error}），降级为占位文本`)
      return { type: 'text', text: '[图片（读取失败）]' }
    }
  }
}

/**
 * attribution 头照 DSH 契约发给 relay（深追问见文件头注释第 7 条）：
 * `user-agent: deepseek-harness/<version> (+url)`。relay 指纹层会在上游侧剥掉
 * 换装，两份契约互不冲突。异常时退回同值手写，契约不缺位。
 */
function attributionHeadersSafe() {
  if (typeof attributionHeaders === 'function') {
    try {
      return attributionHeaders()
    } catch {
      /* 落到兜底 */
    }
  }
  return { 'user-agent': 'deepseek-harness (+https://github.com/deepseek-ai/deepseek-harness)' }
}
