"""Lightweight per-run system and process memory logging (Linux /proc)."""
from __future__ import annotations

import glob
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from modules.run_diagnostics import run_dir


def _proc_snapshot():
    """Return process metadata keyed by PID without capturing command-line contents."""
    processes = {}
    for proc_path in glob.glob('/proc/[0-9]*'):
        pid_text = proc_path.rsplit('/', 1)[-1]
        try:
            stat = Path(proc_path, 'stat').read_text(encoding='utf-8')
            closing = stat.rfind(')')
            fields = stat[closing + 2:].split()
            ppid = int(fields[1])
            name = Path(proc_path, 'comm').read_text(encoding='utf-8').strip()
            processes[int(pid_text)] = {'pid': int(pid_text), 'ppid': ppid, 'name': name,
                                        'path': proc_path}
        except (OSError, ValueError, IndexError):
            continue
    return processes


def _memory_for_process(process):
    """Read RSS and proportional set size (PSS) in MiB for one live process."""
    values = {}
    try:
        for line in Path(process['path'], 'smaps_rollup').read_text(encoding='utf-8').splitlines():
            if line.startswith(('Rss:', 'Pss:')):
                key, amount, *_ = line.split()
                values[key[:-1].lower() + '_mb'] = round(int(amount) / 1024, 1)
    except (OSError, ValueError):
        try:
            for line in Path(process['path'], 'status').read_text(encoding='utf-8').splitlines():
                if line.startswith('VmRSS:'):
                    values['rss_mb'] = round(int(line.split()[1]) / 1024, 1)
                    break
        except (OSError, ValueError, IndexError):
            pass
    return values


def _group_snapshot(processes):
    root_pid = os.getpid()
    children = {}
    for process in processes.values():
        children.setdefault(process['ppid'], []).append(process['pid'])
    descendants = set()
    pending = [root_pid]
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in descendants:
                descendants.add(child)
                pending.append(child)

    bot = processes.get(root_pid)
    chrome = [p for pid, p in processes.items() if pid in descendants and
              p['name'].lower() in ('chrome', 'chromium', 'chromedriver')]
    ollama = [p for p in processes.values() if p['name'].lower() == 'llama-server']

    def summarize(items):
        measured = []
        for process in items:
            item = {'pid': process['pid'], 'name': process['name']}
            item.update(_memory_for_process(process))
            measured.append(item)
        return {
            'process_count': len(measured),
            'rss_mb': round(sum(p.get('rss_mb', 0) for p in measured), 1),
            'pss_mb': round(sum(p.get('pss_mb', 0) for p in measured), 1),
            'processes': measured,
        }

    return {
        'bot': summarize([bot] if bot else []),
        'chrome_tree': summarize(chrome),
        'ollama_runner': summarize(ollama),
    }


def _system_snapshot():
    result = {}
    try:
        values = {}
        for line in Path('/proc/meminfo').read_text(encoding='utf-8').splitlines():
            key, _, raw = line.partition(':')
            if key in ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'):
                values[key] = int(raw.strip().split()[0])
        total, available = values.get('MemTotal'), values.get('MemAvailable')
        swap_total, swap_free = values.get('SwapTotal'), values.get('SwapFree')
        result['ram_total_mb'] = round(total / 1024, 1) if total is not None else None
        result['ram_available_mb'] = round(available / 1024, 1) if available is not None else None
        result['ram_used_mb'] = round((total - available) / 1024, 1) if total is not None and available is not None else None
        result['swap_total_mb'] = round(swap_total / 1024, 1) if swap_total is not None else None
        result['swap_used_mb'] = round((swap_total - swap_free) / 1024, 1) if swap_total is not None and swap_free is not None else None
    except (OSError, ValueError, IndexError):
        pass
    return result


def _gpu_snapshot():
    try:
        response = subprocess.run(
            ['nvidia-smi', '--query-gpu=memory.used,memory.total,utilization.gpu',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=2, check=False)
        if response.returncode == 0:
            devices = []
            for line in response.stdout.splitlines():
                values = [part.strip() for part in line.split(',')]
                if len(values) == 3:
                    devices.append({'vram_used_mb': int(values[0]),
                                    'vram_total_mb': int(values[1]),
                                    'gpu_utilization_percent': int(values[2])})
            return devices
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


class ResourceMonitor:
    """Append timestamped RAM, process PSS/RSS, swap and NVIDIA VRAM samples."""

    def __init__(self, run_id, root=None, interval_seconds=5):
        self.path = run_dir(run_id, root) / 'resources.jsonl'
        self.interval_seconds = max(2, float(interval_seconds))
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if not Path('/proc').is_dir():
            return
        self._thread = threading.Thread(target=self._run, name='run-resource-monitor', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _sample(self):
        sample = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'system': _system_snapshot(),
            'processes': _group_snapshot(_proc_snapshot()),
            'gpus': _gpu_snapshot(),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(sample, separators=(',', ':')) + '\n')
                stream.flush()
        except OSError:
            pass

    def _run(self):
        while not self._stop.is_set():
            try:
                self._sample()
            except Exception:
                # Monitoring must never prevent the automation from running.
                pass
            self._stop.wait(self.interval_seconds)
