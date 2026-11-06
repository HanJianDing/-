import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from .security import redact


def now():
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    def __init__(self, path: Path, *, initialize=True, execution_enabled=True):
        self.path = path
        self.existing_only = not initialize
        self.execution_enabled = execution_enabled
        self.file_identity = None
        if self.existing_only:
            from .acceptance import plain_path, identity
            self.path = plain_path(path)
            self.file_identity = identity(self.path)
        if initialize:
            self.initialize_schema()

    def require_execution(self):
        if not self.execution_enabled:
            from fastapi import HTTPException
            raise HTTPException(409, '安全验收模式：后台 Agent 执行已禁用')

    def initialize_schema(self):
        if self.existing_only:
            raise RuntimeError('验收模式禁止初始化数据库')
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed','interrupted')),
                mode TEXT NOT NULL, query_json TEXT NOT NULL, result_json TEXT, error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            columns = {r[1] for r in db.execute('PRAGMA table_info(tasks)')}
            if 'analysis_type' not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN analysis_type TEXT NOT NULL DEFAULT 'sales'")
            if 'question' not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN question TEXT NOT NULL DEFAULT ''")
            for name, declaration in [('owner_id', 'TEXT'), ('approval_status', "TEXT NOT NULL DEFAULT 'approved'"),
                                      ('attempt', 'INTEGER NOT NULL DEFAULT 1'), ('source', "TEXT NOT NULL DEFAULT 'manual'")]:
                if name not in columns:
                    db.execute(f'ALTER TABLE tasks ADD COLUMN {name} {declaration}')
            db.execute('''CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
                event_key TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
                session_id TEXT, role TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(task_id,event_key))''')
            db.execute('CREATE INDEX IF NOT EXISTS events_task_id ON task_events(task_id,id)')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY,task_id TEXT UNIQUE REFERENCES tasks(id),
                  requester TEXT NOT NULL,status TEXT NOT NULL,reviewer TEXT,reason TEXT,created_at TEXT,decided_at TEXT);
                CREATE TABLE IF NOT EXISTS checkpoints(task_id TEXT REFERENCES tasks(id),domain TEXT,payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,PRIMARY KEY(task_id,domain));
                CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,title TEXT NOT NULL,
                  session_id TEXT NOT NULL UNIQUE,task_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chat_turns(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES conversations(id),
                  request_id TEXT NOT NULL,user_text TEXT NOT NULL,reply TEXT,status TEXT NOT NULL,error TEXT,
                  provider TEXT,model TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                  UNIQUE(conversation_id,request_id));
            ''')
            for table, name, declaration in [('conversations', 'deleted_at', 'TEXT'), ('chat_turns', 'task_id', 'TEXT REFERENCES tasks(id)')]:
                if name not in {r[1] for r in db.execute(f'PRAGMA table_info({table})')}:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')

    @contextmanager
    def connection(self):
        if self.existing_only:
            from .acceptance import plain_path, identity
            if plain_path(self.path) != self.path or identity(self.path) != self.file_identity:
                raise RuntimeError('验收数据库文件身份发生变化')
            # Auxiliary lifecycle/path checks are performed atomically by pinned
            # handles in connect_existing, not exists()/resolve() before opening.
        if self.existing_only:
            from .acceptance import connect_existing
            db = connect_existing(self.path, expected=self.file_identity)
        else:
            db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, query: dict, mode: str, analysis_type='sales', question='', owner_id=None, approval_status='approved'):
        with self.connection() as db:
            task_id = self.create_in_transaction(db, query, mode, analysis_type, question, owner_id, approval_status)
        return self.get(task_id)

    def create_in_transaction(self, db, query, mode, analysis_type, question, owner_id, approval_status, source='manual'):
        task_id = str(uuid4())
        db.execute("INSERT INTO tasks (id,session_id,status,mode,query_json,created_at,updated_at,analysis_type,question,owner_id,approval_status,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (task_id, f'enterprise-{task_id}', 'queued', mode, json.dumps(query), now(), now(), analysis_type, redact(question), owner_id, approval_status, source))
        self._event(db, task_id, 'task.created', '任务已创建', {'query': query, 'analysis_type': analysis_type, 'question': question, 'source': source}, event_key='created')
        if approval_status == 'pending':
            self._request_approval(db, task_id, owner_id)
        return task_id

    def get(self, task_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["query"] = json.loads(result.pop("query_json"))
        raw = result.pop("result_json")
        result["result"] = json.loads(raw) if raw is not None else None
        return redact(result)

    def list(self, limit=30, offset=0, status=None, owner_id=None):
        clauses, params = [], []
        if status:
            clauses.append('status=?'); params.append(status)
        if owner_id:
            clauses.append('owner_id=?'); params.append(owner_id)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self.connection() as db:
            count = db.execute('SELECT count(*) FROM tasks' + where, params).fetchone()[0]
            rows = db.execute('SELECT id,session_id,status,mode,query_json,analysis_type,question,owner_id,approval_status,attempt,source,created_at,updated_at FROM tasks' + where + ' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', [*params, limit, offset]).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item['query'] = json.loads(item.pop('query_json'))
            items.append(redact(item))
        return {'items': items, 'total': count, 'limit': limit, 'offset': offset}

    def _event(self, db, task_id, kind, title, detail, session_id=None, role='main', event_key=None):
        db.execute('INSERT OR IGNORE INTO task_events(task_id,event_key,kind,title,session_id,role,detail_json,created_at) VALUES (?,?,?,?,?,?,?,?)',
                   (task_id, event_key or str(uuid4()), kind, title, session_id, role, json.dumps(redact(detail), ensure_ascii=False), now()))

    def add_event(self, task_id, kind, title, detail, **kwargs):
        with self.connection() as db:
            self._event(db, task_id, kind, title, detail, **kwargs)

    def events(self, task_id, after=0, limit=200):
        with self.connection() as db:
            rows = db.execute('SELECT * FROM task_events WHERE task_id=? AND id>? ORDER BY id LIMIT ?', (task_id, after, limit)).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item['detail'] = json.loads(item.pop('detail_json'))
            output.append(redact(item))
        return output

    def claim(self, task_id):
        self.require_execution()
        with self.connection() as db:
            changed = db.execute("UPDATE tasks SET status='running', updated_at=? WHERE id=? AND status='queued' AND approval_status='approved'", (now(), task_id)).rowcount == 1
            if changed:
                attempt = db.execute('SELECT attempt FROM tasks WHERE id=?', (task_id,)).fetchone()[0]
                self._event(db, task_id, 'task.started', '主 Agent 开始处理', {'attempt': attempt}, event_key=f'started:{attempt}')
            return changed

    def finish(self, task_id, *, result=None, error=None):
        status = "failed" if error is not None else "succeeded"
        with self.connection() as db:
            changed = db.execute("UPDATE tasks SET status=?,result_json=?,error=?,updated_at=? WHERE id=? AND status='running'",
                                 (status, json.dumps(redact(result), ensure_ascii=False) if result is not None else None,
                                  redact(error), now(), task_id)).rowcount
            if changed != 1:
                raise RuntimeError("task no longer owned by this execution")
            attempt = db.execute('SELECT attempt FROM tasks WHERE id=?', (task_id,)).fetchone()[0]
            self._event(db, task_id, 'task.failed' if error is not None else 'task.completed', '任务执行失败' if error is not None else '最终报告已保存',
                        {'error': error} if error is not None else {'summary': result.get('summary', '') if isinstance(result, dict) else result}, event_key=f'finished:{attempt}')

    def interrupt_unfinished(self):
        # Phase one is a single API process. Never silently replay an uncertain task.
        with self.connection() as db:
            rows = db.execute("SELECT id FROM tasks WHERE status IN ('queued','running')").fetchall()
            db.execute("UPDATE tasks SET status='interrupted', error='service restarted; execution outcome requires review', updated_at=? WHERE status IN ('queued','running')", (now(),))
            for row in rows:
                self._event(db, row['id'], 'task.interrupted', '服务重启，任务中断', {'reason': '请检查已执行操作后重新提交'}, event_key='interrupted')
            return len(rows)

    def _request_approval(self, db, task_id, owner):
        db.execute('INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET status=excluded.status,reviewer=NULL,reason=NULL,created_at=excluded.created_at,decided_at=NULL',
                   (str(uuid4()), task_id, owner, 'pending', None, None, now(), None))
        self._event(db, task_id, 'approval.requested', '等待审批', {'requester': owner})

    def resume(self, task_id, approval='approved', actor=None):
        self.require_execution()
        from fastapi import HTTPException
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row or row['status'] not in {'failed', 'interrupted'} or row['approval_status'] == 'rejected':
                raise HTTPException(409, '仅可续跑失败或中断且未被驳回的任务')
            attempt = row['attempt'] + 1
            db.execute("UPDATE tasks SET status='queued',error=NULL,attempt=?,session_id=?,approval_status=?,updated_at=? WHERE id=?",
                       (attempt, f'enterprise-{task_id}-attempt-{attempt}', approval, now(), task_id))
            self._event(db, task_id, 'task.resumed', '已申请断点续跑', {'attempt': attempt, 'actor': actor})
            if approval == 'pending':
                self._request_approval(db, task_id, row['owner_id'])
        return self.get(task_id)

    def recover(self):
        self.require_execution()
        # The process lock guarantees no other worker owns this database.
        with self.connection() as db:
            rows = db.execute("SELECT id,attempt FROM tasks WHERE status='running'").fetchall()
            for row in rows:
                db.execute("UPDATE tasks SET status='interrupted',error='worker restarted',updated_at=? WHERE id=?", (now(), row['id']))
                self._event(db, row['id'], 'task.interrupted', '进程中断，检查持久化断点', {'attempt': row['attempt']})
            db.execute("UPDATE chat_turns SET status='interrupted',error='服务中断；该轮未自动重放，可继续提问，已有业务任务保持原编号',updated_at=? WHERE status='running'", (now(),))
        for row in rows:
            if row['attempt'] < 3:
                self.resume(row['id'], actor='recovery')

    def ready(self):
        with self.connection() as db:
            row = db.execute("SELECT id FROM tasks WHERE status='queued' AND approval_status='approved' ORDER BY created_at LIMIT 1").fetchone()
        return self.get(row['id']) if row else None

    def save_checkpoint(self, task_id, domain, payload):
        with self.connection() as db:
            changed = db.execute('INSERT OR IGNORE INTO checkpoints VALUES (?,?,?,?)',
                                 (task_id, domain, json.dumps(redact(payload), ensure_ascii=False), now())).rowcount
            if changed:
                self._event(db, task_id, 'checkpoint.saved', '专业分析断点已保存', {'domain': domain, 'child_session_id': payload['session_id']}, role=domain)

    def checkpoints(self, task_id):
        with self.connection() as db:
            return {row['domain']: json.loads(row['payload_json']) for row in db.execute('SELECT * FROM checkpoints WHERE task_id=?', (task_id,))}
