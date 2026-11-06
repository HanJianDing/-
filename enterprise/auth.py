"""Local accounts, revocable sessions and per-task internal query credentials."""
import hashlib
import hmac
import json
import os
import secrets
import time
from uuid import uuid4
from fastapi import HTTPException
from .store import now
from .security import redact

ROLES = {'admin', 'approver', 'analyst', 'viewer'}


def require_independent_reviewer(applicant, reviewer):
    """Shared human-approval invariant for analysis and purchase workflows."""
    if applicant == reviewer:
        raise HTTPException(403, '不能审批自己的请求')


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    value = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()
    return salt + ':' + value


class AuthService:
    def __init__(self, store, *, initialize=True):
        self.store = store
        if initialize:
            self.initialize_schema()

    def initialize_schema(self):
        store = self.store
        if store.existing_only:
            raise RuntimeError('验收模式禁止初始化认证结构')
        with store.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,username TEXT NOT NULL UNIQUE,
                  password_hash TEXT NOT NULL,role TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,
                  must_change INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS auth_sessions(hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),
                  csrf TEXT NOT NULL,expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS login_failures(subject TEXT NOT NULL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY,actor TEXT,action TEXT NOT NULL,
                  subject TEXT,detail_json TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS worker_credentials(hash TEXT PRIMARY KEY,task_id TEXT NOT NULL,
                  attempt INTEGER NOT NULL,expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS chat_credentials(hash TEXT PRIMARY KEY,turn_id TEXT NOT NULL,expires REAL NOT NULL);
            ''')

    def audit(self, db, actor, action, subject='', detail=None):
        db.execute('INSERT INTO audit_log(actor,action,subject,detail_json,created_at) VALUES (?,?,?,?,?)',
                   (actor, action, redact(subject), json.dumps(redact(detail or {}), ensure_ascii=False), now()))

    def bootstrap(self):
        if self.store.existing_only:
            raise RuntimeError('验收模式禁止创建初始账号')
        with self.store.connection() as db:
            if db.execute('SELECT count(*) FROM users').fetchone()[0]:
                return
        password = secrets.token_urlsafe(18)
        path = self.store.path.parent / 'bootstrap-admin.txt'
        # Not served by HTTP or committed. User must change this one-time password.
        path.write_text('Username: admin\nPassword: ' + password + '\n首次登录后必须修改密码。\n', encoding='utf-8')
        self.create_user('admin', password, 'admin', must_change=True)

    @staticmethod
    def public(row):
        return redact({k: row[k] for k in ['id', 'username', 'role', 'active', 'must_change', 'created_at']})

    def create_user(self, username, password, role, actor=None, must_change=False):
        if role not in ROLES or not 3 <= len(username) <= 40 or not 12 <= len(password) <= 256:
            raise HTTPException(422, '用户名3–40字符，密码12–256字符，角色必须有效')
        import sqlite3
        try:
            with self.store.connection() as db:
                uid = str(uuid4())
                db.execute('INSERT INTO users(id,username,password_hash,role,must_change,created_at) VALUES (?,?,?,?,?,?)',
                           (uid, username, password_hash(password), role, int(must_change), now()))
                self.audit(db, actor, 'user.created', uid, {'username': username, 'role': role})
                return self.public(db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone())
        except sqlite3.IntegrityError:
            raise HTTPException(409, '用户名已存在') from None

    def login(self, username, password, address):
        subject = digest(address + ':' + username.casefold())
        with self.store.connection() as db:
            db.execute('DELETE FROM login_failures WHERE created<?', (time.time() - 300,))
            failures = db.execute('SELECT count(*) FROM login_failures WHERE subject=?', (subject,)).fetchone()[0]
            if failures >= 5:
                raise HTTPException(429, '登录失败次数过多，请5分钟后再试')
            row = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        expected = row['password_hash'] if row else password_hash('invalid-password', '0' * 32)
        valid = hmac.compare_digest(password_hash(password, expected.split(':')[0]), expected)
        if not row or not valid or not row['active']:
            with self.store.connection() as db:
                db.execute('INSERT INTO login_failures VALUES (?,?)', (subject, time.time()))
                self.audit(db, None, 'login.failed', username)
            raise HTTPException(401, '用户名或密码错误')
        raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.store.connection() as db:
            db.execute('DELETE FROM login_failures WHERE subject=?', (subject,))
            db.execute('DELETE FROM auth_sessions WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO auth_sessions VALUES (?,?,?,?)', (digest(raw), row['id'], csrf, time.time() + 8 * 3600))
            self.audit(db, row['id'], 'login.succeeded')
        return raw, {**self.public(row), 'csrf': csrf}

    def session(self, raw):
        if not raw:
            return None
        with self.store.connection() as db:
            row = db.execute('SELECT u.*,s.csrf FROM auth_sessions s JOIN users u ON u.id=s.user_id WHERE s.hash=? AND s.expires>? AND u.active=1',
                             (digest(raw), time.time())).fetchone()
        return {**self.public(row), 'csrf': row['csrf']} if row else None

    def logout(self, raw):
        with self.store.connection() as db:
            db.execute('DELETE FROM auth_sessions WHERE hash=?', (digest(raw or ''),))

    def change_password(self, user, current, new):
        if not 12 <= len(new) <= 256 or new == current:
            raise HTTPException(422, '新密码须为12–256字符，且不同于旧密码')
        with self.store.connection() as db:
            row = db.execute('SELECT password_hash FROM users WHERE id=?', (user['id'],)).fetchone()
            if not hmac.compare_digest(password_hash(current, row[0].split(':')[0]), row[0]):
                raise HTTPException(403, '原密码错误')
            db.execute('UPDATE users SET password_hash=?,must_change=0 WHERE id=?', (password_hash(new), user['id']))
            db.execute('DELETE FROM auth_sessions WHERE user_id=?', (user['id'],))
            self.audit(db, user['id'], 'password.changed')

    def update_user(self, actor, uid, role, active):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
            if not row:
                raise HTTPException(404, '用户不存在')
            if row['role'] == 'admin' and row['active'] and (role != 'admin' or not active):
                if db.execute("SELECT count(*) FROM users WHERE role='admin' AND active=1").fetchone()[0] <= 1:
                    raise HTTPException(409, '不能停用或降级最后一个管理员')
            db.execute('UPDATE users SET role=?,active=? WHERE id=?', (role, int(active), uid))
            db.execute('DELETE FROM auth_sessions WHERE user_id=?', (uid,))
            self.audit(db, actor['id'], 'user.updated', uid, {'role': role, 'active': active})

    def issue_worker(self, task):
        self.store.require_execution()
        raw = secrets.token_urlsafe(32)
        with self.store.connection() as db:
            db.execute('DELETE FROM worker_credentials WHERE expires<? OR task_id=?', (time.time(), task['id']))
            db.execute('INSERT INTO worker_credentials VALUES (?,?,?,?)', (digest(raw), task['id'], task['attempt'], time.time() + 600))
        return raw

    def worker_task(self, raw):
        with self.store.connection() as db:
            row = db.execute("SELECT t.id FROM worker_credentials w JOIN tasks t ON t.id=w.task_id WHERE w.hash=? AND w.expires>? AND t.status='running' AND t.attempt=w.attempt AND t.approval_status='approved'", (digest(raw), time.time())).fetchone()
        return self.store.get(row['id']) if row else None

    def revoke_worker(self, task_id):
        with self.store.connection() as db:
            db.execute('DELETE FROM worker_credentials WHERE task_id=?', (task_id,))

    def issue_chat(self, turn_id):
        self.store.require_execution()
        raw = secrets.token_urlsafe(32)
        with self.store.connection() as db:
            db.execute('DELETE FROM chat_credentials WHERE expires<? OR turn_id=?', (time.time(), turn_id))
            db.execute('INSERT INTO chat_credentials VALUES (?,?,?)', (digest(raw), turn_id, time.time()+600))
        return raw

    def chat_identity(self, raw):
        with self.store.connection() as db:
            row = db.execute("SELECT t.id AS turn_id,c.owner_id FROM chat_credentials k JOIN chat_turns t ON t.id=k.turn_id JOIN conversations c ON c.id=t.conversation_id JOIN users u ON u.id=c.owner_id WHERE k.hash=? AND k.expires>? AND t.status='running' AND c.deleted_at IS NULL AND u.active=1 AND u.must_change=0", (digest(raw), time.time())).fetchone()
            return dict(row) if row else None

    def revoke_chat(self, turn_id):
        with self.store.connection() as db:
            db.execute('DELETE FROM chat_credentials WHERE turn_id=?', (turn_id,))


def require_role(user, *roles):
    if user['role'] not in roles:
        raise HTTPException(403, '当前账号没有此操作权限')


def can_read_task(user, task):
    return user['role'] in {'admin', 'approver', 'viewer'} or task.get('owner_id') == user['id']
