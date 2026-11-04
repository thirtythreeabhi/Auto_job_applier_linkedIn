"""Persistent, local-only storage for LinkedIn comment drafts."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import threading

DB_PATH = Path(__file__).resolve().parents[1] / 'linkedin_comment_drafts.sqlite3'
_LOCK = threading.RLock()


def _connect(path=None):
    connection = sqlite3.connect(str(path or DB_PATH), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('''CREATE TABLE IF NOT EXISTS comment_drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_url TEXT NOT NULL UNIQUE,
        post_text TEXT NOT NULL,
        author TEXT NOT NULL DEFAULT '',
        rationale TEXT NOT NULL DEFAULT '',
        comment TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'draft',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        error TEXT NOT NULL DEFAULT ''
    )''')
    connection.commit()
    return connection


def add_draft(post_url, post_text, author, rationale, comment, path=None):
    now = datetime.now(timezone.utc).isoformat()
    with _LOCK, _connect(path) as db:
        cursor = db.execute('''INSERT OR IGNORE INTO comment_drafts
            (post_url, post_text, author, rationale, comment, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)''',
            (post_url, post_text, author or '', rationale or '', comment or '', now, now))
        if cursor.rowcount:
            return int(cursor.lastrowid)
        row = db.execute('SELECT id FROM comment_drafts WHERE post_url=?', (post_url,)).fetchone()
        return int(row['id'])


def list_drafts(path=None):
    with _LOCK, _connect(path) as db:
        rows = db.execute('''SELECT * FROM comment_drafts
            WHERE status IN ('draft', 'error') ORDER BY created_at DESC''').fetchall()
        return [dict(row) for row in rows]


def get_by_url(post_url, path=None):
    with _LOCK, _connect(path) as db:
        row = db.execute('SELECT * FROM comment_drafts WHERE post_url=?', (post_url,)).fetchone()
        return dict(row) if row else None


def refresh_draft(draft_id, post_url, post_text, author, rationale, comment, path=None):
    """Refresh an unposted draft for a post that is being re-evaluated."""
    now = datetime.now(timezone.utc).isoformat()
    with _LOCK, _connect(path) as db:
        cursor = db.execute('''UPDATE comment_drafts SET post_url=?, post_text=?, author=?, rationale=?,
            comment=?, status='draft', error='', updated_at=? WHERE id=? AND status!='posted' ''',
            (post_url, post_text, author or '', rationale or '', comment or '', now, int(draft_id)))
        return cursor.rowcount == 1


def update_status(draft_id, status, error='', path=None):
    allowed = {'draft', 'skipped', 'error', 'posted'}
    if status not in allowed:
        raise ValueError('Invalid comment draft status.')
    now = datetime.now(timezone.utc).isoformat()
    with _LOCK, _connect(path) as db:
        cursor = db.execute('''UPDATE comment_drafts SET status=?, error=?, updated_at=?
            WHERE id=? AND status IN ('draft', 'error', 'skipped')''',
            (status, str(error)[:1000], now, int(draft_id)))
        return cursor.rowcount == 1
