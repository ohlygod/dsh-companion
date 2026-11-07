/** dsh-companion：把 companion_v2 陪伴核心接到 DSH。
 *
 * 结构：DSH(JS) <- NDJSON 管道 -> Python 桥(bridge.py) -> companion_v2 内核。
 * 模型不用第二把 key：Python 反过来向 DSH 请求一次生成，用 DSH 已配置的模型。
 */
import { spawn } from 'node:child_process'
import { existsSync, mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'

export const name = 'dsh-companion'

const TEXT_OUTPUT = {
  schema: {
    type: 'object',
    additionalProperties: false,
    required: ['ok', 'text'],
    properties: {
      ok: { type: 'boolean', description: 'Whether the call succeeded.' },
      text: { type: 'string', description: 'The companion reply, or an error message.' },
    },
  },
  render: (_args, value) => [{ type: 'text', text: String(value?.text ?? '') }],
}

const PLUGIN_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')

/** DSH 自带的 Python：优先用运行时目录，其次 Electron 的 resourcesPath。 */
function dshPythonCandidates() {
  const roots = [
    process.env.DSH_RUNTIME,
    process.env.DSH_HOME ? join(process.env.DSH_HOME, 'dsh-runtimes', 'dsh-primary-runtime') : undefined,
    process.resourcesPath ? join(process.resourcesPath, 'runtime') : undefined,
  ].filter(Boolean)
  return roots.map(root => join(root, 'primary-runtime', 'dependencies', 'python', 'python.exe'))
}

/** 找 Python：显式配置 -> 插件自带 venv -> DSH 自带运行时 -> PATH。 */
const PYTHON_CANDIDATES = [
  process.env.DSH_COMPANION_PYTHON,
  join(PLUGIN_ROOT, '.venv', 'Scripts', 'python.exe'),
  ...dshPythonCandidates(),
].filter(Boolean)

function resolvePython(config) {
  if (config?.python && existsSync(config.python)) return config.python
  for (const candidate of PYTHON_CANDIDATES) {
    if (candidate && existsSync(candidate)) return candidate
  }
  return 'python'
}

export function apply(root, config = {}) {
  const python = resolvePython(config)
  const dataRoot = config.dataRoot || join(homedir(), '.dsh-companion')
  try { mkdirSync(dataRoot, { recursive: true }) } catch { /* 子进程自己会建 */ }

  /** 常驻 Python 桥：一条 stdin/stdout NDJSON 管道，进程内单例。 */
  let child = null
  let ready = null
  const waiting = new Map()
  const modelHandlers = new Set()
  let seq = 0

  const llmBridge = () => {
    const llm = typeof root.get === 'function' ? root.get('llm') : undefined
    if (!llm) throw new Error('LLM 服务不可用：dsh-llm 未启用')
    return llm
  }

  function pickProvider(llm) {
    try {
      const list = llm.listProviders()
      if (list && list.length) {
        const wanted = list.find(item => (item.id || item) === config.provider)
        if (wanted) return wanted.id || wanted
        return list[0].id || list[0]
      }
    } catch { /* 落到下面 */ }
    throw new Error('DSH 没有可用模型 provider')
  }

  async function pickModel(llm, provider) {
    if (config.model) return config.model
    try {
      const models = await llm.listModels(provider)
      if (models && models.length) return models[0].id || models[0]
    } catch { /* 用默认 */ }
    return undefined
  }

  /** 用 DSH 自己配置的模型答一次生成请求。 */
  async function callModel({ system, user }) {
    const llm = llmBridge()
    const provider = pickProvider(llm)
    const model = await pickModel(llm, provider)
    let text = ''
    // 官方消息构造器只在宿主里可用；取不到就用同形状的普通对象，
    // 这样插件在宿主外（自检、迁移）也能跑。
    let userMessage = null
    try {
      const mod = await import('@deepseek-ai/dsh-llm')
      const makeUser = mod.createUserMessage || (mod.default && mod.default.createUserMessage)
      if (typeof makeUser === 'function') {
        userMessage = makeUser({ content: [{ type: 'text', text: user }], source: { kind: 'user' } })
      }
    } catch { /* 宿主外没有这个包 */ }
    if (!userMessage) {
      userMessage = {
        role: 'user',
        content: [{ type: 'text', text: user }],
        source: { kind: 'user' },
        id: 'cmp-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8),
      }
    }

    let gen
    if (typeof llm.prepareCall === 'function') {
      const prepared = await llm.prepareCall({ provider, model }, undefined)
      gen = prepared.stream({ provider, model, system, messages: [userMessage], temperature: 0.8 })
    } else {
      gen = llm.stream({ provider, model, system, messages: [userMessage], temperature: 0.8 })
    }
    for await (const chunk of gen) {
      if (!chunk || typeof chunk !== 'object') continue
      if (chunk.type === 'finish') break
      if (chunk.type === 'text-delta' && typeof chunk.text === 'string') text += chunk.text
      else if (chunk.type === 'block-end' && chunk.block && typeof chunk.block.text === 'string') text += chunk.block.text
      else if (chunk.type === 'raw-text' && typeof chunk.text === 'string') text += chunk.text
    }
    return text
  }

  function startBridge() {
    if (ready) return ready
    ready = new Promise((resolve, reject) => {
      const env = {
        ...process.env,
        PYTHONUTF8: '1',
        PYTHONIOENCODING: 'utf-8',
        DSH_COMPANION_DATA: dataRoot,
        DSH_COMPANION_USER: config.user || 'owner',
      }
      child = spawn(python, [join(PLUGIN_ROOT, 'bridge.py')], {
        cwd: PLUGIN_ROOT, env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
      })
      child.stderr.on('data', chunk => {
        const line = chunk.toString('utf8').trim()
        if (line) console.error('[dsh-companion] ' + line)
      })
      child.on('exit', code => {
        child = null
        ready = null
        for (const [, entry] of waiting) entry.reject(new Error('陪伴桥已退出 code=' + code))
        waiting.clear()
      })
      const lines = createInterface({ input: child.stdout })
      lines.on('line', line => {
        const trimmed = line.trim()
        if (!trimmed) return
        let payload
        try { payload = JSON.parse(trimmed) } catch { return }
        if (payload.kind === 'ready') { resolve(payload); return }
        if (payload.kind === 'model') {
          for (const handler of modelHandlers) handler(payload)
          return
        }
        if (payload.id && waiting.has(payload.id)) {
          const entry = waiting.get(payload.id)
          waiting.delete(payload.id)
          entry.resolve(payload)
        }
      })
      setTimeout(() => reject(new Error('陪伴桥启动超时')), 30000).unref?.()
    })
    return ready
  }

  function request(command) {
    return new Promise(async (resolve, reject) => {
      try { await startBridge() } catch (error) { reject(error); return }
      const id = String(++seq)
      waiting.set(id, { resolve, reject })
      child.stdin.write(JSON.stringify({ id, ...command }) + '\n')
      setTimeout(() => {
        if (waiting.has(id)) {
          waiting.delete(id)
          reject(new Error('等待陪伴核心超时'))
        }
      }, 180000).unref?.()
    })
  }

  // 模型反向请求：Python 要一次生成，这里用 DSH 自己的模型答它。
  modelHandlers.add(async payload => {
    let text = ''
    try {
      text = await callModel(payload)
    } catch (error) {
      console.error('[dsh-companion] ' + ((error && error.message) || error))
    }
    if (child && child.stdin.writable) {
      child.stdin.write(JSON.stringify({ kind: 'model', rid: payload.rid, text }) + '\n')
    }
  })

  root.inject(['tools'], ctx => {
    ctx.tools.register({
      name: 'companion_chat',
      description: '和陪伴角色聊一句。角色会记住之前的对话、有自己的状态和日程；返回角色这次的回复。',
      parameters: {
        type: 'object',
        properties: {
          text: { type: 'string', description: '你要说的话。' },
          conversation: { type: 'string', description: '会话标识，默认 main；不同值等于不同的人设/记忆线。' },
          persona: { type: 'string', description: '本轮的角色设定（人格提示词），留空沿用上次。' },
        },
        required: ['text'],
      },
      execute: async args => {
        const result = await request({
          cmd: 'chat',
          text: String(args?.text ?? ''),
          conversation: String(args?.conversation ?? 'main'),
          prompt: String(args?.persona ?? ''),
        })
        if (!result.ok) return { ok: false, text: `companion 出错：${result.error} ${result.reason ?? ''}` }
        const replies = Array.isArray(result.replies) ? result.replies : []
        return { ok: true, text: replies.length ? replies.join('\n') : '（角色这轮没有出声）' }
      },
      output: TEXT_OUTPUT,
    })

    ctx.tools.register({
      name: 'companion_status',
      description: '看陪伴核心当前的运行状态与最近的对话批次。',
      parameters: {
        type: 'object',
        properties: { conversation: { type: 'string', description: '会话标识，默认 main。' } },
      },
      execute: async args => {
        const result = await request({ cmd: 'status', conversation: String(args?.conversation ?? 'main') })
        return { ok: true, text: JSON.stringify(result, null, 2) }
      },
      output: TEXT_OUTPUT,
    })
  })

  try {
    root.inject(['commands'], ctx => {
      const offs = []
      offs.push(ctx.commands.register({
        name: 'companion-ping',
        description: '检查陪伴桥是否在线',
        handler: async () => {
          const r = await request({ cmd: 'ping' })
          return { kind: r.ok ? 'success' : 'error', text: r.ok ? '陪伴桥在线' : '陪伴桥异常' }
        },
      }))
      offs.push(ctx.commands.register({
        name: 'companion-reset',
        description: '重置 main 会话的陪伴实例（记忆库保留）',
        handler: async () => {
          const r = await request({ cmd: 'reset', conversation: 'main' })
          return { kind: r.ok ? 'success' : 'error', text: r.ok ? '已重置 main' : '重置失败' }
        },
      }))
      return () => { for (const off of offs) { try { off?.() } catch { /* 已卸载 */ } } }
    })
  } catch { /* 命令服务不可用，工具照常 */ }

  root.on?.('dispose', () => {
    if (child) { try { child.stdin.end() } catch { /* 已退出 */ } }
  })
}
