from contextlib import asynccontextmanager
import os
import asyncio
import json
import hmac
from typing import Literal
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from .sales import SalesQuery, query_sales
from .inventory import query_inventory
from .settings import Settings, ROOT
from .store import TaskStore
from .runner import HarnessRunner
from .auth import AuthService, can_read_task, require_role
from .portal import mount_portal
from .worker import Worker, ProcessLease
from .task_service import TaskRequest, submit_task
from .procurement import migrate as migrate_procurement
from .purchase_api import mount_purchase
from .acceptance import AuxiliaryUnavailable


def create_app(settings=None, runner=None):
    settings = (settings or Settings.from_env()).checked()
    # Protect initialization as well as the running application. The short
    # construction lease is released for TestClient/factory use, then reacquired
    # for the entire lifespan before recovery or any request can be served.
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    lease = ProcessLease(settings.db_path)
    lease.acquire()
    try:
        if settings.isolated:
            from .acceptance import connect_existing, validate_schema
            from contextlib import closing
            with closing(connect_existing(settings.db_path, readonly=True)) as db:
                validate_schema(db)
        store = TaskStore(settings.db_path, initialize=not settings.isolated,
                          execution_enabled=not settings.acceptance)
        runner = runner or HarnessRunner(settings, store)
        auth = AuthService(store, initialize=not settings.isolated)
        if not settings.isolated:
            migrate_procurement(store)
    finally:
        lease.release()

    @asynccontextmanager
    async def lifespan(app):
        runtime_lease = ProcessLease(settings.db_path)
        runtime_lease.acquire()
        try:
            if not settings.acceptance:
                if not settings.isolated:
                    auth.bootstrap()
                    store.recover()
                else:
                    with store.connection() as db:
                        if db.execute("SELECT 1 FROM tasks WHERE status IN ('queued','running') LIMIT 1").fetchone() or db.execute("SELECT 1 FROM chat_turns WHERE status IN ('queued','running') LIMIT 1").fetchone():
                            raise RuntimeError('独立执行环境存在历史未完成任务；拒绝自动消费，请先人工核查')
                app.state.worker = Worker(settings, store, auth, runner)
                app.state.worker.start()
            try:
                yield
            finally:
                if app.state.worker is not None:
                    await asyncio.to_thread(app.state.worker.stop)
        finally:
            runtime_lease.release()

    app = FastAPI(title="企业多 Agent 工作台", lifespan=lifespan)
    app.state.worker = None
    app.state.settings = settings
    app.state.store = store
    app.state.auth = auth
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.2'] if settings.isolated else ['127.0.0.1', 'localhost', 'testserver'])

    @app.middleware('http')
    async def authorization(request: Request, call_next):
        path = request.url.path
        if path.startswith('/api/'):
            if settings.acceptance and (path.startswith('/api/internal/') or request.headers.get('authorization', '').startswith('Bearer ')):
                return JSONResponse({'detail': '安全验收模式：内部 Agent 凭证入口已禁用'}, status_code=403)
            if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
                origin = request.headers.get('origin')
                allowed = {str(request.base_url).rstrip('/')}
                if not settings.isolated:
                    allowed.update({'http://127.0.0.1:5173', 'http://localhost:5173'})
                if origin and origin not in allowed:
                    return JSONResponse({'detail': '来源校验失败'}, status_code=403)
            worker = None
            chat_identity = None
            if path in {'/api/internal/assistant/tasks','/api/internal/assistant/plan','/api/internal/assistant/plan-choice'} and request.method == 'POST':
                header = request.headers.get('authorization', '')
                if header.startswith('Bearer '):
                    chat_identity = await asyncio.to_thread(auth.chat_identity, header[7:])
                if not chat_identity:
                    return JSONResponse({'detail': '助手凭据无效或已撤销'}, status_code=403)
                request.state.chat_identity = chat_identity
            if path in {'/api/sales', '/api/inventory'} and request.method == 'GET':
                header = request.headers.get('authorization', '')
                if header.startswith('Bearer '):
                    worker = await asyncio.to_thread(auth.worker_task, header[7:])
                    if worker:
                        query = {k: request.query_params.get(k, v) for k, v in {'start_date': '2026-10-01', 'end_date': '2026-10-31', 'region': 'all'}.items()}
                        domain = path.rsplit('/', 1)[-1]
                        with store.connection() as db:
                            risk_row=db.execute('SELECT payload_json FROM risk_evaluations WHERE task_id=?',(worker['id'],)).fetchone()
                        purchase_scope = bool(risk_row and json.loads(risk_row[0]).get('trigger'))
                        if query != worker['query'] or (worker['analysis_type'] not in {domain, 'combined', 'auto','procurement'} and not purchase_scope):
                            return JSONResponse({'detail': '工具凭证不允许查询此范围'}, status_code=403)
            if path != '/api/auth/login' and not worker and not chat_identity:
                try:
                    user = await asyncio.to_thread(auth.session, request.cookies.get(settings.cookie_name))
                except AuxiliaryUnavailable as exc:
                    # Authentication runs outside FastAPI's endpoint exception
                    # handler. Keep exhausted connection retries a controlled 503.
                    if not settings.acceptance:raise
                    return JSONResponse({'detail': exc.detail}, status_code=503)
                if not user:
                    return JSONResponse({'detail': '请先登录'}, status_code=401)
                request.state.user = user
                if user['must_change'] and path not in {'/api/auth/me', '/api/auth/password', '/api/auth/logout'}:
                    return JSONResponse({'detail': '请先修改初始密码'}, status_code=403)
                if request.method not in {'GET', 'HEAD', 'OPTIONS'} and not hmac.compare_digest(request.headers.get('x-csrf-token', ''), user['csrf']):
                    return JSONResponse({'detail': '请求校验失败，请重新登录'}, status_code=403)
        if settings.acceptance and request.method == 'POST' and (path == '/api/tasks' or
                (path.startswith('/api/tasks/') and path.endswith('/resume')) or
                (path.startswith('/api/conversations/') and path.endswith('/messages'))):
            return JSONResponse({'detail': '安全验收模式：后台 Agent 执行已禁用'}, status_code=409)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        if path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    def accessible(task_id, user):
        task = store.get(task_id)
        if task is None or not can_read_task(user, task):
            raise HTTPException(404, 'task not found')
        return task

    mount_portal(app, store, auth, settings)
    mount_purchase(app, store)

    @app.get("/health")
    def health():
        return {"status": "ok", "agent_mode": "disabled" if settings.acceptance else settings.mode,
                "app_mode": settings.app_mode, "execution_enabled": not settings.acceptance}

    @app.get("/api/sales")
    def sales(start_date: str = Query("2026-10-01"), end_date: str = Query("2026-10-31"), region: str = Query("all")):
        try:
            query = SalesQuery(start_date=start_date, end_date=end_date, region=region)
        except ValueError:
            raise HTTPException(422, "invalid sales query") from None
        return query_sales(query)

    @app.post("/api/tasks", status_code=202)
    def create_task(query: TaskRequest, request: Request):
        user = request.state.user
        require_role(user, 'admin', 'analyst')
        task = submit_task(store, settings, user['id'], query)
        return {"task_id": task["id"], "status": "queued", "session_id": task["session_id"], 'approval_status': task['approval_status']}

    @app.get('/api/inventory')
    def inventory(start_date: str = '2026-10-01', end_date: str = '2026-10-31', region: str = 'all'):
        try:
            query = SalesQuery(start_date=start_date, end_date=end_date, region=region)
        except ValueError:
            raise HTTPException(422, 'invalid inventory query') from None
        return query_inventory(query)

    @app.get('/api/tasks')
    def list_tasks(request: Request, limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0),
                   status: Literal['queued', 'running', 'succeeded', 'failed', 'interrupted'] | None = None):
        user = request.state.user
        return store.list(limit, offset, status, owner_id=user['id'] if user['role'] == 'analyst' else None)

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str, request: Request):
        return accessible(task_id, request.state.user)

    @app.get('/api/tasks/{task_id}/events')
    def events(task_id: str, request: Request, after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=500)):
        accessible(task_id, request.state.user)
        return {'items': store.events(task_id, after, limit)}

    @app.get('/api/tasks/{task_id}/stream')
    async def stream(task_id: str, request: Request, after: int = Query(0, ge=0)):
        accessible(task_id, request.state.user)
        try:
            cursor = max(after, int(request.headers.get('last-event-id', '0')))
        except ValueError:
            raise HTTPException(400, 'invalid event cursor') from None

        async def follow():
            nonlocal cursor
            ticks = 0
            while not await request.is_disconnected():
                user = await asyncio.to_thread(auth.session, request.cookies.get(settings.cookie_name))
                if not user or not can_read_task(user, store.get(task_id)):
                    return
                batch = await asyncio.to_thread(store.events, task_id, cursor)
                for item in batch:
                    cursor = item['id']
                    yield f"id: {cursor}\nevent: activity\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                if len(batch) == 200:
                    continue
                task = await asyncio.to_thread(store.get, task_id)
                if task['status'] not in {'queued', 'running'}:
                    # A terminal transaction may have committed after the previous read.
                    if await asyncio.to_thread(store.events, task_id, cursor):
                        continue
                    yield f"event: done\ndata: {json.dumps({'status': task['status']})}\n\n"
                    return
                ticks += 1
                if ticks % 20 == 0:
                    yield ': keepalive\n\n'
                await asyncio.sleep(.5)
        return StreamingResponse(follow(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    frontend = ROOT / 'frontend' / ('dist-acceptance' if settings.isolated else 'dist')
    if (frontend / 'assets').exists():
        app.mount('/assets', StaticFiles(directory=frontend / 'assets'), name='assets')

    @app.get('/', include_in_schema=False)
    def web():
        if not (frontend / 'index.html').exists():
            raise HTTPException(503, 'Web build missing: run npm run web:build')
        return FileResponse(frontend / 'index.html')

    return app
