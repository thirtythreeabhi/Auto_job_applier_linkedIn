"""Run-scoped cooperative pause/resume control shared by Flask and the worker."""
from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


CONTROL_PATH = Path(__file__).resolve().parents[1] / '.bot_control.json'


def _read(path=CONTROL_PATH):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def status(path=CONTROL_PATH):
    return _read(path)


def _write(value, path=CONTROL_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


@contextmanager
def _locked(path):
    lock_path = str(path) + '.lock'
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, 'a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(lock.fileno()).st_size == 0:
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def lifecycle_lock(path=CONTROL_PATH):
    """Serialize dashboard lifecycle operations across independent processes."""
    with _locked(str(path) + '.lifecycle'):
        yield


def create_run(run_id, path=CONTROL_PATH):
    """Reset stale commands and create a fresh run before launching its worker."""
    with _locked(path):
        _write({'run_id': str(run_id), 'command': 'resume', 'state': 'running'}, path)


def request(command, run_id, path=CONTROL_PATH):
    """Write pause/resume only for the currently registered run."""
    if command not in ('pause', 'resume'):
        raise ValueError('Unsupported automation command')
    with _locked(path):
        current = _read(path)
        if not run_id or current.get('run_id') != str(run_id):
            return None
        state = current.get('state')
        if command == 'pause' and state in ('pausing', 'paused'):
            return current
        if command == 'resume' and state in ('running', 'resuming'):
            return current
        current.update(command=command, state='pausing' if command == 'pause' else 'resuming')
        _write(current, path)
        return current


def report(state, run_id, path=CONTROL_PATH):
    with _locked(path):
        current = _read(path)
        if current.get('run_id') != str(run_id):
            return False
        if state == 'paused' and current.get('command') != 'pause':
            return True
        if state == 'running' and current.get('command') == 'pause':
            return True
        current['state'] = state
        _write(current, path)
        return True


def set_stopped(run_id=None, path=CONTROL_PATH):
    with _locked(path):
        current = _read(path)
        if run_id is not None and current.get('run_id') != str(run_id):
            return False
        current.update(command='stop', state='stopped')
        _write(current, path)
        return True


class PauseController:
    def __init__(self, run_id, path=CONTROL_PATH, poll_interval=0.25):
        self.run_id = str(run_id)
        self.path = path
        self.poll_interval = poll_interval

    def checkpoint(self, validate=None):
        """Wait cooperatively while paused; invoke validation after resume."""
        while True:
            with _locked(self.path):
                current = _read(self.path)
                if current.get('run_id') != self.run_id:
                    break
                if current.get('command') == 'stop':
                    raise InterruptedError('Automation was stopped.')
                if current.get('command') == 'pause':
                    if current.get('state') != 'paused':
                        current['state'] = 'paused'
                        _write(current, self.path)
                    waiting = True
                else:
                    if current.get('state') != 'running':
                        current['state'] = 'running'
                        _write(current, self.path)
                    waiting = False
            if not waiting:
                break
            time.sleep(self.poll_interval)
        if validate is not None and not validate():
            raise RuntimeError('The LinkedIn page changed while automation was paused; inspect the browser before continuing.')
        return True

    def finish(self):
        report('stopped', self.run_id, self.path)
