// Thin submission bridge. Existing durable Worker and business Agent chain do all analysis.
import { defineTool } from '@deepseek-ai/dsh-tools'
export const name = 'enterprise-assistant-tasks'
export const inject = ['tools']
export function apply(ctx) {
  const base = new URL(process.env.SALES_API_URL ?? 'http://127.0.0.1:8091')
  if (base.protocol !== 'http:' || !['localhost', '127.0.0.1', '127.0.0.2'].includes(base.hostname)) throw new Error('Assistant service must be local')
  const authorized = agent => process.env.ENTERPRISE_ANALYSIS_TYPE === 'chat'
    && process.env.ENTERPRISE_CHAT_CAN_SUBMIT === '1' && !!process.env.ENTERPRISE_CHAT_TOKEN
    && agent?.session.header.parentSession === undefined
  ctx.tools.register(defineTool({
    name: 'submit_analysis_task',
    description: 'Submit one sales/inventory analysis task using the existing main Agent and specialists. Returns durable task ID and approval state immediately, NOT an analysis result. One task per user turn; retries return the same task. Use explicit requested dates and region or inherit the latest conversation task. Do not submit for mere explanations of existing reports. Analyst submissions require approval.',
    parameters: {
      start_date: { type: 'string', required: true, description: 'Inclusive YYYY-MM-DD' },
      end_date: { type: 'string', required: true, description: 'Inclusive YYYY-MM-DD; use the last day for a whole month' },
      region: { type: 'string', required: true, enum: ['all','east','south','north'] },
      analysis_type: { type: 'string', required: true, enum: ['sales','inventory','combined','auto','procurement'] },
      question: { type: 'string', required: true, description: 'Self-contained business request with inherited context, max 2000 characters. No fabricated data.' },
    },
    output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
    async execute(args, exec) {
      if (!authorized(exec.agent)) throw new Error('Task submission is not authorized')
      const response = await fetch(new URL('/api/internal/assistant/tasks', base), {
        method: 'POST', headers: { 'Content-Type':'application/json', Authorization:`Bearer ${process.env.ENTERPRISE_CHAT_TOKEN}` },
        body: JSON.stringify(args), signal: AbortSignal.any([exec.signal, AbortSignal.timeout(10000)]),
      })
      if (!response.ok) throw new Error(`Task submission rejected: HTTP ${response.status}`)
      return JSON.stringify(await response.json())
    },
  }), { when: ({agent}) => authorized(agent) })
  const canEdit = agent => authorized(agent) && process.env.ENTERPRISE_CHAT_CAN_PURCHASE === '1'
  ctx.tools.register(defineTool({name:'revise_replenishment_plan',description:'Revise quantities of an existing replenishment proposal in this conversation. Does not modify an already created purchase draft and never submits or approves. Include all retained items.',
    parameters:{plan_id:{type:'string',required:true},expected_current_revision:{type:'number',required:true,description:'The CURRENT revision from the server plan snapshot. Copy it exactly. Do NOT increment; the backend creates the next revision.'},items_json:{type:'string',required:true,description:'JSON array of {product_id,warehouse,quantity}; quantity positive integer.'}},
    output:{schema:{type:'string'},render:(_args,value)=>[{type:'text',text:value}]},
    async execute(args,exec){if(!canEdit(exec.agent))throw new Error('No proposal edit permission')
      const response=await fetch(new URL('/api/internal/assistant/plan',base),{method:'POST',headers:{'Content-Type':'application/json',Authorization:`Bearer ${process.env.ENTERPRISE_CHAT_TOKEN}`},body:JSON.stringify({plan_id:args.plan_id,revision:args.expected_current_revision,items:JSON.parse(args.items_json)}),signal:AbortSignal.any([exec.signal,AbortSignal.timeout(10000)])})
      if(!response.ok)throw new Error(`Proposal edit rejected: HTTP ${response.status}`);return JSON.stringify(await response.json())
    }
  }),{when:({agent})=>canEdit(agent)})
  ctx.tools.register(defineTool({name:'select_replenishment_option',description:'Only when the current user explicitly says 选择/改为/采用/就用 plus 内部调拨/外部采购/暂不处理, record that proposal choice. External creates a DRAFT only. Never submit or approve. Ambiguous questions must use the UI.',
    parameters:{plan_id:{type:'string',required:true},expected_current_revision:{type:'number',required:true,description:'Copy current plan revision exactly, not the next revision.'},choice:{type:'string',required:true,enum:['transfer','external','defer']}},
    output:{schema:{type:'string'},render:(_args,value)=>[{type:'text',text:value}]},
    async execute(args,exec){if(!canEdit(exec.agent))throw new Error('No proposal permission')
      const response=await fetch(new URL('/api/internal/assistant/plan-choice',base),{method:'POST',headers:{'Content-Type':'application/json',Authorization:`Bearer ${process.env.ENTERPRISE_CHAT_TOKEN}`},body:JSON.stringify({plan_id:args.plan_id,revision:args.expected_current_revision,choice:args.choice}),signal:AbortSignal.any([exec.signal,AbortSignal.timeout(10000)])})
      if(!response.ok)throw new Error(`Proposal choice rejected: HTTP ${response.status}`);return JSON.stringify(await response.json())
    }
  }),{when:({agent})=>canEdit(agent)})
}
