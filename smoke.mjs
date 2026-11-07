/** 插件自检：假装自己是 DSH 宿主，把 apply() 跑一遍。
 *
 * 检查：模块能 import、apply 不抛错、两个工具注册了、命令注册了、
 * 且 companion_chat 能真的走完 Python 桥（模型由本文件伪造应答）。
 */
import { apply, name } from './lib/index.js'

const registered = { tools: [], commands: [] }
const llm = {
  listProviders: () => [{ id: 'buddy' }],
  listModels: async () => [{ id: 'deepseek-v4.1-flash' }],
  stream: async function* () {
    yield { type: 'text-delta', text: '{"action":"text","text":"在的，一直都在。",' }
    yield { type: 'text-delta', text: '"claims":[{"id":"c1","kind":"subjective","statement":"在的","evidence":null}]}' }
    yield { type: 'finish', reason: { kind: 'stop' } }
  },
}

const root = {
  get: key => (key === 'llm' ? llm : undefined),
  on: () => {},
  inject: (deps, factory) => {
    const ctx = {
      tools: { register: spec => registered.tools.push(spec) },
      commands: { register: spec => registered.commands.push(spec) },
    }
    factory(ctx)
  },
}

if (name !== 'dsh-companion') throw new Error('bad name: ' + name)
apply(root, { dataRoot: process.env.SMOKE_DATA })

console.log('tools:', registered.tools.map(t => t.name).join(', '))
console.log('commands:', registered.commands.map(c => c.name).join(', '))
if (registered.tools.length !== 2) throw new Error('expected 2 tools')
if (registered.commands.length !== 2) throw new Error('expected 2 commands')

const chat = registered.tools.find(t => t.name === 'companion_chat')
const result = await chat.execute({ text: '在吗', persona: '你是 YG 的私人陪伴角色。' }, {})
console.log('chat result:', JSON.stringify(result))
if (!result.ok) throw new Error('chat failed: ' + JSON.stringify(result))
if (!String(result.text).includes('在的')) throw new Error('reply was not the model text: ' + result.text)

const status = registered.tools.find(t => t.name === 'companion_status')
const statusResult = await status.execute({ conversation: 'main' }, {})
console.log('status ok:', statusResult.ok)
console.log('SMOKE PASS')
process.exit(0)
