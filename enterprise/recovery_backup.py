"""Explicit complete SQLite snapshot; no startup, migrations or credential cleanup.

Windows only. Reuses existing path guards; does not claim to eliminate the
SQLite auxiliary-file first-open race. Failures retain an incomplete directory.
"""
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import time

from .acceptance import connect_existing, identity, plain_path, quoted, validate_history, validate_schema
from .safe_files import Pins, private_directory


def full_fingerprint(db):
    """All tables, including credentials and SQLite sequence state; no row values exported."""
    schema = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name').fetchall()
    tables = {}
    for (name,) in db.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"):
        rows = sorted(repr(tuple(row)) for row in db.execute('SELECT * FROM ' + quoted(name)))
        tables[name] = {'rows': len(rows), 'sha256': hashlib.sha256(repr(rows).encode()).hexdigest()}
    return {'schema_sha256': hashlib.sha256(repr(schema).encode()).hexdigest(), 'tables': tables,
            'user_version': db.execute('PRAGMA user_version').fetchone()[0],
            'application_id': db.execute('PRAGMA application_id').fetchone()[0]}


def create_recovery_backup(source, root, timeout=30):
    source, root = plain_path(source), plain_path(root)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be finite and positive')
    if not source.is_file() or os.path.lexists(root) or not root.parent.is_dir():
        raise ValueError('existing source and parent, and a NEW target directory are required')
    if source.is_relative_to(root):
        raise ValueError('source must not be inside destination')
    pins = Pins()
    started = time.time()
    deadline = time.monotonic() + timeout
    target = root / 'recovery.sqlite3'
    marker = root / 'recovery-backup.json'

    def budget():
        if time.monotonic() >= deadline:
            raise TimeoutError('backup deadline exceeded; incomplete destination retained')

    try:
        pins.parents(root)
        private_directory(root)
        pins.add(root, directory=True)
        pins.add(target, create=True)
        pins.verify()
        with closing(connect_existing(source, readonly=True)) as src:
            src.execute('PRAGMA query_only=ON')
            src.execute('BEGIN')
            # Pin one read snapshot for both fingerprint and Backup API.
            expected = full_fingerprint(src)
            budget()
            with closing(connect_existing(target, expected=identity(target))) as dst:
                def progress(status, remaining, total):
                    budget()
                    pins.verify()
                src.backup(dst, pages=128, progress=progress, sleep=.05)
                validate_schema(dst)
                validate_history(dst)
                actual = full_fingerprint(dst)
                if actual != expected:
                    raise ValueError('complete snapshot mismatch')
            src.rollback()
        budget()
        pins.verify()
        report = {'kind': 'enterprise-full-recovery-v1', 'source': str(source),
                  'database': str(target), 'file_identity': identity(target),
                  'started_at': started, 'finished_at': time.time(),
                  'method': 'sqlite-backup-api', 'credentials_preserved': True,
                  'fingerprint': actual, 'integrity': 'ok', 'foreign_keys': 'ok',
                  'schema': 'ok', 'purchase_history': 'ok',
                  'limitations': 'No automatic restore/start. Source WAL/SHM coordination may occur. Deadline is cooperative.'}
        pins.add(marker, create=True)
        marker.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        pins.verify()
        return report
    finally:
        # Never delete uncertain paths. Any exception means this output is NOT approved.
        pins.close()
