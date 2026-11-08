"""Small operating-system helpers for identifying and stopping worker processes.

This module has no Flask dependency. The dashboard owns run state and decides
when these low-level process operations should happen.
"""

import os
import select
import signal
import subprocess
import sys


def identify_job_bot(pid, root, executable=None):
    """Return identity details only when *pid* is running this project's job bot."""
    executable = executable or sys.executable
    if os.name == 'nt':
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                           wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
            kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return None
            try:
                image = ctypes.create_unicode_buffer(32768)
                length = wintypes.DWORD(len(image))
                if not kernel32.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
                    return None
                created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
                if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                                ctypes.byref(kernel), ctypes.byref(user)):
                    return None
                start = (created.dwHighDateTime << 32) | created.dwLowDateTime
                if os.path.normcase(os.path.abspath(image.value)) != os.path.normcase(os.path.abspath(executable)):
                    return None
                return {'start_ticks': str(start),
                        'script': os.path.realpath(os.path.join(root, 'runAiBot.py'))}
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, ValueError):
            return None

    if os.name != 'posix' or not os.path.isdir('/proc'):
        return None
    try:
        with open(f'/proc/{pid}/stat', encoding='utf-8') as stream:
            raw_stat = stream.read()
        # comm is parenthesized and may itself contain spaces or parentheses.
        fields = raw_stat[raw_stat.rfind(')') + 2:].split()
        start_ticks = fields[19]  # proc field 22, with fields[0] being field 3
        with open(f'/proc/{pid}/cmdline', 'rb') as stream:
            argv = stream.read().rstrip(b'\0').split(b'\0')
        # The process could exit and its PID could be reused while cmdline is
        # being read. Accept the result only if the same process is still there.
        with open(f'/proc/{pid}/stat', encoding='utf-8') as stream:
            current_stat = stream.read()
        current_fields = current_stat[current_stat.rfind(')') + 2:].split()
        if current_fields[19] != start_ticks:
            return None
        args = [os.fsdecode(arg) for arg in argv]
        script = os.path.realpath(os.path.join(root, 'runAiBot.py'))
        if not any(os.path.realpath(arg) == script for arg in args if arg):
            return None
        return {'start_ticks': start_ticks, 'script': script, 'argv': args,
                'process_group': int(fields[2])}
    except (OSError, IndexError, ValueError):
        return None


def stop_verified_linux_process_group(pid, root, expected_start_ticks, grace_seconds=5):
    """Stop a verified worker group without signaling a reused PID/PGID.

    A pidfd pins the worker process. It is briefly stopped before signaling its
    group, so the group ID cannot be released and reused during that signal.
    If pidfds are unavailable, fail closed instead of signaling a bare number.
    """
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        return False
    try:
        pidfd = os.pidfd_open(pid, 0)
    except OSError:
        return False

    try:
        identity = identify_job_bot(pid, root)
        if (not identity or identity['start_ticks'] != str(expected_start_ticks)
                or identity['process_group'] != pid):
            return False

        # Once SIGSTOP succeeds, this process remains the group leader until
        # we either resume it or kill the group. Its numeric PGID cannot be
        # reassigned to an unrelated process group in that interval.
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGSTOP)
        except OSError:
            return False
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            signal.pidfd_send_signal(pidfd, signal.SIGCONT)
            return False
        signal.pidfd_send_signal(pidfd, signal.SIGCONT)

        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        if poller.poll(max(0, int(grace_seconds * 1000))):
            return True

        # If the bot is still alive, stop that same pinned process again. Only
        # then is it safe to use its PID as the process-group ID for force-kill.
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGSTOP)
        except OSError:
            # It exited during the check; never fall back to a bare-PID kill.
            return True
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            signal.pidfd_send_signal(pidfd, signal.SIGCONT)
            return False
        return True
    finally:
        os.close(pidfd)


def stop_job_bot_process(proc, root=None, expected_start_ticks=None):
    """Stop the job bot process and its children where the platform allows."""
    if proc is None or proc.poll() is not None:
        return
    if sys.platform.startswith('linux'):
        identity = identify_job_bot(proc.pid, root) if root else None
        expected = expected_start_ticks or (identity or {}).get('start_ticks')
        if not root or not expected:
            return False
        stopped = stop_verified_linux_process_group(proc.pid, root, expected)
        if stopped:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return False
        return stopped
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except Exception:
            pass
