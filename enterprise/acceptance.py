"""Fail-closed acceptance environment checks and explicit offline preparation.

This module never opens the production database implicitly. Preparation takes an
explicit source and is not called by application startup.
"""
import hashlib
import json
import math
import re
import os
from pathlib import Path
import sqlite3
import stat
import time
from contextlib import closing
from fastapi import HTTPException

from .settings import ROOT

MARKER = 'acceptance-environment.json'
EPHEMERAL = {'auth_sessions', 'worker_credentials', 'chat_credentials'}


class AuxiliaryUnavailable(HTTPException):
    def __init__(self):
        super().__init__(503, '验收数据库辅助文件正在变化，请稍后重试')


def plain_path(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if part.is_symlink() or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024):
                raise ValueError('验收路径不得包含符号链接或目录联接')
            if part.is_file() and info.st_nlink != 1:
                raise ValueError('验收文件不得使用硬链接')
    return path.resolve()


def identity(path):
    info = path.stat()
    return [info.st_dev, info.st_ino]


def checked_target(path, root, protected):
    if not path or not root:
        raise ValueError('必须显式指定 ACCEPTANCE_DB 和 ACCEPTANCE_ROOT')
    path, root = plain_path(path), plain_path(root)
    if not root.is_dir() or path == root or not path.is_relative_to(root):
        raise ValueError('验收数据库必须位于已存在的隔离目录中')
    for source in [ROOT/'data'/'tasks.sqlite3', *protected]:
        source = Path(os.path.abspath(source))
        # No open/read of the protected database is needed for this check.
        if path == source.resolve() or root == source.parent.resolve() or root in source.resolve().parents:
            raise ValueError('验收目录或数据库不得指向原业务环境')
        # Hard links were already rejected using the target's link count; do not
        # stat/open protected database files merely to start acceptance.
    for suffix in ('-wal', '-shm', '-journal', '.worker.lock'):
        plain_path(Path(str(path)+suffix))
    return path, root


def validate_environment(path, root, protected):
    path, root = checked_target(path, root, [protected])
    if not path.is_file():
        raise ValueError('验收数据库必须已存在，不会自动创建')
    marker = plain_path(root/MARKER)
    if not marker.is_file():
        raise ValueError('缺少隔离环境准备清单')
    document = json.loads(marker.read_text(encoding='utf-8'))
    if document.get('kind') != 'enterprise-acceptance-v1' or document.get('database') != str(path) or document.get('file_identity') != identity(path) or document.get('directory_identity') != identity(root):
        raise ValueError('隔离环境标识或数据库文件身份不匹配')
    for name in ('logs', 'tmp', 'harness'):
        directory = plain_path(root/name)
        if not directory.is_dir():
            raise ValueError('验收环境目录不完整：'+name)
    return path, root


class GuardedConnection(sqlite3.Connection):
    pins = None
    def close(self):
        try:super().close()
        finally:
            if self.pins is not None:self.pins.close();self.pins=None


def _pin_auxiliaries(pins, path):
    from .safe_files import UnsafePath, SidecarTransient
    for suffix in ('-wal', '-shm', '-journal'):
        try:
            pins.add_sidecar(Path(str(path)+suffix))
        except SidecarTransient:
            raise
        except UnsafePath as exc:
            # Preserve the existing public path-validation error contract.
            raise ValueError(str(exc)) from exc


def _connect_existing_once(path, readonly=False, expected=None):
    from .safe_files import Pins
    path=plain_path(path);pins=Pins()
    try:
        pins.parents(path);pins.add(path)
        if expected is not None and identity(path)!=expected:
            raise ValueError('数据库文件身份不匹配')
        _pin_auxiliaries(pins, path)
        pins.verify()
        db=sqlite3.connect(path.as_uri()+('?mode=ro' if readonly else '?mode=rw'),
                           uri=True,timeout=1,factory=GuardedConnection)
        db.pins=pins
        try:
            # sqlite3.connect is lazy: attach the pager while still inside the
            # establishment boundary, before validating the auxiliary handles.
            # No application SQL or transaction is replayed by the outer retry.
            db.execute('SELECT count(*) FROM sqlite_schema').fetchone()
            _pin_auxiliaries(pins, path)
            pins.verify()
            return db
        except BaseException:
            db.close();raise
    except BaseException:
        pins.close();raise


def connect_existing(path, readonly=False, expected=None):
    """Retry connection establishment only; returned transactions are never replayed."""
    from .safe_files import SidecarTransient
    import logging
    import random
    # Stable identity remains fixed across attempts, even for callers without expected.
    path = plain_path(path)
    expected = identity(path) if expected is None else expected
    parent_identity = identity(path.parent)
    deadline = time.monotonic() + 2.0
    for attempt in range(8):
        if attempt and time.monotonic() >= deadline:
            raise AuxiliaryUnavailable()
        if identity(path.parent) != parent_identity:
            raise ValueError('隔离目录身份发生变化')
        try:
            return _connect_existing_once(path, readonly, expected)
        except SidecarTransient as exc:
            logging.getLogger(__name__).warning('SQLite auxiliary lifecycle retry: attempt=%s reason=%s', attempt + 1, exc)
            delay = min(0.02 * (2 ** attempt), 0.1) + random.uniform(0, 0.02)
            if attempt == 7 or time.monotonic() + delay >= deadline:
                raise AuxiliaryUnavailable() from None
            time.sleep(delay)


def quoted(name):
    return '"'+name.replace('"','""')+'"'


def checks(sql):
    # Tokenize SQL, ignoring comments; preserve quoted literal case and nesting.
    pattern=r"--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`[^`]*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9]*|[^\s]"
    tokens=[t if t[0] in "'\"`[" else t.lower() for t in re.findall(pattern,sql or '')
            if not t.startswith(('--','/*'))]
    result=[]
    for i,token in enumerate(tokens):
        if token!='check' or i+1==len(tokens) or tokens[i+1]!='(':continue
        depth=0;body=[]
        for item in tokens[i+1:]:
            body.append(item)
            if item=='(':depth+=1
            elif item==')':depth-=1
            if depth==0:break
        if depth:raise ValueError('CHECK 语法不完整')
        result.append(body)
    return sorted(result)


def schema_description(db):
    objects = db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return {
        'checks': {name:checks(sql) for kind,name,sql in objects if kind=='table'},
        'unique_keys': {name: sorted([[index[4], [list(c)[1:6] for c in db.execute('PRAGMA index_xinfo('+quoted(index[1])+')') if c[5]]] for index in db.execute('PRAGMA index_list('+quoted(name)+')') if index[2]], key=repr) for kind,name,sql in objects if kind=='table'},
        'triggers': {name:' '.join(sql.split()) for kind,name,sql in objects if kind=='trigger'},
        'tables': {name: sorted([list(row)[1:] for row in db.execute('PRAGMA table_info('+quoted(name)+')')])
                   for kind, name, sql in objects if kind == 'table'},
        'guards': {name: ' '.join(sql.split()).lower() for kind, name, sql in objects if kind in {'trigger', 'index'} and sql},
        'foreign_keys': {name: sorted([list(row) for row in db.execute('PRAGMA foreign_key_list('+quoted(name)+')')])
                         for kind, name, sql in objects if kind == 'table'},
    }


def validate_schema(db):
    expected = json.loads((Path(__file__).with_name('acceptance-schema.json')).read_text(encoding='utf-8'))
    actual = schema_description(db)
    for key in ('checks','unique_keys','triggers'):
        if actual[key]!=expected[key]:
            raise ValueError(('验收数据库历史保护不兼容：' if key=='triggers' else '验收数据库约束不兼容：')+key)
    for table, columns in expected['tables'].items():
        if table not in actual['tables'] or any(col not in actual['tables'][table] for col in columns):
            raise ValueError('验收数据库结构不兼容：'+table)
    for name, definition in expected['guards'].items():
        if actual['guards'].get(name) != definition:
            raise ValueError('验收数据库缺少或改变历史保护/索引：'+name)
    for name, relations in expected['foreign_keys'].items():
        if actual['foreign_keys'].get(name) != relations:
            raise ValueError('验收数据库外键结构不兼容：'+name)


def validate_history(db):
    if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise ValueError('数据库完整性校验失败')
    if db.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('数据库外键校验失败')
    for rid, version, text, digest in db.execute('SELECT request_id,version,snapshot_json,snapshot_hash FROM purchase_versions'):
        if hashlib.sha256(text.encode()).hexdigest() != digest or json.loads(text).get('version') != version:
            raise ValueError('送审快照校验失败')
        if not db.execute("SELECT 1 FROM business_approval_instances WHERE business_type='purchase_request' AND business_id=? AND version=?", (rid, version)).fetchone():
            raise ValueError('送审版本缺少审批轮次')
    for iid, rid, version in db.execute("SELECT id,business_id,version FROM business_approval_instances WHERE business_type='purchase_request'"):
        if not db.execute('SELECT 1 FROM purchase_versions WHERE request_id=? AND version=?', (rid, version)).fetchone():
            raise ValueError('审批轮次缺少送审版本')
        if db.execute('SELECT ordinal FROM business_approval_steps WHERE instance_id=? ORDER BY ordinal', (iid,)).fetchall() != [(1,), (2,)]:
            raise ValueError('两级审批节点不完整')
    if db.execute('SELECT 1 FROM purchase_requests r WHERE version>0 AND NOT EXISTS (SELECT 1 FROM purchase_versions v WHERE v.request_id=r.id AND v.version=r.version)').fetchone():
        raise ValueError('申请缺少当前送审版本')


def fingerprints(db):
    result = {}
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        if table in EPHEMERAL:
            continue
        quoted = '"'+table.replace('"', '""')+'"'
        rows = sorted(json.dumps(row, ensure_ascii=False, default=str) for row in db.execute('SELECT * FROM '+quoted))
        result[table] = {'rows': len(rows), 'sha256': hashlib.sha256('\n'.join(rows).encode()).hexdigest()}
    return result


def prepare_copy(source, root, timeout=30):
    """Create a NEW isolated directory; never overwrite any existing environment."""
    source, root = plain_path(source), plain_path(root)
    if not source.is_file() or root.exists() or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('源文件必须存在，目标目录必须不存在，超时必须为正数')
    if source.is_relative_to(root):
        raise ValueError('源数据库不得位于目标目录中')
    from .safe_files import Pins, private_directory, delete_owned, UnsafePath
    if not root.parent.is_dir():raise ValueError('隔离目标的父目录必须已存在')
    pins=Pins();owned={};target=root/'acceptance.sqlite3'
    try:
        pins.parents(root)
        private_directory(root)
        owned[root]=(pins.add(root,directory=True),True)
        checked_target(target,root,[source])
        owned[target]=(pins.add(target,create=True),False)
        pins.verify()
        deadline=time.monotonic()+timeout
        def progress(status,remaining,total):
            pins.verify()
            if time.monotonic()>=deadline:raise TimeoutError('SQLite 一致性备份超时')
        with closing(connect_existing(source,readonly=True)) as src:
            src.execute('PRAGMA query_only=ON')
            with closing(connect_existing(target,expected=identity(target))) as dst:
                src.backup(dst,pages=128,progress=progress,sleep=.05)
                validate_schema(dst);validate_history(dst)
                before=fingerprints(dst);removed={}
                for table in sorted(EPHEMERAL):
                    removed[table]=dst.execute('SELECT count(*) FROM '+table).fetchone()[0]
                    dst.execute('DELETE FROM '+table)
                dst.commit()
                if fingerprints(dst)!=before:raise ValueError('业务历史在准备期间发生变化')
                if any(dst.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in EPHEMERAL):
                    raise ValueError('临时凭证未完全清除')
                validate_history(dst)
        for name in ('logs','tmp','harness'):
            directory=root/name;private_directory(directory)
            owned[directory]=(pins.add(directory,directory=True),True)
        report={'kind':'enterprise-acceptance-v1','database':str(target),'file_identity':identity(target),
                'directory_identity':identity(root),'source':str(source),'method':'sqlite-backup-api',
                'prepared_at':time.time(),'business_fingerprints':before,'removed_credentials':removed,
                'integrity':'ok','schema':'ok','purchase_history':'ok',
                'path_protection':'windows-pinned-handles-private-dacl',
                'source_auxiliary_files':'read-only database; SQLite WAL/SHM coordination may occur'}
        marker=root/MARKER
        owned[marker]=(pins.add(marker,create=True),False)
        pins.verify()
        marker.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        pins.verify()
        return report
    except BaseException as error:
        try:
            pins.verify()
            # Unknown files are NOT assumed to belong to this invocation.
            if root in owned and any(p not in owned for p in root.rglob('*')):
                raise UnsafePath('发现未登记文件，停止自动清理')
            for path,(token,directory) in reversed(owned.items()):
                pins.release(path)
                delete_owned(path,token,directory=directory)
        except BaseException as cleanup_error:
            raise UnsafePath('无法证明清理安全，已停止自动清理；需人工核查：'+str(root)) from cleanup_error
        raise error
    finally:
        pins.close()
