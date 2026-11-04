"""Small file based run history for the Flask worker subprocess."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import traceback as traceback_module
from datetime import datetime, timezone
from pathlib import Path

RUNS_DIR = Path(__file__).resolve().parents[1] / 'logs' / 'runs'
_LOCAL_LOCK = threading.RLock()
_RUN_ID_RE = re.compile(r'^[a-f0-9]{32}$')
TERMINAL = {'completed', 'failed', 'stopped'}


def valid_run_id(run_id):
    return isinstance(run_id, str) and bool(_RUN_ID_RE.fullmatch(run_id))


def run_dir(run_id, root=RUNS_DIR):
    if not valid_run_id(run_id):
        raise ValueError('Invalid run ID')
    root = Path(root).resolve()
    target = (root / run_id).resolve()
    if target.parent != root:
        raise ValueError('Invalid run ID')
    return target


def _now():
    return datetime.now(timezone.utc).isoformat()


def _safe_text(value):
    text = str(value or '')
    text = re.sub(r'(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(password|passwd|token|cookie|api[_ -]?key)(\s*[:=]\s*)[^\s,;]+', r'\1\2[REDACTED]', text)
    text = re.sub(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', '[EMAIL]', text, flags=re.I)
    text = re.sub(r'(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)', '[PHONE]', text)
    return text


def sanitize_log_line(line):
    text = str(line or '')
    if re.search(r'(?i)\bAI (?:answered|enriched)\b', text):
        return '[Redacted form answer output.]\n'
    text = re.sub(r'(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(password|passwd|token|cookie|api[_ -]?key)(\s*[:=]\s*)[^\s,;]+', r'\1\2[REDACTED]', text)
    text = re.sub(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', '[EMAIL]', text, flags=re.I)
    text = re.sub(r'(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)', '[PHONE]', text)
    return text


def _lock_file(directory):
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / '.result.lock'
    stream = open(lock_path, 'a+b')
    if os.name == 'nt':
        import msvcrt
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
    return stream


def _unlock_file(stream):
    if os.name == 'nt':
        import msvcrt
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    stream.close()


def _atomic_json(path, value):
    fd, temp_name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _read_result(directory):
    try:
        value = json.loads((directory / 'result.json').read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def create_run(run_id, root=RUNS_DIR):
    directory = run_dir(run_id, root)
    directory.mkdir(parents=True, exist_ok=True)
    now = _now()
    result = {
        'run_id': run_id, 'started_at': now, 'ended_at': None,
        'status': 'running', 'reason_code': None, 'explanation': '',
        'last_successful_stage': None, 'failed_stage': None,
        'exception_type': None, 'traceback': None, 'process_exit_code': None,
        'job_url': None,
        'stats': {'jobs_inspected': 0, 'jobs_applied': 0, 'jobs_skipped': 0, 'jobs_failed': 0},
    }
    with _LOCAL_LOCK:
        _atomic_json(directory / 'result.json', result)
        append_event(run_id, {'event': 'run_started', 'stage': 'startup'}, root)
    prune_runs(root, keep=30)
    return result


def append_event(run_id, event, root=RUNS_DIR):
    directory = run_dir(run_id, root)
    item = {'timestamp': _now(), **event}
    line = json.dumps(item, ensure_ascii=False) + '\n'
    with open(directory / 'events.jsonl', 'a', encoding='utf-8') as stream:
        stream.write(line)
        stream.flush()


def update(run_id, root=RUNS_DIR, **changes):
    directory = run_dir(run_id, root)
    lock = _lock_file(directory)
    try:
        result = _read_result(directory)
        if result is None:
            return None
        if result.get('status') in TERMINAL:
            return result
        result.update(changes)
        _atomic_json(directory / 'result.json', result)
        return result
    finally:
        _unlock_file(lock)


def begin_stage(run_id, stage, root=RUNS_DIR):
    result = update(run_id, root, current_stage=stage)
    append_event(run_id, {'event': 'stage_started', 'stage': stage}, root)
    return result


def complete_stage(run_id, stage, root=RUNS_DIR):
    result = update(run_id, root, current_stage=stage, last_successful_stage=stage)
    append_event(run_id, {'event': 'stage_completed', 'stage': stage}, root)
    return result


def increment(run_id, key, amount=1, root=RUNS_DIR):
    if key not in ('jobs_inspected', 'jobs_applied', 'jobs_skipped', 'jobs_failed'):
        raise ValueError('Unknown run statistic')
    directory = run_dir(run_id, root)
    lock = _lock_file(directory)
    try:
        result = _read_result(directory)
        if result is None or result.get('status') in TERMINAL:
            return result
        stats = result.setdefault('stats', {})
        stats[key] = int(stats.get(key, 0)) + int(amount)
        _atomic_json(directory / 'result.json', result)
        return result
    finally:
        _unlock_file(lock)


def set_job_url(run_id, url, root=RUNS_DIR):
    # Only retain LinkedIn job URLs; never store arbitrary page URLs or query data.
    raw = str(url or '')
    match = re.search(r'(?:linkedin\.com/jobs/(?:view|search-results)/)(\d+)', raw, re.I)
    if not match:
        match = re.search(r'[?&]currentJobId=(\d+)', raw, re.I)
    safe_url = f'https://www.linkedin.com/jobs/view/{match.group(1)}/' if match else None
    return update(run_id, root, job_url=safe_url)


def finish(run_id, status, reason_code, explanation, exit_code=None, root=RUNS_DIR):
    if status not in TERMINAL:
        raise ValueError('A terminal run status is required')
    directory = run_dir(run_id, root)
    lock = _lock_file(directory)
    try:
        result = _read_result(directory)
        if result is None or result.get('status') in TERMINAL:
            return result
        result.update(status=status, reason_code=reason_code,
                      explanation=_safe_text(explanation), ended_at=_now(),
                      process_exit_code=exit_code)
        _atomic_json(directory / 'result.json', result)
        append_event(run_id, {'event': 'run_finished', 'status': status,
                              'reason_code': reason_code}, root)
    finally:
        _unlock_file(lock)
    prune_runs(root, keep=30)
    return result


def stop_for_easy_apply_limit(run_id, error, job_url, screenshot, root=RUNS_DIR):
    """Persist the specific restriction result before worker cleanup can reconcile it."""
    directory = run_dir(run_id, root)
    current = _read_result(directory) or {}
    safe_url = set_job_url(run_id, job_url, root)
    screenshot_ref = Path(screenshot).name if screenshot and Path(screenshot).is_file() else None
    evidence = {'visible_message': _safe_text(getattr(error, 'visible_message', str(error))),
                'screenshot': screenshot_ref,
                'submission_state': getattr(error, 'submission_state', 'not_submitted')}
    lock = _lock_file(directory)
    try:
        result = _read_result(directory)
        if result is None or result.get('status') in TERMINAL:
            return result
        result.update(status='stopped', reason_code='EASY_APPLY_LIMIT_REACHED',
                      explanation='LinkedIn reported an Easy Apply application limit.',
                      message='LinkedIn reported an Easy Apply application limit.',
                      stage='easy_apply', failed_stage='easy_apply',
                      detected_at=_now(), evidence=evidence,
                      job_url=(safe_url or {}).get('job_url'), ended_at=_now(),
                      process_exit_code=0)
        _atomic_json(directory / 'result.json', result)
        append_event(run_id, {'event': 'run_finished', 'status': 'stopped',
                              'reason_code': 'EASY_APPLY_LIMIT_REACHED',
                              'stage': 'easy_apply', 'evidence': evidence}, root)
    finally:
        _unlock_file(lock)
    prune_runs(root, keep=30)
    return result


def record_exit_code(run_id, exit_code, root=RUNS_DIR):
    directory = run_dir(run_id, root)
    lock = _lock_file(directory)
    try:
        result = _read_result(directory)
        if result is None:
            return None
        result['process_exit_code'] = exit_code
        _atomic_json(directory / 'result.json', result)
        return result
    finally:
        _unlock_file(lock)


def fail(run_id, reason_code, explanation, error=None, failed_stage=None,
         driver=None, exit_code=1, root=RUNS_DIR):
    directory = run_dir(run_id, root)
    trace = traceback_module.format_exc() if error is not None else ''
    safe_trace = _safe_text(trace)
    if error is not None:
        try:
            (directory / 'traceback.txt').write_text(safe_trace, encoding='utf-8')
        except OSError:
            pass
    if driver is not None:
        try:
            if getattr(driver, 'session_id', None):
                driver.save_screenshot(str(directory / 'failure.png'))
        except Exception as screenshot_error:
            append_event(run_id, {'event': 'screenshot_failed',
                                  'error_type': type(screenshot_error).__name__}, root)
    current = _read_result(directory) or {}
    changes = {'exception_type': type(error).__name__ if error is not None else None,
               'traceback': safe_trace or None,
               'failed_stage': failed_stage or current.get('current_stage'),
               'explanation': _safe_text(explanation), 'ended_at': _now(),
               'status': 'failed', 'reason_code': reason_code,
               'process_exit_code': exit_code}
    result = update(run_id, root, **changes)
    append_event(run_id, {'event': 'run_failed', 'reason_code': reason_code,
                          'failed_stage': failed_stage or current.get('current_stage'),
                          'exception_type': type(error).__name__ if error is not None else None}, root)
    prune_runs(root, keep=30)
    return result


def reconcile_exit(run_id, exit_code, root=RUNS_DIR):
    current = _read_result(run_dir(run_id, root))
    if current is None or current.get('status') in TERMINAL:
        return current
    if exit_code == 0:
        return finish(run_id, 'completed', 'COMPLETED', 'Automation completed successfully.', 0, root)
    stage = current.get('current_stage') or 'startup'
    reason = 'STARTUP_FAILED' if stage == 'startup' else 'PROCESS_CRASHED'
    explanation = 'Worker exited before reporting a final outcome.'
    directory = run_dir(run_id, root)
    try:
        log_tail = (directory / 'worker.log').read_text(encoding='utf-8', errors='replace')[-20000:]
    except OSError:
        log_tail = ''
    if log_tail:
        safe_trace = _safe_text(log_tail)
        try:
            (directory / 'traceback.txt').write_text(safe_trace, encoding='utf-8')
        except OSError:
            pass
        lock = _lock_file(directory)
        try:
            latest = _read_result(directory)
            if latest and latest.get('status') not in TERMINAL:
                latest['traceback'] = safe_trace
                _atomic_json(directory / 'result.json', latest)
        finally:
            _unlock_file(lock)
    return finish(run_id, 'failed', reason, explanation, exit_code, root)


def list_runs(root=RUNS_DIR, limit=30):
    root = Path(root)
    items = []
    if not root.is_dir():
        return items
    for directory in root.iterdir():
        if not directory.is_dir() or not valid_run_id(directory.name):
            continue
        item = _read_result(directory)
        if item:
            items.append(item)
    items.sort(key=lambda item: item.get('started_at') or '', reverse=True)
    return items[:limit]


def get_run(run_id, root=RUNS_DIR):
    return _read_result(run_dir(run_id, root))


def prune_runs(root=RUNS_DIR, keep=30):
    items = list_runs(root, limit=100000)
    terminal = [item for item in items if item.get('status') in TERMINAL]
    for item in terminal[keep:]:
        try:
            shutil.rmtree(run_dir(item['run_id'], root))
        except OSError:
            pass


class RunDiagnostics:
    def __init__(self, run_id, root=RUNS_DIR):
        self.run_id = str(run_id)
        self.root = root

    def begin_stage(self, stage): return begin_stage(self.run_id, stage, self.root)
    def complete_stage(self, stage): return complete_stage(self.run_id, stage, self.root)
    def increment(self, key, amount=1): return increment(self.run_id, key, amount, self.root)
    def set_job_url(self, url): return set_job_url(self.run_id, url, self.root)
    def event(self, event, **data): return append_event(self.run_id, {'event': event, **data}, self.root)
    def finish(self, status, reason_code, explanation, exit_code=None):
        return finish(self.run_id, status, reason_code, explanation, exit_code, self.root)
    def stop_for_easy_apply_limit(self, error, job_url, screenshot=None):
        return stop_for_easy_apply_limit(self.run_id, error, job_url, screenshot, self.root)
    def fail(self, reason_code, explanation, error=None, failed_stage=None,
             driver=None, exit_code=1):
        return fail(self.run_id, reason_code, explanation, error, failed_stage,
                    driver, exit_code, self.root)
    def result(self): return get_run(self.run_id, self.root)
