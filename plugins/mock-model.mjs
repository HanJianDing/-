// Test-only deterministic model. Agent loop, delegation, HTTP calls and persistence stay real.
import { LlmAdapter } from '@deepseek-ai/dsh-llm'
export const name = 'enterprise-test-model'
export const inject = ['llm']

class TestAdapter extends LlmAdapter {
  async *stream(options) {
    options.signal?.throwIfAborted()
    const assistantTool = options.tools?.find(t => t.name === 'submit_analysis_task')
    if (assistantTool) {
      const lastUserIndex = options.messages.findLastIndex(m => m.role === 'user' && !m.content.some(b => b.text?.startsWith('Current runtime context.')))
      const content = options.messages[lastUserIndex]?.content.map(b=>b.text??'').join('') ?? ''
      const question = content.split('\n\n服务端提供')[0]
      const priorResults = options.messages.slice(lastUserIndex+1).filter(m=>m.role==='tool')
      const needsTask = /分析.*(销售|库存)|那库存|只看东部|联合.*销售|补货|采购建议/.test(question)
      if (needsTask || priorResults.length) {
        const last = priorResults.at(-1)
        const block = last ? {type:'text',text:'任务已提交：'+last.content.map(b=>b.text??'').join('')} : {
          type:'tool-call',id:`submit-${options.sessionId}-${lastUserIndex}`,name:'submit_analysis_task',arguments:{
            start_date:[...content.matchAll(/"start_date":\s*"([\d-]+)"/g)].at(-1)?.[1]??'2026-10-01',
            end_date:[...content.matchAll(/"end_date":\s*"([\d-]+)"/g)].at(-1)?.[1]??'2026-10-31',
            region:question.includes('东部')?'east':([...content.matchAll(/"region":\s*"(all|east|south|north)"/g)].at(-1)?.[1]??'all'),
            analysis_type:/补货|采购建议/.test(question)?'procurement':question.includes('库存')?(question.includes('销售')?'combined':'inventory'):'sales',question,
          }}
        if (block.type === 'tool-call') block.arguments = JSON.stringify(block.arguments)
        yield {type:'block-start',index:0,blockType:block.type}
        if(block.type==='text')yield {type:'text-delta',index:0,text:block.text}
        else yield {type:'tool-call-delta',index:0,id:block.id,name:block.name,argumentsDelta:block.arguments}
        yield {type:'block-end',index:0,block}
        yield {type:'finish',reason:{kind:block.type==='text'?'stop':'tool-calls'}}
        return
      }
    }
    const tools = options.tools?.filter(t => ['analyze_sales', 'analyze_inventory', 'query_sales', 'query_inventory','get_replenishment_context'].includes(t.name)) ?? []
    const procurement = tools.some(t=>t.name==='get_replenishment_context')
    const tool = tools[0]
    const results = options.messages.filter(m => m.role === 'tool')
    if (!tool) {
      const text = '测试助手历史：' + options.messages.filter(m => m.role === 'user').map(m => m.content.map(b => b.text ?? '').join('')).join('；')
      yield { type: 'block-start', index: 0, blockType: 'text' }
      yield { type: 'text-delta', index: 0, text }
      yield { type: 'block-end', index: 0, block: { type: 'text', text } }
      yield { type: 'finish', reason: { kind: 'stop' } }
      return
    }
    const delegates = tools.filter(t => t.name.startsWith('analyze_'))
    const next = procurement ? tools[results.length] : delegates[results.length] ?? (results.length === 0 ? tool : null)
    let block
    if (next) {
      if (!tool) throw new Error('Expected business tool unavailable')
      const user = options.messages.filter(m => m.role === 'user').map(m => JSON.stringify(m.content)).join('\n')
      const unescaped = user.replaceAll('\\"', '"')
      const start = unescaped.match(/"start_date"\s*:\s*"([\d-]+)"/)?.[1] ?? '2026-10-01'
      const end = unescaped.match(/"end_date"\s*:\s*"([\d-]+)"/)?.[1] ?? '2026-10-31'
      const region = unescaped.match(/"region"\s*:\s*"(all|east|south|north)"/)?.[1] ?? 'all'
      block = { type: 'tool-call', id: `call-${options.sessionId}-${results.length}`, name: next.name,
        arguments: next.name==='get_replenishment_context'?{}:{ start_date: start, end_date: end, region } }
    } else {
      const result = results.at(-1)
      if (result.isError) throw new Error('Business tool failed in deterministic integration test')
      const text = result.content.map(b => b.text ?? '').join('\n')
      if (procurement) {
        block={type:'text',text:'模拟采购建议：使用已查询库存、销售和程序风险结果。可选择内部调拨、外部采购或暂不处理；仅建议，未执行业务。'}
      } else if (tool?.name === 'query_sales') {
        const sales = JSON.parse(text)
        const totals = sales.totals
        block = { type: 'text', text: `模拟数据经营分析：销售额 ${totals.revenue_cents / 100} 元，毛利 ${totals.gross_profit_cents / 100} 元，毛利率 ${totals.gross_margin_pct ?? '无数据'}%，订单 ${totals.orders} 笔。无历史数据，不计算同比环比。` }
      } else if (tool?.name === 'query_inventory') {
        const inventory = JSON.parse(text)
        block = { type: 'text', text: `模拟库存分析：在库 ${inventory.totals.on_hand} 件，可用 ${inventory.totals.available} 件，缺口 ${inventory.totals.shortage} 件。` }
      } else block = { type: 'text', text: `主Agent汇总：\n${results.map(r => r.content.map(b => b.text ?? '').join('\n')).join('\n')}` }
    }
    if (block.type === 'tool-call') block.arguments = JSON.stringify(block.arguments)
    yield { type: 'block-start', index: 0, blockType: block.type }
    if (block.type === 'text') yield { type: 'text-delta', index: 0, text: block.text }
    else yield { type: 'tool-call-delta', index: 0, id: block.id, name: block.name, argumentsDelta: block.arguments }
    yield { type: 'block-end', index: 0, block }
    yield { type: 'finish', reason: { kind: block.type === 'text' ? 'stop' : 'tool-calls' } }
  }
}
export function apply(ctx) { ctx.llm.registerAdapter(['enterprise-test'], new TestAdapter()) }
