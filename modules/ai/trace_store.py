"""Local, multiprocess-safe storage for AI execution traces.

This module is deliberately independent of the model and automation layers so
those callers can opt into tracing without changing model behavior.
"""

from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid


TRACE_DB_PATH = Path(__file__).resolve().parents[2] / '.ai_traces.sqlite3'
BUSY_TIMEOUT_MS = 10_000

_SECRET_KEY = re.compile(r'(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|cookie|session(?:id|_id)?)', re.I)
_SECRET_PATTERNS = (
    (re.compile(r'(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+'), r'\1[REDACTED]'),
    (re.compile(r'(?i)(\b(?:sk|rk|pk)-)[A-Za-z0-9_-]{12,}'), r'\1[REDACTED]'),
    (re.compile(r'(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|cookie|session(?:id|_id)?)\b\s*[=:]\s*)[^\s,;]+'), r'\1[REDACTED]'),
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _redact(value, key=''):
    """Return JSON-safe content with common credentials/session secrets hidden."""
    if _SECRET_KEY.search(str(key)):
        return '[REDACTED]'
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _json(value):
    return json.dumps(_redact(value), ensure_ascii=False, separators=(',', ':'), default=str)


def _db_path():
    return Path(os.environ.get('AI_TRACE_DB_PATH') or TRACE_DB_PATH)


def _connect():
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f'PRAGMA busy_timeout={int(BUSY_TIMEOUT_MS)}')
    conn.execute('PRAGMA foreign_keys=ON')
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
            if str(mode).lower() != 'wal':
                conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
            _initialize(conn)
            break
        except sqlite3.OperationalError as error:
            if 'locked' not in str(error).lower() or time.monotonic() >= deadline:
                conn.close()
                raise
            time.sleep(0.025)
    return conn


@contextmanager
def _connection():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def _initialize(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS ai_traces (
            trace_id TEXT PRIMARY KEY,
            run_id TEXT,
            job_id TEXT,
            job_title TEXT NOT NULL DEFAULT '',
            company TEXT NOT NULL DEFAULT '',
            timestamp TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            prompt TEXT NOT NULL DEFAULT '',
            raw_response TEXT NOT NULL DEFAULT '',
            candidate_context TEXT NOT NULL DEFAULT '',
            parsed_output_json TEXT,
            latency_ms REAL,
            status TEXT NOT NULL DEFAULT 'running',
            error TEXT NOT NULL DEFAULT '',
            retry_info_json TEXT,
            decision TEXT,
            application_outcome TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ai_traces_timestamp_idx ON ai_traces(timestamp DESC);
        CREATE INDEX IF NOT EXISTS ai_traces_job_idx ON ai_traces(job_id, timestamp DESC);
        CREATE INDEX IF NOT EXISTS ai_traces_run_idx ON ai_traces(run_id, timestamp DESC);
        CREATE INDEX IF NOT EXISTS ai_traces_category_idx ON ai_traces(category);
        CREATE INDEX IF NOT EXISTS ai_traces_model_idx ON ai_traces(model);
        CREATE TABLE IF NOT EXISTS ai_trace_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL REFERENCES ai_traces(trace_id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            payload_json TEXT,
            UNIQUE(trace_id, sequence)
        );
        CREATE INDEX IF NOT EXISTS ai_trace_events_trace_idx ON ai_trace_events(trace_id, sequence);
        CREATE TABLE IF NOT EXISTS ai_trace_imports (
            source TEXT NOT NULL,
            line_hash TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            PRIMARY KEY(source, line_hash)
        );
    ''')
    # Additive migrations keep local databases from earlier builds usable.
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(ai_traces)')}
    if 'candidate_context' not in columns:
        conn.execute("ALTER TABLE ai_traces ADD COLUMN candidate_context TEXT NOT NULL DEFAULT ''")
    if 'job_title' not in columns:
        conn.execute("ALTER TABLE ai_traces ADD COLUMN job_title TEXT NOT NULL DEFAULT ''")
    if 'company' not in columns:
        conn.execute("ALTER TABLE ai_traces ADD COLUMN company TEXT NOT NULL DEFAULT ''")


def _decode_trace(row):
    result = dict(row)
    for key in ('parsed_output_json', 'retry_info_json'):
        raw = result.pop(key, None)
        result['parsed_output' if key.startswith('parsed') else 'retry_info'] = json.loads(raw) if raw else None
    return result


def create_trace(*, trace_id=None, run_id=None, job_id=None, timestamp=None,
                 job_title='', company='', category='', provider='', model='', prompt='', candidate_context='', status='running',
                 retry_info=None):
    """Create a trace and return its ID. Values are redacted before persistence."""
    trace_id = str(trace_id or uuid.uuid4())
    now = _now()
    with _connection() as conn:
        conn.execute('''INSERT INTO ai_traces
            (trace_id,run_id,job_id,job_title,company,timestamp,category,provider,model,prompt,candidate_context,status,retry_info_json,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (trace_id, _redact(run_id), _redact(job_id), str(_redact(job_title)), str(_redact(company)), timestamp or now,
             str(_redact(category)), str(_redact(provider)), str(_redact(model)),
             str(_redact(prompt)), str(_redact(candidate_context)), str(status), _json(retry_info) if retry_info is not None else None, now))
    return trace_id


def append_event(trace_id, event_type, name='', payload=None, *, timestamp=None):
    """Append an ordered event. Use ``llm_tool_call`` only for actual model tool calls;
    application actions belong under ``workflow`` (or another workflow subtype).
    """
    allowed = {'llm_tool_call', 'workflow', 'llm_request', 'retry', 'error', 'decision', 'application_outcome'}
    event_type = str(event_type)
    if event_type not in allowed:
        raise ValueError('unsupported event_type; use llm_tool_call for model tools and workflow for application operations')
    with _connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT COALESCE(MAX(sequence),0)+1 AS next FROM ai_trace_events WHERE trace_id=?', (str(trace_id),)).fetchone()
        cursor = conn.execute('''INSERT INTO ai_trace_events
            (trace_id,sequence,timestamp,event_type,name,payload_json) VALUES (?,?,?,?,?,?)''',
            (str(trace_id), row['next'], timestamp or _now(), event_type,
             str(_redact(name)), _json(payload) if payload is not None else None))
        conn.commit()
        return cursor.lastrowid


def complete_trace(trace_id, *, raw_response=None, parsed_output=None, latency_ms=None,
                    error=None, status=None, retry_info=None, decision=None,
                    application_outcome=None):
    """Complete/update trace results; omitted fields retain their prior values."""
    updates = {'updated_at': _now()}
    for key, val in (('raw_response', raw_response), ('latency_ms', latency_ms),
                     ('decision', decision), ('application_outcome', application_outcome)):
        if val is not None:
            updates[key] = _redact(val)
    if parsed_output is not None:
        updates['parsed_output_json'] = _json(parsed_output)
    if error is not None:
        updates['error'] = _redact(error)
    if retry_info is not None:
        updates['retry_info_json'] = _json(retry_info)
    if status is not None:
        updates['status'] = str(status)
    elif error:
        updates['status'] = 'error'
    else:
        updates['status'] = 'completed'
    with _connection() as conn:
        assignments = ','.join(f'{key}=?' for key in updates)
        cursor = conn.execute(f'UPDATE ai_traces SET {assignments} WHERE trace_id=?', (*updates.values(), str(trace_id)))
        if not cursor.rowcount:
            raise KeyError(f'unknown trace_id: {trace_id}')
    return get_trace(trace_id)


def correlate_trace(trace_id, *, run_id=None, job_id=None, decision=None, application_outcome=None):
    """Attach classifier and application outcome context to a trace."""
    updates = {'updated_at': _now()}
    for key, val in (('run_id', run_id), ('job_id', job_id), ('decision', decision),
                     ('application_outcome', application_outcome)):
        if val is not None:
            updates[key] = _redact(val)
    with _connection() as conn:
        cursor = conn.execute('UPDATE ai_traces SET ' + ','.join(f'{k}=?' for k in updates) + ' WHERE trace_id=?',
                              (*updates.values(), str(trace_id)))
        if not cursor.rowcount:
            raise KeyError(f'unknown trace_id: {trace_id}')
    return get_trace(trace_id)


def correlate_job(job_id, *, run_id=None, decision=None, application_outcome=None):
    """Attach job-level decision/outcome to traces matching the LinkedIn job and run."""
    updates = {'updated_at': _now()}
    for key, val in (('run_id', run_id), ('decision', decision), ('application_outcome', application_outcome)):
        if val is not None:
            updates[key] = _redact(val)
    clause = 'job_id=?' + (' AND (run_id=? OR run_id IS NULL)' if run_id is not None else '')
    params = list(updates.values()) + [str(_redact(job_id))]
    if run_id is not None:
        params.append(str(_redact(run_id)))
    with _connection() as conn:
        cursor = conn.execute('UPDATE ai_traces SET ' + ','.join(f'{k}=?' for k in updates) + ' WHERE ' + clause, params)
        return cursor.rowcount


def list_traces(*, filters=None, limit=50, offset=0, **kwargs):
    """Return filtered trace summaries and pagination metadata.

    Supported filters: query, category, model, provider, status, decision,
    run_id, job_id, since and until (ISO timestamps).
    """
    filters = {**(filters or {}), **kwargs}
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    where, values = [], []
    for key in ('category', 'model', 'provider', 'status', 'decision', 'run_id', 'job_id'):
        val = filters.get(key)
        if val:
            if key == 'decision' and val == '__none__':
                where.append('(decision IS NULL OR decision=\'\')')
                continue
            where.append(f'{key}=?')
            values.append(str(_redact(val)))
    if filters.get('since'):
        where.append('timestamp>=?'); values.append(str(filters['since']))
    if filters.get('until'):
        where.append('timestamp<=?'); values.append(str(filters['until']))
    if filters.get('query'):
        where.append('(job_id LIKE ? OR run_id LIKE ? OR job_title LIKE ? OR company LIKE ? OR category LIKE ? OR prompt LIKE ? OR candidate_context LIKE ? OR raw_response LIKE ? OR decision LIKE ?)')
        needle = '%' + str(_redact(filters['query'])) + '%'
        values.extend([needle] * 9)
    clause = ' WHERE ' + ' AND '.join(where) if where else ''
    with _connection() as conn:
        total = conn.execute('SELECT COUNT(*) FROM ai_traces' + clause, values).fetchone()[0]
        rows = conn.execute('''SELECT trace_id,run_id,job_id,job_title,company,timestamp,category,provider,model,status,
            latency_ms,error,retry_info_json,decision,application_outcome FROM ai_traces''' + clause +
            ' ORDER BY timestamp DESC, trace_id DESC LIMIT ? OFFSET ?', (*values, limit, offset)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            raw_retry = item.pop('retry_info_json', None)
            item['retry_info'] = json.loads(raw_retry) if raw_retry else None
            items.append(item)
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset}


def get_trace(trace_id):
    """Return a complete trace including ordered workflow/tool events, or None."""
    with _connection() as conn:
        row = conn.execute('SELECT * FROM ai_traces WHERE trace_id=?', (str(trace_id),)).fetchone()
        if row is None:
            return None
        result = _decode_trace(row)
        result['events'] = []
        for event in conn.execute('SELECT * FROM ai_trace_events WHERE trace_id=? ORDER BY sequence', (str(trace_id),)):
            item = dict(event)
            payload = item.pop('payload_json', None)
            item['payload'] = json.loads(payload) if payload else None
            result['events'].append(item)
        return result


def import_legacy_prompt_log(path=None):
    """Import valid JSONL prompt records once; malformed lines are skipped safely."""
    source = Path(path or (Path(__file__).resolve().parents[2] / '.ai_prompt_log.jsonl'))
    if not source.exists():
        return {'imported': 0, 'malformed': 0}
    imported = malformed = 0
    with source.open(encoding='utf-8') as handle:
        for line in handle:
            if not line.strip():
                continue
            digest = hashlib.sha256(line.encode('utf-8')).hexdigest()
            try:
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError('record must be an object')
            except (ValueError, TypeError):
                malformed += 1
                continue
            with _connection() as conn:
                exists = conn.execute('SELECT trace_id FROM ai_trace_imports WHERE source=? AND line_hash=?',
                                      (str(source.resolve()), digest)).fetchone()
                if exists:
                    continue
                trace_id = str(uuid.uuid4())
                now = _now()
                conn.execute('''INSERT INTO ai_traces
                    (trace_id,timestamp,category,provider,model,prompt,raw_response,error,status,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)''',
                    (trace_id, item.get('timestamp') or now, str(_redact(item.get('category', ''))),
                     str(_redact(item.get('provider', ''))), str(_redact(item.get('model', ''))),
                     str(_redact(item.get('prompt', ''))), str(_redact(item.get('response', ''))),
                     str(_redact(item.get('error', ''))), 'error' if item.get('error') else 'completed', now))
                conn.execute('INSERT INTO ai_trace_imports(source,line_hash,trace_id) VALUES (?,?,?)',
                             (str(source.resolve()), digest, trace_id))
                imported += 1
    return {'imported': imported, 'malformed': malformed}


def redact(value):
    """Public redaction helper shared with the JSONL compatibility writer."""
    return _redact(value)
