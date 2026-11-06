"""Project public SDK notifications; never store model reasoning or request headers."""
import json
from .security import redact

TOOLS = {'analyze_sales': '委派经营分析', 'analyze_inventory': '委派库存分析',
         'query_sales': '查询销售数据', 'query_inventory': '查询库存数据'}


class EventRecorder:
    def __init__(self, store, task):
        self.store, self.task = store, task
        self.roles = {task['session_id']: 'main'}
        self.calls = {}
        self.data = {}
        self.reports = {}

    def __call__(self, notification):
        if notification.method != 'session.event':
            return
        payload = notification.payload
        event = payload.get('event', {})
        sid = payload.get('sessionId')
        data, kind = event.get('data', {}), event.get('type')
        detail = {}
        title = None
        role = self.roles.get(sid, 'agent')
        if kind == 'subagent/catalog':
            role = 'procurement' if '采购' in data.get('label', '') else 'inventory' if '库存' in data.get('label', '') else 'sales'
            self.roles[data['childId']] = role
            title, kind = data.get('label', '子 Agent'), 'agent.delegated'
            detail = {'child_session_id': data['childId'], 'parent_session_id': sid, 'role': role}
            sid = data['childId']
        elif kind == 'turn/start':
            title, kind = 'Agent 开始执行', 'agent.started'
        elif kind == 'tool/call':
            name, call_id = data['name'], data['callId']
            self.calls[(sid, call_id)] = name
            args = data.get('arguments')
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            detail = {'tool': name, 'call_id': call_id, 'arguments': args}
            title, kind = TOOLS.get(name, name), 'tool.called'
        elif kind == 'tool/result':
            message = data.get('message', {})
            call_id = message.get('toolCallId')
            name = self.calls.get((sid, call_id), 'tool')
            content = '\n'.join(b.get('text', '') for b in message.get('content', []) if b.get('type') == 'text')
            try:
                content = json.loads(content)
            except ValueError:
                pass
            detail = {'tool': name, 'call_id': call_id, 'output': content, 'is_error': message.get('isError', False)}
            title = TOOLS.get(name, name) + ('失败' if message.get('isError') else '完成')
            kind = 'tool.failed' if message.get('isError') else 'tool.completed'
            if name in {'query_sales', 'query_inventory'} and not message.get('isError') and isinstance(content, dict):
                if role == 'procurement':
                    self.data.setdefault(sid, {'query':self.task['query']})[name.removeprefix('query_')] = content
                else:
                    self.data[sid] = content
        elif kind == 'assistant/message':
            message = data.get('message', {})
            text = '\n'.join(b['text'] for b in message.get('content', []) if b.get('type') == 'text')
            if not text:
                return
            title, kind = 'Agent 输出报告', 'agent.result'
            detail = {'text': text, 'model': message.get('source', {}).get('model'), 'provider': message.get('source', {}).get('provider')}
            self.reports[sid] = text
        elif kind == 'turn/end':
            reason = data.get('reason', {}).get('kind')
            title, kind = ('Agent 执行完成', 'agent.completed') if reason == 'completed' else ('Agent 执行异常', 'agent.failed')
            detail = {'reason': reason}
        if title is not None:
            self.store.add_event(self.task['id'], kind, title, redact(detail), session_id=sid,
                                 role=role, event_key=f"sdk:{payload.get('sessionId')}:{event['seq']}")
            if kind == 'agent.completed' and role in {'sales', 'inventory', 'procurement'} and sid in self.data and sid in self.reports:
                data = self.data[sid]
                complete = role != 'procurement' or all(data.get(k, {}).get('query') == self.task['query'] for k in ['sales','inventory'])
                if complete and data.get('query') == self.task['query']:
                    self.store.save_checkpoint(self.task['id'], role, {'role': role, 'session_id': sid,
                        'analysis': self.reports[sid], 'data': data})
