"""Cross-process single-flight gate for local LLM requests."""
from contextlib import contextmanager
import os
from pathlib import Path
import threading

_LOCAL_LOCK = threading.Lock()
_LOCK_PATH = Path(__file__).resolve().parents[2] / '.llm_request.lock'


def queued_call(function, *args, **kwargs):
    """Run one blocking model operation in the shared single-request queue."""
    with llm_request_slot():
        return function(*args, **kwargs)


@contextmanager
def llm_request_slot():
    """Serialize model calls from the job and commenting worker processes."""
    with _LOCAL_LOCK:
        _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK_PATH.open('a+b') as handle:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b'0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
