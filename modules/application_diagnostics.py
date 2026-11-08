"""Append-only per-attempt exception diagnostics for application runs."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path

from modules import run_diagnostics

_LOCK = threading.RLock()


class ApplicationRunContextError(RuntimeError):
    """A supplied run ID is invalid, missing, or no longer active."""


@dataclass(frozen=True)
class ApplicationRunContext:
    run_id: str | None
    root: Path = run_diagnostics.RUNS_DIR
    source: str = 'direct'


def run_context_for_id(run_id, root=run_diagnostics.RUNS_DIR, *, source='explicit', require_active=True):
    """Build an explicit run context; never substitute a different run ID."""
    root = Path(root)
    if run_id is None or run_id == '':
        return ApplicationRunContext(None, root, 'direct' if source == 'environment' else source)
    if not run_diagnostics.valid_run_id(run_id):
        raise ApplicationRunContextError('BOT_RUN_ID must be a valid 32-character run ID.')
    if require_active:
        result = run_diagnostics.get_run(run_id, root)
        if not result or result.get('status') != 'running':
            raise ApplicationRunContextError(
                f'Run ID {run_id} does not identify an active run; refusing to attach application evidence to it.'
            )
    return ApplicationRunContext(str(run_id), root, source)


def run_context_from_environment(root=run_diagnostics.RUNS_DIR):
    """Resolve the worker's run ID once at startup, validating its lifecycle."""
    candidate = os.environ.get('BOT_RUN_ID')
    context = run_context_for_id(candidate, root, source='environment', require_active=True)
    if context.run_id is None:
        os.environ.pop('BOT_RUN_ID', None)
    else:
        os.environ['BOT_RUN_ID'] = context.run_id
    return context


def append_exception_event(event, root=run_diagnostics.RUNS_DIR):
    """Persist one diagnostic event without replacing earlier attempt evidence."""
    run_id = event.get('run_id')
    if run_diagnostics.valid_run_id(run_id):
        directory = run_diagnostics.run_dir(run_id, root)
        path = directory / 'application_attempts.jsonl'
        directory.mkdir(parents=True, exist_ok=True)
    else:
        run_id = None
        path = Path(root).resolve().parent / 'application_attempts_unassigned.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)

    record = {'schema_version': 2, 'event': 'application_exception',
              'timestamp': datetime.now(timezone.utc).isoformat(), **event, 'run_id': run_id}
    payload = json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n'
    with _LOCK:
        lock = run_diagnostics._lock_file(path.parent)
        fd = None
        try:
            fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            stream = os.fdopen(fd, 'a', encoding='utf-8')
            fd = None  # stream now owns the descriptor
            with stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if fd is not None:
                os.close(fd)
            run_diagnostics._unlock_file(lock)


def ensure_attempt_log(run_id=None, root=run_diagnostics.RUNS_DIR):
    """Create the per-run JSONL evidence file even when no exception occurs."""
    if run_diagnostics.valid_run_id(run_id):
        directory = run_diagnostics.run_dir(run_id, root)
        path = directory / 'application_attempts.jsonl'
    else:
        path = Path(root).resolve().parent / 'application_attempts_unassigned.jsonl'
        directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    return path
