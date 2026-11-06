import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api, isAcceptance } from './api'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Activity, ArrowDown, ArrowRight, Boxes, Check, CheckCircle2, ChevronLeft, ChevronRight, Circle, Clock3, FileText, GitBranch, LayoutDashboard, LoaderCircle, Plus, RefreshCw, Search, Send, Sparkles, TrendingUp, X, AlertCircle, Download, Database } from 'lucide-react'
import './style.css'

const statuses = { queued: '排队中', running: '执行中', succeeded: '已完成', failed: '失败', interrupted: '已中断' }
const types = { sales: '经营分析', inventory: '库存分析', combined: '经营与库存联合分析', auto: '智能选择', procurement: '补货与采购建议' }
const regions = { all: '全部区域', east: '东部', south: '南部', north: '北部' }
const roles = { main: '主 Agent', sales: '经营分析 Agent', inventory: '库存分析 Agent', procurement: '采购分析 Agent', human: '人工业务操作', agent: '子 Agent' }
const time = value => new Date(value).toLocaleString('zh-CN', { hour12: false })
const shortTime = value => new Date(value).toLocaleTimeString('zh-CN', { hour12: false })
const busy = task => !isAcceptance() && ['queued', 'running'].includes(task?.status) && task?.approval_status !== 'pending'
function Badge({ status }) { return <span className={`badge ${status}`}><i/>{status === 'unknown' ? '无历史事件' : (isAcceptance() && ['running','queued'].includes(status) ? '历史状态 · '+statuses[status] : statuses[status]) || status}</span> }
function Report({ text }) { return <div className="markdown"><Markdown remarkPlugins={[remarkGfm]} components={{ img: () => null, a: props => <a {...props} target="_blank" rel="noreferrer"/> }}>{text}</Markdown></div> }

function Modal({ title, children, onClose, wide = false }) {
  const box = useRef()
  useEffect(() => {
    const prior = document.activeElement
    box.current?.focus()
    const key = e => {
      if (e.key === 'Escape') onClose()
      if (e.key === 'Tab') {
        const nodes = box.current.querySelectorAll('button,input,select,textarea,a[href]')
        const first = nodes[0], last = nodes[nodes.length - 1]
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus() }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
      }
    }
    document.addEventListener('keydown', key)
    return () => { document.removeEventListener('keydown', key); prior?.focus() }
  }, [onClose])
  return <div className="overlay" onMouseDown={e => e.target === e.currentTarget && onClose()}><section className={`modal ${wide ? 'wide' : ''}`} role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} ref={box}><div className="modal-head"><h2>{title}</h2><button className="icon-button" aria-label="关闭" onClick={onClose}><X size={20}/></button></div>{children}</section></div>
}

function CreateTask({ onClose, onCreated, question = '' }) {
  const [form, setForm] = useState({ analysis_type: 'combined', region: 'all', start_date: '2026-10-01', end_date: '2026-10-31', question })
  const [sending, setSending] = useState(false), [error, setError] = useState('')
  const field = e => setForm({ ...form, [e.target.name]: e.target.value })
  async function submit(e) {
    e.preventDefault(); setSending(true); setError('')
    try { const task = await api('/api/tasks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(form) }); onCreated(task.task_id) }
    catch (e) { setError(e.message); setSending(false) }
  }
  return <Modal title="创建分析任务" onClose={onClose}><form onSubmit={submit}>
    <p className="muted">提出经营问题，让专业 Agent 分工分析。</p>
    <label>分析类型<select name="analysis_type" value={form.analysis_type} onChange={field}>{Object.entries(types).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label>
    <div className="form-grid"><label>开始日期<input type="date" required name="start_date" value={form.start_date} onChange={field}/></label><label>结束日期<input type="date" required name="end_date" min={form.start_date} value={form.end_date} onChange={field}/></label></div>
    <label>分析区域<select name="region" value={form.region} onChange={field}>{Object.entries(regions).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label>
    <label>业务问题 <span className="muted">{form.analysis_type === 'auto' ? '必填' : '选填'}</span><textarea name="question" rows={3} maxLength={2000} required={form.analysis_type === 'auto'} value={form.question} onChange={field} placeholder="例如：哪些区域需要优先补货？结合销售表现给出建议。"/></label>
    <div className="notice"><Database size={16}/><span>使用模拟业务数据与真实 DeepSeek 模型。销售样本为 2026 年 10 月，库存快照为 10 月 31 日。</span></div>
    {error && <p role="alert" className="error">{error}</p>}
    <div className="modal-actions"><button type="button" className="secondary" onClick={onClose} disabled={sending}>取消</button><button className="primary" disabled={sending}>{sending ? <LoaderCircle className="spin" size={17}/> : <Send size={17}/>}提交任务</button></div>
  </form></Modal>
}

export default function TaskWorkspace({ initialTaskId = '', initialQuestion = '', user }) {
  const [list, setList] = useState({ items: [], total: 0 }), [selected, setSelected] = useState(initialTaskId)
  const [task, setTask] = useState(null), [events, setEvents] = useState([]), [filter, setFilter] = useState(''), [page, setPage] = useState(0)
  const [create, setCreate] = useState(false), [detail, setDetail] = useState(null), [tab, setTab] = useState('chain'), [error, setError] = useState('')
  const [health, setHealth] = useState(null), [connection, setConnection] = useState('connecting'), [loading, setLoading] = useState(true)
  const [reportRole, setReportRole] = useState('summary')
  useEffect(() => { if (initialTaskId) setSelected(initialTaskId) }, [initialTaskId])
  useEffect(() => { if (initialQuestion) setCreate(true) }, [initialQuestion])
  const closeCreate = useCallback(() => setCreate(false), []), closeDetail = useCallback(() => setDetail(null), [])
  const selectedRef = useRef(selected)
  selectedRef.current = selected
  const refresh = useCallback(async () => {
    try {
      const result = await api(`/api/tasks?limit=20&offset=${page * 20}${filter ? `&status=${filter}` : ''}`)
      setList(result); setError('')
      if (!selectedRef.current && result.items.length) setSelected(result.items[0].id)
    } catch (e) { setError(e.message) } finally { setLoading(false) }
  }, [filter, page])
  useEffect(() => { refresh(); const timer = setInterval(refresh, 5000); return () => clearInterval(timer) }, [refresh])
  useEffect(() => { api('/health').then(setHealth).catch(() => setError('服务暂时不可用，请确认 FastAPI 已启动')) }, [])
  useEffect(() => {
    if (!selected) return
    location.hash = 'tasks/' + selected
    setTask(null); setEvents([]); setConnection('connecting'); setReportRole('summary')
    let active = true
    const readTask = () => api(`/api/tasks/${selected}`).then(t => { if (active) setTask(t) }).catch(e => active && setError(e.message))
    readTask()
    const stream = new EventSource(`/api/tasks/${selected}/stream`)
    stream.onopen = () => active && setConnection('connected')
    stream.addEventListener('activity', e => {
      if (!active) return
      const item = JSON.parse(e.data)
      setEvents(old => old.some(x => x.id === item.id) ? old : [...old, item])
      if (item.kind.startsWith('task.')) readTask()
    })
    stream.addEventListener('done', () => { if (active) { setConnection('complete'); readTask(); refresh() } stream.close() })
    stream.onerror = () => active && setConnection('reconnecting')
    return () => { active = false; stream.close() }
  }, [selected, refresh])
  const newTask = id => { setCreate(false); setSelected(id); setTab('chain'); setFilter(''); setPage(0); refresh() }
  const nodes = ['main', ...new Set(events.filter(e => ['sales','inventory','procurement'].includes(e.role)).map(e => e.role))]
  function nodeStatus(role) {
    if (task?.approval_status === 'pending') return '待审批'
    const boundary = events.findLastIndex(e => e.kind === 'task.resumed' || e.kind === 'task.started')
    const rows = events.slice(Math.max(0, boundary)).filter(e => e.role === role)
    if (!events.length && !busy(task)) return 'unknown'
    if (rows.some(e => e.kind.endsWith('.failed'))) return 'failed'
    if (rows.some(e => e.kind === 'agent.completed' || e.kind === 'checkpoint.reused')) return 'succeeded'
    return busy(task) && rows.length ? 'running' : task?.status === 'interrupted' ? 'interrupted' : 'queued'
  }
  function renderNode(role) {
    return <button key={role} className={`agent-node ${role}`} onClick={() => setDetail({ title: roles[role], role, detail: { events: events.filter(e => e.role === role) } })}><span className="agent-symbol">{role === 'main' ? <Sparkles size={20}/> : role === 'inventory' ? <Boxes size={20}/> : <TrendingUp size={20}/>}</span><strong>{roles[role]}</strong><small>{role === 'main' ? '理解需求与汇总' : role === 'sales' ? '销售表现与经营建议' : '库存风险与补货建议'}</small><Badge status={nodeStatus(role)}/></button>
  }
  const report = reportRole === 'summary' ? task?.result?.summary : task?.result?.children?.find(c => c.session_id === reportRole)?.analysis
  function download() {
    const url = URL.createObjectURL(new Blob([report], { type: 'text/markdown;charset=utf-8' }))
    const link = document.createElement('a'); link.href = url; link.download = `${selected}-report.md`; link.click(); URL.revokeObjectURL(url)
  }
  return <div className="embedded-workbench">
    <main><header className="topbar"><span>工作空间 <ChevronRight size={14}/> 任务中心</span><span className="local-pill">LOCAL WORKSPACE</span></header>
      <div className="page-heading"><div><div className="eyebrow">TASK CENTER</div><h1>任务中心<span>。</span></h1><p>统一查看 AI 助手和手动提交的任务状态、执行链路与分析结果。</p></div>{['admin','analyst'].includes(user.role) && <button className="secondary" onClick={() => setCreate(true)}><Plus size={18}/>新建任务（备用）</button>}</div>
      {error && <div className="error-banner" role="alert"><AlertCircle size={18}/>{error}<button onClick={refresh}>重试</button></div>}
      <div className="stats"><div><span className="stat-icon"><FileText size={20}/></span><span><small>{filter ? '筛选结果数' : '任务总数'}</small><strong>{list.total}</strong></span></div><div><span className="stat-icon teal"><Activity size={20}/></span><span><small>本页进行中</small><strong>{list.items.filter(busy).length}</strong></span></div><div><span className="stat-icon mint"><CheckCircle2 size={20}/></span><span><small>本页已完成</small><strong>{list.items.filter(x => x.status === 'succeeded').length}</strong></span></div><div className="stats-note"><GitBranch size={22}/><span>专业分工，共同决策<small>经营分析 × 库存分析 × 采购建议</small></span></div></div>
      <div className="workbench"><section className="task-panel"><div className="panel-title"><h2>任务记录 <span>{list.total}</span></h2><button className="icon-button" title="刷新任务" onClick={refresh}><RefreshCw size={16}/></button></div><div className="list-filter"><Search size={15}/><select aria-label="筛选任务状态" value={filter} onChange={e => { setFilter(e.target.value); setPage(0) }}><option value="">全部状态</option>{Object.entries(statuses).map(([s, label]) => <option value={s} key={s}>{label}</option>)}</select></div>
        <div className="task-list">{loading && <div className="empty"><LoaderCircle className="spin"/>正在加载任务</div>}{!loading && !list.items.length && <div className="empty"><FileText size={30}/><strong>还没有任务</strong><span>创建任务，开始你的第一次分析。</span></div>}
          {list.items.map(item => <button key={item.id} className={`task-item ${selected === item.id ? 'selected' : ''}`} onClick={() => { setSelected(item.id); setDetail(null) }}><div className="task-item-top"><span className="task-type"><GitBranch size={14}/>{types[item.analysis_type] || '经营分析'}</span><Badge status={item.approval_status === 'pending' ? '待审批' : item.status}/></div><strong>{item.question || `${regions[item.query.region]} · ${types[item.analysis_type] || '经营分析'}`}</strong><div className="task-date">{item.query.start_date} — {item.query.end_date}</div><div className="task-item-foot"><code>#{item.id.slice(0, 8)}</code><span>{shortTime(item.created_at)}</span></div></button>)}
        </div><div className="pagination"><span>第 {page + 1} 页</span><button aria-label="上一页" disabled={page === 0} onClick={() => setPage(page - 1)}><ChevronLeft size={16}/></button><button aria-label="下一页" disabled={(page + 1) * 20 >= list.total} onClick={() => setPage(page + 1)}><ChevronRight size={16}/></button></div>
      </section>
      <section className="detail-panel">{!task ? <div className="empty welcome"><GitBranch size={44}/><h2>{selected ? '正在加载任务…' : '所有任务，在这里追踪'}</h2><p>选择左侧任务，查看 Agent 协作过程与报告。</p>{!selected && ['admin','analyst'].includes(user.role) && <button className="secondary" onClick={() => setCreate(true)}>创建分析任务 <ArrowRight size={15}/></button>}</div> : <>
        <div className="detail-heading"><div><div className="detail-id">TASK / {task.id.slice(0, 8).toUpperCase()}</div><h2>{types[task.analysis_type] || '经营分析'}</h2><p>{regions[task.query.region]}<i/> {task.query.start_date} 至 {task.query.end_date}</p></div><Badge status={task.approval_status === 'pending' ? '待审批' : task.status}/></div>
        <div className="task-actions"><span>{task.source === 'assistant' ? 'AI助手提交' : '手动提交'} · 第 {task.attempt || 1} 次执行 · {task.approval_status === 'pending' ? '审批通过后执行' : '步骤断点持久化'}</span>{task.approval_status === 'pending' && <a href="#approvals">查看审批</a>}{['failed','interrupted'].includes(task.status) && task.approval_status !== 'rejected' && ['admin','analyst'].includes(user.role) && <button className="secondary" onClick={async () => { try { await api(`/api/tasks/${task.id}/resume`, {method:'POST'}); setSelected(''); setTimeout(() => setSelected(task.id), 0); refresh() } catch(e) {setError(e.message)} }}>从断点续跑</button>}{task.result && <button className="secondary" onClick={async () => {try {const c = await api('/api/conversations',{method:'POST',body:JSON.stringify({title:'报告追问 · '+task.id.slice(0,8),task_id:task.id})}); location.hash='assistant/'+c.id} catch(e){setError(e.message)} }}>向 AI 助手追问</button>}</div>
        {task.question && <div className="question"><span>业务问题</span>{task.question}</div>}
        <div className="tabs"><button className={tab === 'chain' ? 'active' : ''} onClick={() => setTab('chain')}><GitBranch size={16}/>执行链路 <span>{events.length}</span></button><button className={tab === 'report' ? 'active' : ''} onClick={() => setTab('report')}><FileText size={16}/>分析报告</button><span className={`stream-state ${connection}`}><i/>{connection === 'connected' ? '实时同步' : connection === 'complete' ? '已同步' : connection === 'reconnecting' ? '连接中断，正在重连' : '连接中'}</span></div>
        {task.error && <div role="alert" className="task-error"><AlertCircle size={18}/><div><strong>任务未能完成</strong><p>{task.error}</p></div></div>}
        {tab === 'chain' ? <div className="chain-content"><div className="section-caption"><h3>Agent 协作</h3><span>点击节点查看详情 <ArrowRight size={14}/></span></div><div className="agent-graph">{renderNode('main')}{nodes.length > 1 && <><div className="graph-link"><ArrowRight size={20}/></div><div className="graph-branches">{nodes.filter(role => role !== 'main').map(renderNode)}</div></>}</div>
          <div className="section-caption events-caption"><h3>执行时间线</h3><span>{events.length} 条持久化事件</span></div>
          {!events.length && <div className="empty"><Clock3 size={25}/>{busy(task) ? '等待执行事件…' : '历史任务暂无事件记录，仍可查看已保存的报告。'}</div>}
          <div className="timeline">{events.map((event, i) => <button className={`timeline-row ${event.kind.endsWith('.failed') || event.kind.endsWith('.interrupted') ? 'failure' : ''}`} key={event.id} onClick={() => setDetail(event)}><span className="timeline-dot">{event.kind.endsWith('.failed') ? <AlertCircle size={15}/> : event.kind.endsWith('.completed') ? <Check size={14}/> : <Circle size={9}/>}</span><span className="event-title">{event.title}<small>{roles[event.role]}{event.detail?.tool && ` · ${event.detail.tool}`}</small></span><time>{shortTime(event.created_at)}</time><ChevronRight size={14}/></button>)}</div>
          {busy(task) && <div className="running-note"><LoaderCircle size={16} className="spin"/>Agent 正在处理，新的事件将自动显示。</div>}
        </div> : <div className="report-content">{task.result ? <><div className="report-toolbar"><select aria-label="选择分析报告" value={reportRole} onChange={e => setReportRole(e.target.value)}><option value="summary">最终汇总报告</option>{task.result.children?.map(c => <option value={c.session_id} key={c.session_id}>{roles[c.role]}报告</option>)}</select><button className="secondary" onClick={download}><Download size={15}/>导出报告</button></div><Report text={report || '暂无报告'}/></> : <div className="empty"><FileText size={35}/><strong>{busy(task) ? '报告正在生成' : '本次任务没有生成报告'}</strong><span>{busy(task) ? '你可以先在执行链路中查看实时进度。' : '请查看执行链路中的异常详情。'}</span><button className="secondary" onClick={() => setTab('chain')}>查看执行链路</button></div>}</div>}
        <div className="detail-footer"><Database size={13}/>结果与事件已持久化至本机 SQLite<span>创建于 {time(task.created_at)}</span></div>
      </>}</section></div><footer className="page-footer">序策 · 企业多 Agent 工作台<span>业务数据为模拟样本 · AI 结论请结合实际业务审核</span></footer>
    </main>
    {create && <CreateTask onClose={closeCreate} onCreated={newTask} question={initialQuestion}/>}
    {detail && <Modal title={detail.title} onClose={closeDetail} wide><div className="event-meta"><span>{roles[detail.role]}</span>{detail.created_at && <time>{time(detail.created_at)}</time>}</div>{detail.detail?.text ? <Report text={detail.detail.text}/> : <pre className="json-detail">{JSON.stringify(detail.detail, null, 2)}</pre>}{detail.session_id && <p className="session-id">会话：{detail.session_id}</p>}</Modal>}
  </div>
}

