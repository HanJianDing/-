import test from 'node:test'
import assert from 'node:assert/strict'
import { apply } from '../plugins/business.mjs'
import { writeFileSync, unlinkSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'

test('role conditions exclude root sales access and child delegation', () => {
  const tools = new Map()
  apply({ tools: { register: (definition, options) => tools.set(definition.name, { definition, options }) }, subagents: {} })
  const root = { id: 'root', session: { header: {} } }
  const child = { id: 'child', session: { header: { parentSession: 'root' } } }
  assert.equal(tools.get('query_sales').options.when({ agent: root }), false)
  assert.equal(tools.get('query_sales').options.when({ agent: child }), true)
  assert.equal(tools.get('analyze_sales').options.when({ agent: root }), true)
  assert.equal(tools.get('analyze_sales').options.when({ agent: child }), false)
})

test('sales API failure is surfaced, not replaced with fabricated data', async () => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = async () => ({ ok: false, status: 503 })
  const tools = new Map()
  apply({ tools: { register: (definition) => tools.set(definition.name, definition) }, subagents: {} })
  try {
    await assert.rejects(tools.get('query_sales').execute(
      { start_date: '2026-10-01', end_date: '2026-10-31', region: 'all' },
      { agent: { id: 'child', session: { header: { parentSession: 'root' } } }, signal: new AbortController().signal }), /503/)
  } finally { globalThis.fetch = originalFetch }
})

test('specialists cannot query each others data and only root delegates inventory', () => {
  const old = process.env.ENTERPRISE_ANALYSIS_TYPE
  process.env.ENTERPRISE_ANALYSIS_TYPE = 'combined'
  try {
    const tools = new Map()
    apply({ tools: { register: (definition, options) => tools.set(definition.name, { definition, options }) }, subagents: {} })
    const agent = preset => ({ session: { header: { parentSession: 'root', agentPreset: preset } } })
    const inventory = agent('inventory-analysis'), sales = agent('business-analysis')
    assert.equal(tools.get('query_inventory').options.when({ agent: inventory }), true)
    assert.equal(tools.get('query_sales').options.when({ agent: inventory }), false)
    assert.equal(tools.get('query_inventory').options.when({ agent: sales }), false)
    assert.equal(tools.get('analyze_inventory').options.when({ agent: inventory }), false)
    assert.equal(tools.get('analyze_inventory').options.when({ agent: { session: { header: {} } } }), true)
  } finally { if (old === undefined) delete process.env.ENTERPRISE_ANALYSIS_TYPE; else process.env.ENTERPRISE_ANALYSIS_TYPE = old }
})

test('assistant has no delegation or query authority', () => {
  const tools = new Map()
  apply({ tools: { register: (definition, options) => tools.set(definition.name, options) }, subagents: {} })
  const agent = { session: { header: { agentPreset: 'enterprise-assistant' } } }
  for (const options of tools.values()) assert.equal(options.when({ agent }), false)
})

test('persisted specialist checkpoint is reused without starting another child', async () => {
  const path = join(tmpdir(), `enterprise-checkpoint-${randomUUID()}.json`)
  const previous = process.env.ENTERPRISE_CHECKPOINTS
  const query = { start_date: '2026-10-01', end_date: '2026-10-31', region: 'all' }
  writeFileSync(path, JSON.stringify({ sales: { session_id: 'original-child', analysis: 'persisted report', data: { query } } }))
  process.env.ENTERPRISE_CHECKPOINTS = path
  try {
    const tools = new Map()
    apply({ tools: { register: definition => tools.set(definition.name, definition) },
      subagents: { startActivation: () => { throw new Error('must not start a new child') } } })
    const agent = { id: 'new-attempt', session: { header: { agentPreset: 'enterprise-main' } } }
    const result = await tools.get('analyze_sales').execute(query, { agent, signal: new AbortController().signal })
    assert.equal(result, 'persisted report')
  } finally {
    if (previous === undefined) delete process.env.ENTERPRISE_CHECKPOINTS
    else process.env.ENTERPRISE_CHECKPOINTS = previous
    unlinkSync(path)
  }
})
