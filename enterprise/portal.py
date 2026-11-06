import json
from typing import Literal
from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, Field
from .auth import require_role, can_read_task
from .conversations import Conversations
from .store import now
from .sales import SalesQuery, query_sales
from .inventory import query_inventory
from .security import redact
from .task_service import TaskRequest, submit_task


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=256)


class NewUser(Login):
    role: Literal['admin', 'approver', 'analyst', 'viewer'] = 'analyst'


class EditUser(BaseModel):
    role: Literal['admin', 'approver', 'analyst', 'viewer']
    active: bool


class Password(BaseModel):
    current: str = Field(max_length=256)
    new: str = Field(min_length=12, max_length=256)


class Decision(BaseModel):
    decision: Literal['approved', 'rejected']
    reason: str = Field(min_length=1, max_length=1000)


class ConversationInput(BaseModel):
    title: str = Field(default='新的业务讨论', min_length=1, max_length=100)
    task_id: str | None = None


class ChatInput(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    request_id: str = Field(min_length=8, max_length=80)


class ConversationTitle(BaseModel):
    title: str = Field(min_length=1, max_length=100)


class DeleteConversation(BaseModel):
    confirm: Literal[True]


def mount_portal(app, store, auth, settings):
    chats = Conversations(store)

    @app.post('/api/auth/login')
    def login(body: Login, request: Request, response: Response):
        raw, user = auth.login(body.username.strip(), body.password, request.client.host if request.client else 'local')
        response.set_cookie(settings.cookie_name, raw, httponly=True, samesite='strict', max_age=8 * 3600,
                            secure=request.url.scheme == 'https')
        response.headers['Cache-Control'] = 'no-store'
        return user

    @app.get('/api/auth/me')
    def me(request: Request):
        return request.state.user

    @app.post('/api/auth/logout')
    def logout(request: Request, response: Response):
        auth.logout(request.cookies.get(settings.cookie_name))
        response.delete_cookie(settings.cookie_name)
        return {'ok': True}

    @app.post('/api/auth/password')
    def password(body: Password, request: Request, response: Response):
        auth.change_password(request.state.user, body.current, body.new)
        response.delete_cookie(settings.cookie_name)
        return {'ok': True, 'message': '密码已更新，请重新登录'}

    @app.get('/api/users')
    def users(request: Request):
        require_role(request.state.user, 'admin')
        with store.connection() as db:
            return {'items': [auth.public(r) for r in db.execute('SELECT * FROM users ORDER BY created_at')]}

    @app.post('/api/users', status_code=201)
    def create_user(body: NewUser, request: Request):
        require_role(request.state.user, 'admin')
        return auth.create_user(body.username.strip(), body.password, body.role, request.state.user['id'], must_change=True)

    @app.patch('/api/users/{uid}')
    def update_user(uid: str, body: EditUser, request: Request):
        require_role(request.state.user, 'admin')
        auth.update_user(request.state.user, uid, body.role, body.active)
        return {'ok': True}

    @app.get('/api/audit')
    def audit(request: Request):
        require_role(request.state.user, 'admin')
        with store.connection() as db:
            return {'items': [dict(r) for r in db.execute('SELECT a.*,u.username FROM audit_log a LEFT JOIN users u ON u.id=a.actor ORDER BY a.id DESC LIMIT 100')]}

    @app.get('/api/approvals')
    def approvals(request: Request):
        user = request.state.user
        where, params = ('', []) if user['role'] in {'admin', 'approver'} else (' WHERE a.requester=?', [user['id']])
        with store.connection() as db:
            rows = db.execute('SELECT a.*,t.question,t.analysis_type,t.attempt,u.username AS requester_name FROM approvals a JOIN tasks t ON t.id=a.task_id LEFT JOIN users u ON u.id=a.requester' + where + ' ORDER BY a.created_at DESC LIMIT 100', params)
            return {'items': [dict(r) for r in rows]}

    @app.post('/api/approvals/{approval_id}/decision')
    def decide(approval_id: str, body: Decision, request: Request):
        body.reason = redact(body.reason)
        user = request.state.user
        require_role(user, 'admin', 'approver')
        with store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            approval = db.execute('SELECT * FROM approvals WHERE id=?', (approval_id,)).fetchone()
            if not approval:
                raise HTTPException(404, '审批不存在')
            from .auth import require_independent_reviewer
            require_independent_reviewer(approval['requester'], user['id'])
            if approval['status'] != 'pending':
                raise HTTPException(409, '审批已处理，不能重复决定')
            db.execute('UPDATE approvals SET status=?,reviewer=?,reason=?,decided_at=? WHERE id=?',
                       (body.decision, user['id'], body.reason, now(), approval_id))
            if body.decision == 'approved':
                db.execute("UPDATE tasks SET approval_status='approved',updated_at=? WHERE id=? AND status='queued'", (now(), approval['task_id']))
            else:
                db.execute("UPDATE tasks SET approval_status='rejected',status='failed',error='审批被驳回',updated_at=? WHERE id=? AND status='queued'", (now(), approval['task_id']))
            store._event(db, approval['task_id'], 'approval.' + body.decision, '审批通过' if body.decision == 'approved' else '审批驳回',
                         {'reviewer': user['username'], 'reason': body.reason})
            auth.audit(db, user['id'], 'approval.' + body.decision, approval['task_id'], {'reason': body.reason})
        return {'ok': True, 'task_id': approval['task_id']}

    @app.post('/api/tasks/{task_id}/resume', status_code=202)
    def resume(task_id: str, request: Request):
        settings.require_execution()
        user = request.state.user
        require_role(user, 'admin', 'analyst')
        task = store.get(task_id)
        if not task or not can_read_task(user, task):
            raise HTTPException(404, '任务不存在')
        from .procurement import Procurement
        sales_employee = Procurement(store).capabilities(user['id'])['apply']
        task = store.resume(task_id, 'pending' if user['role'] == 'analyst' and not sales_employee else 'approved', user['id'])
        with store.connection() as db:
            auth.audit(db, user['id'], 'task.resume', task_id, {'attempt': task['attempt']})
        return task

    @app.get('/api/tasks/{task_id}/checkpoints')
    def checkpoints(task_id: str, request: Request):
        task = store.get(task_id)
        if not task or not can_read_task(request.state.user, task):
            raise HTTPException(404, '任务不存在')
        return {'items': list(store.checkpoints(task_id).values())}

    @app.get('/api/dashboard')
    def dashboard(request: Request):
        user = request.state.user
        where, params = (' WHERE owner_id=?', [user['id']]) if user['role'] == 'analyst' else ('', [])
        with store.connection() as db:
            counts = {r['status']: r['n'] for r in db.execute('SELECT status,count(*) n FROM tasks' + where + ' GROUP BY status', params)}
            pending = db.execute("SELECT count(*) FROM tasks" + where + (' AND' if where else ' WHERE') + " approval_status='pending'", params).fetchone()[0]
        return {'counts': counts, 'pending_approvals': pending, 'sales': query_sales(SalesQuery()),
                'inventory': query_inventory(SalesQuery()), 'scope': '我的任务' if where else '企业全部任务',
                'recent': store.list(6, owner_id=user['id'] if where else None)['items']}

    @app.get('/api/monitor')
    def monitor(request: Request):
        user = request.state.user
        owner = user['id'] if user['role'] == 'analyst' else None
        where, params = (' WHERE t.owner_id=?', [owner]) if owner else ('', [])
        with store.connection() as db:
            rows = db.execute('SELECT e.*,t.status,t.approval_status,t.attempt FROM task_events e JOIN tasks t ON t.id=e.task_id' + where + ' ORDER BY e.id DESC LIMIT 100', params).fetchall()
            events = [{**dict(r), 'detail': json.loads(r['detail_json'])} for r in rows]
        return {'worker_alive': bool(app.state.worker and app.state.worker.thread.is_alive()), 'mode': 'disabled' if settings.acceptance else settings.mode,
                'app_mode': settings.app_mode, 'execution_enabled': not settings.acceptance,
                'running': [] if settings.acceptance else store.list(100, status='running', owner_id=owner)['items'],
                'historical_running': store.list(100, status='running', owner_id=owner)['items'] if settings.acceptance else [],
                'queued': store.list(100, status='queued', owner_id=owner)['items'], 'events': events}

    @app.get('/api/conversations')
    def list_conversations(request: Request):
        return {'items': chats.list(request.state.user['id'])}

    @app.post('/api/conversations', status_code=201)
    def create_conversation(body: ConversationInput, request: Request):
        if body.task_id:
            task = store.get(body.task_id)
            if not task or not can_read_task(request.state.user, task) or not task['result']:
                raise HTTPException(404, '关联报告不存在或无权读取')
        return chats.create(request.state.user['id'], body.title, body.task_id)

    @app.get('/api/conversations/{cid}')
    def get_conversation(cid: str, request: Request):
        return chats.get(cid, request.state.user['id'])

    @app.patch('/api/conversations/{cid}')
    def rename_conversation(cid: str, body: ConversationTitle, request: Request):
        return chats.rename(cid, request.state.user['id'], body.title)

    @app.delete('/api/conversations/{cid}')
    def delete_conversation(cid: str, body: DeleteConversation, request: Request):
        chats.delete(cid, request.state.user['id'])
        return {'ok': True}

    @app.post('/api/internal/assistant/tasks', status_code=202)
    def assistant_task(body: TaskRequest, request: Request):
        identity = request.state.chat_identity
        task = submit_task(store, settings, identity['owner_id'], body, identity['turn_id'])
        return {k: task[k] for k in ['id', 'status', 'approval_status', 'query', 'analysis_type']}

    @app.post('/api/conversations/{cid}/messages', status_code=202)
    def chat(cid: str, body: ChatInput, request: Request):
        return chats.submit(cid, request.state.user['id'], body.text, body.request_id)
