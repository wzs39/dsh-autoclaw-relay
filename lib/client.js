/**
 * dsh-autoclaw-relay — 浏览器半身（Client half）。
 *
 * 注册 usage 卡片到 conversation.composer.dock，每 60 秒轮询宿主侧
 * /plugins/dsh-autoclaw-relay/usage 路由（由 index.js 注册的 webServer
 * 路由转发到 relay 的 /api/usage），显示账号列表、冷却状态、签到状态
 * 和 metrics。
 *
 * 宿主以经典脚本加载 client.js（非 ESM），必须走
 * window.__ModuleLoader__.load({ id, factory }) 包装，React 用 require 取。
 *
 * @module client.js
 */

window.__ModuleLoader__.load({
  id: 'dsh-autoclaw-relay',
  factory: require => {
    const module = { exports: {} }
    const exports = module.exports
    const React = require('react')
    const { useEffect, useState } = React

    const name = 'dsh-autoclaw-relay-client'

    /** 只声明宿主必定存在的服务。 */
    const inject = ['slots', 'locale']

    const USAGE_PATH = '/plugins/dsh-autoclaw-relay/usage'
    const POLL_MS = 60_000

    // ── locale ──────────────────────────────────────────────────────────────
    const LOCALE = {
      zh: {
        title: 'AutoClaw 中继状态',
        account: '账号',
        region: '区域',
        cooldown: '冷却中',
        ready: '就绪',
        checkinToday: '已签到',
        checkinFailed: '签到失败',
        requests: '请求',
        errors: '错误',
        enabled: '启用',
        disabled: '停用',
        noAccounts: '无账号',
        updated: '更新于',
      },
      en: {
        title: 'AutoClaw Relay Status',
        account: 'Account',
        region: 'Region',
        cooldown: 'Cooldown',
        ready: 'Ready',
        checkinToday: 'Checked in',
        checkinFailed: 'Checkin failed',
        requests: 'Requests',
        errors: 'Errors',
        enabled: 'Enabled',
        disabled: 'Disabled',
        noAccounts: 'No accounts',
        updated: 'Updated',
      },
    }

    // ── usage 轮询 hook ─────────────────────────────────────────────────────
    async function fetchUsage() {
      try {
        const resp = await fetch(USAGE_PATH, { signal: AbortSignal.timeout(5000) })
        if (!resp.ok) return null
        return await resp.json()
      } catch {
        return null
      }
    }

    // ── UsageCard 组件 ──────────────────────────────────────────────────────
    function fmtTime(ts) {
      if (!ts) return '—'
      const d = new Date(ts * 1000)
      return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    }

    function UsageCard({ locale }) {
      const [usage, setUsage] = useState(null)

      useEffect(() => {
        let timer
        let alive = true
        const load = async () => {
          const data = await fetchUsage()
          if (alive && data) setUsage(data)
          timer = setTimeout(load, POLL_MS)
        }
        load()
        return () => {
          alive = false
          if (timer) clearTimeout(timer)
        }
      }, [])

      if (!usage) return null

      const accounts = usage.accounts ?? []
      const guard = usage.guard ?? {}
      const metrics = usage.metrics ?? {}

      const style = {
        padding: '8px 12px',
        background: 'var(--bg-elevated)',
        border: '1px solid var(--border-subtle)',
        borderRadius: '8px',
        fontSize: '12px',
        color: 'var(--text-secondary)',
        display: 'flex',
        flexDirection: 'column',
        gap: '6px',
      }

      const rowStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: '8px',
      }

      const badgeStyle = (color) => ({
        display: 'inline-block',
        padding: '1px 6px',
        borderRadius: '4px',
        fontSize: '10px',
        fontWeight: 600,
        background: color.bg,
        color: color.fg,
      })

      const cooldownColor = guard.cooldown_remaining_s > 0
        ? { bg: 'rgba(255,100,100,0.15)', fg: '#ff6b6b' }
        : { bg: 'rgba(100,255,100,0.15)', fg: '#6bff6b' }

      return React.createElement('div', { style },
        // 顶部：守卫状态 + 请求数
        React.createElement('div', { style: rowStyle },
          React.createElement('span', null, locale.title),
          guard.cooldown_remaining_s > 0
            ? React.createElement('span', { style: badgeStyle(cooldownColor) }, `${locale.cooldown} ${Math.ceil(guard.cooldown_remaining_s)}s`)
            : React.createElement('span', { style: badgeStyle({ bg: 'rgba(100,255,100,0.15)', fg: '#6bff6b' }) }, locale.ready),
          React.createElement('span', { style: { marginLeft: 'auto', fontSize: '10px', opacity: 0.6 } },
            `${locale.requests} ${metrics.requests ?? 0} · ${locale.errors} ${metrics.blocked_rejections ?? 0}`),
        ),
        // 账号列表
        accounts.length === 0
          ? React.createElement('span', { style: { fontSize: '11px', opacity: 0.5 } }, locale.noAccounts)
          : accounts.map((acc) => {
              const checkin = usage.checkin?.[acc.name] ?? {}
              return React.createElement('div', { key: acc.name, style: { display: 'flex', alignItems: 'center', gap: '8px', fontSize: '11px' } },
                React.createElement('span', { style: { fontWeight: 500 } }, acc.name),
                React.createElement('span', { style: { opacity: 0.5, fontSize: '10px' } }, acc.region),
                React.createElement('span', { style: { marginLeft: 'auto' } },
                  acc.enabled
                    ? React.createElement('span', { style: badgeStyle({ bg: 'rgba(100,200,255,0.15)', fg: '#6bcfff' }) }, locale.enabled)
                    : React.createElement('span', { style: badgeStyle({ bg: 'rgba(150,150,150,0.15)', fg: '#999' }) }, locale.disabled),
                )
              )
            }),
        // 更新时间
        React.createElement('span', { style: { fontSize: '10px', opacity: 0.4, textAlign: 'right' } },
          `${locale.updated} ${fmtTime(usage.ts)}`),
      )
    }

    // ── 挂载 ────────────────────────────────────────────────────────────────
    async function apply(ctx) {
      const locale = ctx.locale
      locale.register?.('dsh-autoclaw-relay', LOCALE)

      // 探测 slots 服务
      const slots = ctx.slots
      if (!slots?.inject) {
        console.warn('[dsh-autoclaw-relay-client] slots service not available, skipping')
        return
      }

      const t = locale.bind?.('dsh-autoclaw-relay') ?? ((key) => LOCALE.zh[key] ?? key)

      // 注册 usage 卡片到 conversation.composer.dock
      slots.inject('conversation.composer.dock', () => {
        const disposer = slots.register({
          name: 'dsh-autoclaw-relay-usage',
          id: 'dsh-autoclaw-relay-usage',
          order: 15,
          locale: 'dsh-autoclaw-relay',
          inject: ['locale'],
        }, (props) => {
          return React.createElement(UsageCard, { locale: t })
        })
        return disposer
      })

      console.info('[dsh-autoclaw-relay-client] usage card registered on conversation.composer.dock')
    }

    exports.name = name
    exports.inject = inject
    exports.apply = apply
    return module.exports
  },
})
