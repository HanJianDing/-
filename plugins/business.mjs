import { appendFileSync, readFileSync } from 'node:fs'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'enterprise-business'
export const inject = ['tools', 'subagents']
const queryParameters = {
  start_date: { type: 'string', required: true, description: 'Inclusive start date YYYY-MM-DD' },
  end_date: { type: 'string', required: true, description: 'Inclusive end date YYYY-MM-DD' },
  region: { type: 'string', required: true, enum: ['all', 'east', 'south', 'north'] },
}
const isChild = agent => agent?.session.header.parentSession !== undefined
const role = agent => agent?.session.header.agentPreset ?? 'business-analysis'
const textOutput = { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] }
const domains = {
  sales: { preset: 'business-analysis', label: '经营分析子Agent', description: 'Analyze sales, revenue, gross profit, regions and products. Amounts are integer CNY cents.' },
  inventory: { preset: 'inventory-analysis', label: '库存分析子Agent', description: 'Analyze the inventory snapshot, available stock, safety-stock shortages and stock valuation. Quantities are units, NOT order counts.' },
}

export function apply(ctx) {
  const base = new URL(process.env.SALES_API_URL ?? 'http://127.0.0.1:8091')
  if (base.protocol !== 'http:' || !['127.0.0.1', '127.0.0.2', 'localhost'].includes(base.hostname)) throw new Error('Business service must be a local HTTP endpoint')
  const selection = process.env.ENTERPRISE_ANALYSIS_TYPE ?? 'sales'
  const checkpoints = process.env.ENTERPRISE_CHECKPOINTS ? JSON.parse(readFileSync(process.env.ENTERPRISE_CHECKPOINTS, 'utf8')) : {}
  const purchaseContext = process.env.ENTERPRISE_PROCUREMENT_CONTEXT ? JSON.parse(readFileSync(process.env.ENTERPRISE_PROCUREMENT_CONTEXT, 'utf8')) : null
  const trace = data => {
    if (process.env.ENTERPRISE_TRACE) appendFileSync(process.env.ENTERPRISE_TRACE,
      JSON.stringify({ time: new Date().toISOString(), ...data }) + '\n', 'utf8')
  }
  const delegated = new WeakMap()
  ctx.tools.register(defineTool({name:'get_replenishment_context',description:'Read deterministic risk thresholds, evaluated risks and transfer candidates. Read-only; never approves or executes purchases.',parameters:{},output:textOutput,
    async execute(_args,exec){if(!isChild(exec.agent)||role(exec.agent)!=='procurement-analysis'||!purchaseContext?.trigger)throw new Error('No procurement authority');return JSON.stringify(purchaseContext)},
  }),{when:({agent})=>isChild(agent)&&role(agent)==='procurement-analysis'&&!!purchaseContext?.trigger})
  const purchaseRuns=new WeakMap()
  async function runPurchase(args,exec){
    if(!purchaseContext?.trigger)return ''
    if(purchaseRuns.has(exec.agent))return purchaseRuns.get(exec.agent)
    const run=(async()=>{
      const cached=checkpoints.procurement
      if(cached && Object.keys(queryParameters).every(k=>cached.data.query[k]===args[k])){
        trace({event:'checkpoint_reused',domain:'procurement',session_id:exec.agent.id,child_session_id:cached.session_id});return cached.analysis
      }
      trace({event:'delegation_started',domain:'procurement',session_id:exec.agent.id,query:args})
      const activation=await ctx.subagents.startActivation({provider:'enterprise-spawn',label:'采购分析子Agent',delivery:'caller',signal:exec.signal,
        request:{parent:exec.agent,maxDepth:1,agentPreset:'procurement-analysis',prompt:[{type:'text',text:'程序风险门控已允许生成采购方案。必须查询库存、销售和风险上下文，参数原样使用 '+JSON.stringify(args)+'。只做方案建议，不提交、不审批、不执行采购。'}]}})
      trace({event:'child_started',domain:'procurement',session_id:exec.agent.id,child_session_id:activation.childId})
      const abort=()=>{void activation.dispose().catch(()=>{})};exec.signal.addEventListener('abort',abort,{once:true})
      try{const result=await activation.result;if(result.stopReason!=='completed')throw new Error('Procurement subagent did not complete')
        const analysis=result.output.filter(b=>b.type==='text').map(b=>b.text).join('\n');if(!analysis.trim())throw new Error('Empty procurement report')
        trace({event:'child_completed',domain:'procurement',session_id:exec.agent.id,child_session_id:activation.childId,analysis});return analysis
      }finally{exec.signal.removeEventListener('abort',abort);await activation.dispose()}
    })();purchaseRuns.set(exec.agent,run);return run
  }
  for (const [domain, spec] of Object.entries(domains)) {
    const admitsQuery = agent => isChild(agent) && (role(agent) === spec.preset || (role(agent)==='procurement-analysis'&&!!purchaseContext?.trigger))
    const admitsDelegation = agent => !isChild(agent) && agent?.session.header.agentPreset !== 'enterprise-assistant' && (['combined', 'auto'].includes(selection) || selection === domain || (selection==='procurement'&&domain==='inventory'))
    ctx.tools.register(defineTool({
      name: `query_${domain}`, description: `Query simulated ${domain} data from FastAPI. ${spec.description} Empty rows mean no data.`,
      parameters: queryParameters, output: textOutput,
      async execute(args, exec) {
        if (!admitsQuery(exec.agent)) throw new Error('Only the matching specialist may query this dataset')
        const url = new URL(`/api/${domain}`, base)
        Object.entries(args).forEach(([key, value]) => url.searchParams.set(key, value))
        const headers = process.env.ENTERPRISE_TOOL_TOKEN ? { Authorization: `Bearer ${process.env.ENTERPRISE_TOOL_TOKEN}` } : {}
        const response = await fetch(url, { headers, signal: AbortSignal.any([exec.signal, AbortSignal.timeout(10000)]) })
        if (!response.ok) throw new Error(`${domain} API returned HTTP ${response.status}`)
        const data = await response.json()
        const metric = domain === 'sales' ? data.totals?.revenue_cents : data.totals?.on_hand
        if (data.source !== `simulated-${domain}-v1` || data.amount_unit !== 'cent' || !Array.isArray(data.rows)
          || !Number.isSafeInteger(metric)) throw new Error(`Invalid ${domain} response`)
        trace({ event: `${domain}_queried`, domain, session_id: exec.agent.id,
          parent_session_id: exec.agent.session.header.parentSession, [domain]: data })
        return JSON.stringify(data)
      },
    }), { when: ({ agent }) => admitsQuery(agent) })
    ctx.tools.register(defineTool({
      name: `analyze_${domain}`, description: `Delegate to a real Harness specialist and wait for its final result. Call at most once. ${spec.description}`,
      parameters: queryParameters, output: textOutput,
      async execute(args, exec) {
        if (!exec.agent || !admitsDelegation(exec.agent)) throw new Error('Delegation not authorized for this agent/task')
        const used = delegated.get(exec.agent) ?? new Set()
        if (used.has(domain)) throw new Error('This specialist was already delegated; use the existing result')
        used.add(domain)
        delegated.set(exec.agent, used)
        const checkpoint = checkpoints[domain]
        if (checkpoint && Object.keys(queryParameters).every(key => checkpoint.data.query[key] === args[key])) {
          trace({ event: 'checkpoint_reused', domain, session_id: exec.agent.id, child_session_id: checkpoint.session_id })
          const purchase=domain==='inventory'?await runPurchase(args,exec):''
          return checkpoint.analysis+(purchase?'\n采购专业建议：\n'+purchase:'')
        }
        trace({ event: 'delegation_started', domain, session_id: exec.agent.id, query: args })
        const activation = await ctx.subagents.startActivation({
          provider: 'enterprise-spawn', label: spec.label, delivery: 'caller', signal: exec.signal,
          request: { parent: exec.agent, maxDepth: 1, agentPreset: spec.preset,
            prompt: [{ type: 'text', text: `你是${spec.label}。必须调用 query_${domain}，参数原样使用 `
              + JSON.stringify(args) + '。根据接口实际数据完成分析，最终直接返回中文报告。注明模拟数据与数据边界，不再委派。' }],
          },
        })
        trace({ event: 'child_started', domain, session_id: exec.agent.id, child_session_id: activation.childId })
        const abort = () => { void activation.dispose().catch(error => { trace({ event: 'child_cancel_failed', domain, error: error.name }) }) }
        exec.signal.addEventListener('abort', abort, { once: true })
        if (exec.signal.aborted) abort()
        try {
          const result = await activation.result
          if (result.stopReason !== 'completed') throw new Error(`Subagent failed: ${result.stopReason}`)
          const analysis = result.output.filter(block => block.type === 'text').map(block => block.text).join('\n')
          if (!analysis.trim()) throw new Error('Subagent returned no analysis')
          trace({ event: 'child_completed', domain, session_id: exec.agent.id, child_session_id: activation.childId, analysis })
          const purchase=domain==='inventory'?await runPurchase(args,exec):''
          return analysis+(purchase?'\n采购专业建议：\n'+purchase:'')
        } finally {
          exec.signal.removeEventListener('abort', abort)
          await activation.dispose()
        }
      },
    }), { when: ({ agent }) => !isChild(agent) && admitsDelegation(agent) })
  }
}
