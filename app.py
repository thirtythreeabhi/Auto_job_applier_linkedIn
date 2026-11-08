'''
Author:     Sai Vignesh Golla
License:    MIT License
            https://opensource.org/license/mit
GitHub:     https://github.com/GodsScion/Auto_job_applier_linkedIn

Local "control panel" web app. It lets a non-technical person configure and run
the tool from a browser instead of editing Python files and using a terminal.

IMPORTANT - how configuration works:
  * This app reads/writes ONLY `user_config.json` at the project root.
  * It NEVER edits the config/*.py files.
  * The config/*.py modules load user_config.json over their built-in defaults
    (see config/_overrides.py), so saving here changes the tool's behaviour
    while leaving the classic "edit the .py files" workflow intact. With no
    user_config.json present the tool behaves exactly as it always has.

SECURITY: this app handles LinkedIn credentials, so it binds to 127.0.0.1 only
(never 0.0.0.0) and runs with debug OFF. Do not change these.
'''

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import csv
from datetime import datetime
import os
import sys
import json
import copy
import signal
import subprocess
import threading
import atexit
import importlib
import string
import tempfile
import time
import uuid
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, urlunsplit

import config_schema
from modules.candidate_registry import CandidateRegistry, REGISTRY_PATH, UNRESOLVED_PATH, sync_changed_config
from config import _overrides
from modules import updater
from modules.ai import prompts as ai_prompts
from modules.linkedin_comments import add_draft, get_by_url, list_drafts, update_status, refresh_draft
from modules.ai.request_queue import queued_call
from modules.ai import trace_store
from modules import automation_control
from modules import run_diagnostics
from modules import worker_process

# READING MAP
# 1. Imports and paths: tools this file uses and files it reads/writes.
# 2. Configuration: _load_defaults, _effective_config, and _coerce.
# 3. Bot process control: helpers that start, stop, and check runAiBot.py.
# 4. Web pages and job history: routes for /, /history, and /applied-jobs.
# 5. Control-panel API: routes the browser calls to configure and control runs.
# 6. AI, logs, and updates: routes for diagnostics and maintenance.
#
# Start with docs/app-reading-guide.md for plain-language explanations. This
# file serves the web panel; the job-search workflow mainly lives in runAiBot.py.

app = Flask(__name__)
CORS(app)

# Project root is the folder this file lives in.
ROOT = os.path.dirname(os.path.abspath(__file__))
USER_CONFIG_PATH = _overrides.USER_CONFIG_PATH
LOG_PATH = os.path.join(ROOT, ".bot_run.log")
PID_PATH = os.path.join(ROOT, ".bot_run.pid")
CONTROL_PATH = os.path.join(ROOT, ".bot_control.json")
DIAGNOSTICS_DIR = os.path.join(ROOT, 'logs', 'runs')

PATH = 'all excels/'


# ===========================================================================
# Default config values (the pristine config/*.py defaults, ignoring any
# user_config.json). Captured once at startup so /api/config can always show
# "default overlaid with the user's current saved values".
# ===========================================================================
def _load_defaults() -> dict:
    '''
    Import each config module with overrides temporarily disabled, so we read
    the untouched Python defaults regardless of whether user_config.json exists
    right now. Returns {config_module: {key: default_value}}.
    '''
    original_loader = _overrides.load_user_config
    _overrides.load_user_config = lambda: {}
    try:
        import config.secrets as _secrets
        import config.personals as _personals
        import config.questions as _questions
        import config.search as _search
        import config.settings as _settings
        modules = {
            "secrets": _secrets,
            "personals": _personals,
            "questions": _questions,
            "search": _search,
            "settings": _settings,
        }
        # Reload in case they were already imported (with real overrides) earlier.
        for module in modules.values():
            importlib.reload(module)
        defaults = {}
        for field in config_schema.iter_fields():
            module_name = field["config_module"]
            key = field["key"]
            module = modules.get(module_name)
            defaults.setdefault(module_name, {})[key] = getattr(module, key, None)
        return defaults
    finally:
        _overrides.load_user_config = original_loader


DEFAULTS = _load_defaults()


# ===========================================================================
# Config API helpers
# ===========================================================================
def _effective_config() -> dict:
    '''
    Return {config_module: {key: value}} of the pristine defaults overlaid with
    the CURRENT contents of user_config.json (re-read from disk on every call).
    Only keys defined in config_schema are included.
    '''
    effective = copy.deepcopy(DEFAULTS)
    user = _overrides.load_user_config()
    for field in config_schema.iter_fields():
        module_name = field["config_module"]
        key = field["key"]
        section = user.get(module_name)
        if isinstance(section, dict) and key in section:
            effective[module_name][key] = section[key]
    return effective


def _coerce(field_type: str, value):
    '''
    Coerce an incoming JSON value into the type declared for the field in the
    schema. Raises ValueError on invalid numbers so the caller can reject them.
    '''
    if field_type in ("text", "password", "textarea", "select"):
        return "" if value is None else str(value)

    if field_type == "number":
        if isinstance(value, bool):
            raise ValueError("expected a number, got a boolean")
        if isinstance(value, (int, float)):
            number = value
        else:
            text = str(value).strip()
            if text == "":
                raise ValueError("expected a number, got an empty value")
            number = float(text)
        # Keep whole numbers as ints (the config defaults are ints).
        if isinstance(number, float) and number.is_integer():
            return int(number)
        return number

    if field_type == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "yes", "on")

    if field_type == "list":
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip() != ""]
        text = str(value).strip()
        if text == "":
            return []
        return [item.strip() for item in text.split(",") if item.strip() != ""]

    # Unknown type: pass through untouched.
    return value


# ===========================================================================
# Bot subprocess management (run / stop / status / logs)
# ===========================================================================
_bot_proc = None
_bot_lock = threading.Lock()


def _bot_command():
    '''The command used to launch the bot. Isolated so tests can monkeypatch it.'''
    return [sys.executable, os.path.join(ROOT, "runAiBot.py")]


def _bot_environment():
    '''Remove GUI toolkit paths injected by the VS Code Snap launcher.

    Those paths make system Chrome load Snap's partial GSettings schemas and
    abort during startup (for example, missing GNOME antialiasing keys).
    Preserve display/session variables so Chrome can still open on Wayland.
    '''
    env = os.environ.copy()
    if not sys.platform.startswith('linux') or not any(
        '/snap/code/' in env.get(name, '')
        for name in ('GSETTINGS_SCHEMA_DIR', 'GTK_EXE_PREFIX', 'XDG_DATA_DIRS')
    ):
        return env
    for name in (
        "GSETTINGS_SCHEMA_DIR", "GIO_MODULE_DIR", "GTK_EXE_PREFIX", "GTK_PATH",
        "GTK_IM_MODULE_FILE", "GTK_MODULES", "XDG_DATA_DIRS",
    ):
        env.pop(name, None)
    env["GSETTINGS_SCHEMA_DIR"] = "/usr/share/glib-2.0/schemas"
    env["XDG_DATA_DIRS"] = "/usr/local/share:/usr/share/ubuntu:/usr/share/gnome:/usr/share:/var/lib/snapd/desktop"
    for name in tuple(env):
        if name.endswith("_VSCODE_SNAP_ORIG"):
            env.pop(name, None)
    return env


def _copy_worker_output(source, log_path):
    """Persist streamed worker output after redacting common personal/secret values."""
    try:
        with open(log_path, 'a', encoding='utf-8') as log_file:
            for line in iter(source.readline, ''):
                log_file.write(run_diagnostics.sanitize_log_line(line))
                log_file.flush()
    except (OSError, ValueError):
        pass
    finally:
        try: source.close()
        except Exception: pass


def _is_running() -> bool:
    '''Reconcile this dashboard's handle or reconnect through the validated PID record.'''
    global _bot_proc
    if _bot_proc is not None:
        return_code = _bot_proc.poll()
        if return_code is None:
            return True
        _reconcile_worker_exit(return_code)
        return False
    return _orphaned_bot_pid() is not None


def _process_identity(pid):
    """Keep project paths here; delegate OS-specific identity checks."""
    return worker_process.identify_job_bot(pid, ROOT, sys.executable)


def _read_worker_record():
    try:
        with open(PID_PATH, encoding='utf-8') as stream:
            raw = stream.read()
        try:
            value = json.loads(raw)
        except ValueError:
            # Upgrade a legacy PID-only record only when /proc proves the exact
            # worker script is still running and the control state has a run ID.
            if os.name == 'nt' or not raw.strip().isdigit():
                return None
            pid = int(raw.strip())
            control_run_id = automation_control.status(CONTROL_PATH).get('run_id')
            identity = _process_identity(pid)
            if not run_diagnostics.valid_run_id(control_run_id) or not identity:
                return None
            value = {'pid': pid, 'run_id': control_run_id,
                     'start_ticks': identity['start_ticks'], 'script': identity['script']}
            temp_path = PID_PATH + '.tmp'
            with open(temp_path, 'w', encoding='utf-8') as stream:
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, PID_PATH)
        if not isinstance(value, dict):
            return None
        pid, run_id = value.get('pid'), value.get('run_id')
        if not isinstance(pid, int) or pid <= 1 or not run_diagnostics.valid_run_id(run_id):
            return None
        if automation_control.status(CONTROL_PATH).get('run_id') != run_id:
            return None
        identity = _process_identity(pid)
        expected_script = os.path.realpath(os.path.join(ROOT, 'runAiBot.py'))
        if (not identity or identity['start_ticks'] != str(value.get('start_ticks'))
                or identity.get('script') != expected_script):
            return None
        return value
    except (OSError, ValueError, TypeError):
        return None


def _orphaned_bot_pid():
    """Find a validated worker from a prior dashboard process."""
    record = _read_worker_record()
    if record:
        return record['pid']
    _remove_pid_file()
    return None


def _current_bot_pid():
    if _bot_proc is not None and _bot_proc.poll() is None:
        return _bot_proc.pid
    return _orphaned_bot_pid()


def _reconcile_missing_run():
    control = automation_control.status(CONTROL_PATH)
    run_id = control.get('run_id')
    if not run_diagnostics.valid_run_id(run_id):
        return None
    result = run_diagnostics.get_run(run_id, DIAGNOSTICS_DIR)
    if result and result.get('status') not in run_diagnostics.TERMINAL:
        stage = result.get('current_stage') or 'startup'
        code = 'STARTUP_FAILED' if stage == 'startup' else 'PROCESS_CRASHED'
        result = run_diagnostics.finish(run_id, 'failed', code,
            'The worker process is no longer active and did not report a final outcome.',
            root=DIAGNOSTICS_DIR)
    automation_control.set_stopped(run_id, CONTROL_PATH)
    return result


def _reconcile_worker_exit(return_code=None):
    run_id = automation_control.status(CONTROL_PATH).get('run_id')
    if run_id:
        run_diagnostics.reconcile_exit(run_id, return_code, DIAGNOSTICS_DIR)
        if return_code is not None:
            run_diagnostics.record_exit_code(run_id, return_code, DIAGNOSTICS_DIR)
        automation_control.set_stopped(run_id, CONTROL_PATH)
    _remove_pid_file()


def _remove_pid_file():
    try:
        os.remove(PID_PATH)
    except OSError:
        pass


def _terminate(proc):
    """Keep route compatibility while delegating OS-level termination."""
    worker_process.stop_job_bot_process(proc, ROOT)


def _terminate_orphaned_bot() -> bool:
    '''Stop a validated worker recorded by another dashboard (explicit Stop only).'''
    record = _read_worker_record()
    pid = record['pid'] if record else None
    if pid is None:
        _remove_pid_file()
        return False
    if os.name == 'nt':
        try:
            result = subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    if sys.platform.startswith('linux'):
        return worker_process.stop_verified_linux_process_group(
            pid, ROOT, record.get('start_ticks'))
    try:
        os.killpg(pid, signal.SIGTERM)
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            threading.Event().wait(0.1)
        os.killpg(pid, signal.SIGKILL)
        return True
    except (OSError, ProcessLookupError):
        return False


def _stop_bot_on_exit(*_args) -> None:
    """Dashboard shutdown is independent; leave automation and its PID record intact."""
    return None


def _handle_shutdown_signal(signum, _frame) -> None:
    """Convert terminal/OS shutdown signals into a clean panel shutdown."""
    _stop_bot_on_exit()
    raise SystemExit(128 + signum)


# The update bar is appended to the rendered page instead of being written into
# control_panel.html, so that template stays a pure offline document.
# ponytail: string append at </body>; move it into the template if it ever needs
# to be more than a one-line banner.
_UPDATE_BAR = '''
<div id="updateBar" style="display:none;position:sticky;bottom:0;z-index:10;background:#fffbeb;
     border-top:1px solid #dce1e8;padding:10px 16px;font:14px -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#1f2733">
  <span id="updateText"></span>
  <button id="updateBtn" style="margin-left:8px;padding:4px 12px;border:0;border-radius:6px;background:#2563eb;color:#fff;cursor:pointer">Update now</button>
</div>
<script>
(function () {
    var bar = document.getElementById('updateBar');
    var text = document.getElementById('updateText');
    var btn = document.getElementById('updateBtn');
    // Fired after the page is already interactive, so a slow or dead network
    // delays nothing. A failed check simply leaves the bar hidden.
    fetch('/api/update-check').then(function (r) { return r.json(); }).then(function (d) {
        if (!d.update_available) return;
        text.textContent = 'Update available: ' + d.current + ' -> ' + d.latest;
        bar.style.display = 'block';
    }).catch(function () {});
    btn.addEventListener('click', function () {
        btn.disabled = true;
        text.textContent = 'Updating, please wait...';
        fetch('/api/update', {method: 'POST'}).then(function (r) { return r.json(); }).then(function (d) {
            text.textContent = d.ok ? 'Updated. Close this window and start the app again.'
                                    : 'Update failed. ' + d.message;
            btn.style.display = 'none';
        }).catch(function (err) { text.textContent = 'Update failed. ' + err; btn.disabled = false; });
    });
})();
</script>
'''


@app.route('/')
def home():
    """Serve the control panel single-page app, with the update bar appended."""
    return render_template('control_panel.html').replace('</body>', _UPDATE_BAR + '</body>', 1)


@app.route('/history')
def history():
    """Serve the applied-jobs history page."""
    return render_template('index.html')


# The browser's job-history page asks this app for rows from the CSV file.
# The applied-jobs history CSV the bot writes, and how its columns map to the JSON
# keys the history page consumes.
_HISTORY_CSV = 'all_applied_applications_history.csv'
_HISTORY_FIELDS = {
    'Job ID': 'Job_ID',
    'Title': 'Title',
    'Company': 'Company',
    'HR Name': 'HR_Name',
    'HR Link': 'HR_Link',
    'Job Link': 'Job_Link',
    'External Job link': 'External_Job_link',
    'Date Applied': 'Date_Applied',
}


@app.route('/applied-jobs', methods=['GET'])
def get_applied_jobs():
    """Return the applied-jobs history as JSON for the history page."""
    csv_path = os.path.join(PATH, _HISTORY_CSV)
    if not os.path.exists(csv_path):
        return jsonify({"error": "No applications history found yet."}), 404
    try:
        jobs = []
        with open(csv_path, 'r', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                jobs.append({key: row.get(col, '') for col, key in _HISTORY_FIELDS.items()})
        return jsonify(jobs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/applied-jobs/<job_id>', methods=['PUT'])
def mark_job_applied(job_id):
    """Stamp one job's 'Date Applied' (matched by Job ID) with the current time."""
    csv_path = os.path.join(PATH, _HISTORY_CSV)
    if not os.path.exists(csv_path):
        return jsonify({"error": f"History file not found at {csv_path}"}), 404
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            columns = reader.fieldnames
            rows = list(reader)
        matched = False
        for row in rows:
            if row.get('Job ID') == job_id:
                row['Date Applied'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                matched = True
        if not matched:
            return jsonify({"error": f"Job ID {job_id} not found"}), 404
        with open(csv_path, 'w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        return jsonify({"message": "Date Applied updated."}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===========================================================================
# Control-panel API
# ===========================================================================
# Configuration routes: the browser reads the schema and saves user settings.
@app.route('/api/schema', methods=['GET'])
def api_schema():
    '''Returns the field schema the UI renders its forms from.'''
    return jsonify(config_schema.SCHEMA)


@app.route('/api/config', methods=['GET'])
def api_get_config():
    '''
    Returns the effective config: pristine defaults overlaid with the current
    user_config.json, grouped by config module (secrets, personals, questions,
    search, settings).
    '''
    return jsonify(_effective_config())


@app.route('/api/config', methods=['POST'])
def api_save_config():
    '''
    Accepts {config_module: {key: value}}, validates against the schema, coerces
    each value to its declared type, rejects unknown modules/keys, merges into
    user_config.json (read-modify-write) and returns the full saved config.
    '''
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected a JSON object of {section: {key: value}}"}), 400

    valid = config_schema.valid_keys()
    unknown = []
    coerced = {}

    for section, values in payload.items():
        if not isinstance(values, dict):
            return jsonify({"error": f"Section '{section}' must be an object"}), 400
        if section not in valid:
            unknown.append(section)
            continue
        for key, value in values.items():
            field = valid[section].get(key)
            if field is None:
                unknown.append(f"{section}.{key}")
                continue
            try:
                coerced.setdefault(section, {})[key] = _coerce(field["type"], value)
            except ValueError as err:
                return jsonify({"error": f"Invalid value for '{section}.{key}': {err}"}), 400

    if unknown:
        return jsonify({"error": "Unknown settings rejected", "unknown": unknown}), 400

    # Read-modify-write user_config.json.
    current = _overrides.load_user_config()
    previous = copy.deepcopy(current)
    existed = os.path.exists(USER_CONFIG_PATH)
    for section, values in coerced.items():
        target = current.get(section)
        if not isinstance(target, dict):
            target = {}
        target.update(values)
        current[section] = target

    try:
        with open(USER_CONFIG_PATH, "w", encoding="utf-8") as file:
            json.dump(current, file, indent=2, ensure_ascii=False)
        if os.name != 'nt': os.chmod(USER_CONFIG_PATH, 0o600)
        sync_changed_config(coerced, current, path=REGISTRY_PATH, unresolved_path=UNRESOLVED_PATH)
    except (OSError, ValueError, KeyError, TypeError) as err:
        try:
            if existed:
                with open(USER_CONFIG_PATH, 'w', encoding='utf-8') as file:
                    json.dump(previous, file, indent=2, ensure_ascii=False)
            elif os.path.exists(USER_CONFIG_PATH):
                os.remove(USER_CONFIG_PATH)
        except OSError:
            pass
        return jsonify({"error": f"Could not save settings: {err}"}), 500

    return jsonify(current)


@app.route('/api/unresolved-questions', methods=['GET'])
def api_unresolved_questions():
    if not UNRESOLVED_PATH.exists(): return jsonify([])
    try:
        with open(UNRESOLVED_PATH, encoding='utf-8') as file:
            data = json.load(file)
        return jsonify([{'question': item.get('question', ''), 'count': item.get('count', 1)}
                        for item in data.get('questions', []) if item.get('status') == 'unresolved'])
    except (OSError, ValueError):
        return jsonify({'error': 'Could not read unanswered questions.'}), 500


@app.route('/api/candidate-answers', methods=['POST'])
def api_candidate_answer():
    payload = request.get_json(silent=True) or {}
    question = str(payload.get('question', '')).strip()
    answer = str(payload.get('answer', '')).strip()
    unit = str(payload.get('unit', '')).strip()
    if not question or not answer or len(question) > 1000 or len(answer) > 4000 or len(unit) > 80:
        return jsonify({'error': 'Enter a question and an answer.'}), 400
    if not REGISTRY_PATH.exists() or not UNRESOLVED_PATH.exists():
        return jsonify({'error': 'No unanswered question is available to update.'}), 404
    try:
        with open(UNRESOLVED_PATH, encoding='utf-8') as file:
            data = json.load(file)
        if not any(item.get('question') == question and item.get('status') == 'unresolved'
                   for item in data.get('questions', [])):
            return jsonify({'error': 'That question is no longer unresolved.'}), 404
        CandidateRegistry(REGISTRY_PATH, UNRESOLVED_PATH).save_exact_answer(question, answer, unit)
        return jsonify({'saved': True})
    except (OSError, ValueError, KeyError):
        return jsonify({'error': 'Could not save the verified answer.'}), 500


# Run-control routes: these start, pause, resume, stop, and report on the bot.
@app.route('/api/run', methods=['POST'])
def api_run():
    '''Starts the bot as a subprocess if it isn't already running.'''
    global _bot_proc
    with automation_control.lifecycle_lock(CONTROL_PATH), _bot_lock:
        if _is_running():
            control = automation_control.status(CONTROL_PATH)
            return jsonify({"running": True, "pid": _current_bot_pid(),
                            "run_id": control.get('run_id'), "state": control.get('state', 'running'),
                            "message": "The tool is already running."})
        try:
            _reconcile_missing_run()
            run_id = uuid.uuid4().hex
            run_diagnostics.create_run(run_id, DIAGNOSTICS_DIR)
            automation_control.create_run(run_id, CONTROL_PATH)
            run_log = run_diagnostics.run_dir(run_id, DIAGNOSTICS_DIR) / 'worker.log'
            run_log.touch(exist_ok=True)
            popen_kwargs = {
                "cwd": ROOT,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "env": _bot_environment(),
                "text": True,
                "bufsize": 1,
            }
            popen_kwargs['env']['BOT_RUN_ID'] = run_id
            popen_kwargs['env']['PYTHONUNBUFFERED'] = '1'
            if os.name == "nt":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True
            _bot_proc = subprocess.Popen(_bot_command(), **popen_kwargs)
            worker_stdout = getattr(_bot_proc, 'stdout', None)
            if worker_stdout is not None:
                logger_kwargs = {
                    'cwd': ROOT, 'stdin': worker_stdout,
                    'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL,
                }
                if os.name == 'nt':
                    logger_kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    logger_kwargs['start_new_session'] = True
                try:
                    subprocess.Popen([sys.executable, '-m', 'modules.worker_log_writer', str(run_log)],
                                     **logger_kwargs)
                    worker_stdout.close()
                except Exception:
                    threading.Thread(target=_copy_worker_output,
                        args=(worker_stdout, str(run_log)), daemon=True).start()
        except Exception as err:
            if 'run_id' in locals():
                run_diagnostics.fail(run_id, 'STARTUP_FAILED',
                                     'The automation worker could not be started.',
                                     err, failed_stage='startup', exit_code=1,
                                     root=DIAGNOSTICS_DIR)
                automation_control.set_stopped(run_id, CONTROL_PATH)
            if 'log_file' in locals():
                try: log_file.close()
                except Exception: pass
            return jsonify({"running": False, "error": str(err)}), 500
        identity = _process_identity(_bot_proc.pid)
        try:
            if identity:
                temp_path = PID_PATH + '.tmp'
                with open(temp_path, 'w', encoding='utf-8') as pid_file:
                    json.dump({'pid': _bot_proc.pid, 'run_id': run_id,
                               'start_ticks': identity['start_ticks'],
                               'script': identity['script']}, pid_file)
                    pid_file.flush()
                    os.fsync(pid_file.fileno())
                os.replace(temp_path, PID_PATH)
        except OSError:
            pass
        return jsonify({"running": True, "pid": _current_bot_pid(), "run_id": run_id, "state": "running"})


@app.route('/api/pause', methods=['POST'])
def api_pause():
    with automation_control.lifecycle_lock(CONTROL_PATH), _bot_lock:
        if not _is_running():
            return jsonify({'running': False, 'state': 'stopped'}), 409
        current = automation_control.status(CONTROL_PATH)
        result = automation_control.request('pause', current.get('run_id'), CONTROL_PATH)
        if result is None:
            return jsonify({'error': 'No active automation run.'}), 409
        return jsonify({'running': True, 'run_id': result['run_id'], 'state': result['state']})


@app.route('/api/resume', methods=['POST'])
def api_resume():
    with automation_control.lifecycle_lock(CONTROL_PATH), _bot_lock:
        if not _is_running():
            return jsonify({'running': False, 'state': 'stopped'}), 409
        current = automation_control.status(CONTROL_PATH)
        result = automation_control.request('resume', current.get('run_id'), CONTROL_PATH)
        if result is None:
            return jsonify({'error': 'No active automation run.'}), 409
        return jsonify({'running': True, 'run_id': result['run_id'], 'state': result['state']})


@app.route('/api/stop', methods=['POST'])
def api_stop():
    '''Stops the running bot subprocess (and its children where possible).'''
    global _bot_proc
    with automation_control.lifecycle_lock(CONTROL_PATH), _bot_lock:
        running = _is_running()
        run_id = automation_control.status(CONTROL_PATH).get('run_id')
        proc = _bot_proc
        if proc is not None:
            stopped = _terminate(proc)
            if stopped is False and proc.poll() is None:
                return jsonify({'running': True, 'run_id': run_id,
                                'error': 'Could not safely verify and stop the job bot process.'}), 409
            _bot_proc = None
        else:
            stopped = _terminate_orphaned_bot()
            if running and not stopped:
                return jsonify({'running': True, 'run_id': run_id,
                                'error': 'Could not safely verify and stop the job bot process.'}), 409
        if running and run_id:
            run_diagnostics.finish(run_id, 'stopped', 'USER_STOPPED',
                                   'Automation was stopped by the user.',
                                   root=DIAGNOSTICS_DIR)
        if run_id and proc is not None and getattr(proc, 'returncode', None) is not None:
            process_code = getattr(proc, 'returncode', None)
            run_diagnostics.record_exit_code(run_id, process_code, DIAGNOSTICS_DIR)
        if run_id:
            automation_control.set_stopped(run_id, CONTROL_PATH)
        _remove_pid_file()
        return jsonify({"running": False, "state": "stopped", "run_id": run_id})


@app.route('/api/status', methods=['GET'])
def api_status():
    '''Reports whether the bot subprocess is currently running.'''
    with automation_control.lifecycle_lock(CONTROL_PATH), _bot_lock:
        running = _is_running()
        pid = _current_bot_pid() if running else None
        control = automation_control.status(CONTROL_PATH)
        if running:
            state = control.get('state', 'running')
        else:
            state = 'stopped'
            _reconcile_missing_run()
        run_id = control.get('run_id')
        run = None
        if run_id and run_diagnostics.valid_run_id(run_id):
            run = run_diagnostics._read_result(run_diagnostics.run_dir(run_id, DIAGNOSTICS_DIR))
        if not running and run and run.get('status') in run_diagnostics.TERMINAL:
            state = run['status']
        if run is None:
            recent = run_diagnostics.list_runs(DIAGNOSTICS_DIR, limit=1)
            run = recent[0] if recent else None
            run_id = run.get('run_id') if run else None
        return jsonify({"running": running, "pid": pid, "run_id": run_id,
                        "state": state, "run": run,
                        "recent_runs": run_diagnostics.list_runs(DIAGNOSTICS_DIR, limit=30)})


@app.route('/api/runs', methods=['GET'])
def api_runs():
    return jsonify(run_diagnostics.list_runs(DIAGNOSTICS_DIR, limit=30))


@app.route('/api/runs/<run_id>', methods=['GET'])
def api_run_details(run_id):
    if not run_diagnostics.valid_run_id(run_id):
        return jsonify({'error': 'Invalid run ID.'}), 400
    try:
        result = run_diagnostics.get_run(run_id, DIAGNOSTICS_DIR)
    except ValueError:
        return jsonify({'error': 'Invalid run ID.'}), 400
    if result is None:
        return jsonify({'error': 'Run not found.'}), 404
    return jsonify(result)


@app.route('/api/runs/<run_id>/log', methods=['GET'])
def api_run_log(run_id):
    if not run_diagnostics.valid_run_id(run_id):
        return jsonify({'error': 'Invalid run ID.'}), 400
    try:
        log_path = run_diagnostics.run_dir(run_id, DIAGNOSTICS_DIR) / 'worker.log'
        offset = max(0, int(request.args.get('offset', 0)))
        with open(log_path, 'rb') as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            if offset > size: offset = 0
            stream.seek(offset)
            data = stream.read(1024 * 1024)
        return jsonify({'content': data.decode('utf-8', errors='replace'),
                        'next_offset': offset + len(data)})
    except (OSError, ValueError):
        return jsonify({'error': 'Run log not found.'}), 404


# LinkedIn feed and comment-draft routes power the optional feed-review tools.
@app.route('/api/comment-drafts', methods=['GET'])
def api_comment_drafts():
    """List locally saved comment drafts; this endpoint never accesses LinkedIn."""
    return jsonify(list_drafts())


@app.route('/api/linkedin-feed/scan', methods=['POST'])
def api_scan_linkedin_feed():
    """Read feed posts, score them against candidate context, and post relevant replies."""
    try:
        from modules.linkedin_feed import scan_feed, post_comment, log_event
        from modules.ai.connections import create_ai_client, close_ai_client
        from modules.ai.prompts import linkedin_comment_prompt
        from modules.ai.prompt_log import record_prompt
        payload = request.get_json(silent=True) or {}
        log_event('--- New sequential feed review run started ---')
        with open(os.path.join(ROOT, 'candidate_context.md'), encoding='utf-8') as handle:
            context = handle.read()
        log_event('Candidate context loaded; chars=%d', len(context))
        user_secrets = _overrides.load_user_config().get('secrets', {})
        import config.secrets as secrets
        setting = lambda name: user_secrets.get(name, getattr(secrets, name, None))
        if setting('local_ai_enabled'):
            log_event('Creating AI client from configured local provider')
            client = create_ai_client(model_name=setting('local_ai_model'),
                provider=setting('local_ai_provider'), api_url=setting('local_ai_api_url'),
                api_key=setting('local_ai_api_key'), force=True)
        else:
            log_event('Creating AI client from configured default provider')
            client = create_ai_client(force=True)
        if client is None:
            log_event('AI client unavailable; no posts evaluated or published')
            return jsonify({'error': 'The configured AI model is unavailable.'}), 503
        outcomes = []
        posted_count = 0

        def review_post(post):
            nonlocal posted_count
            post_number = len(outcomes) + 1
            log_event('AI review started for post %d before feed advances', post_number)
            existing = get_by_url(post['post_url'])
            if existing and existing.get('status') == 'posted':
                log_event('Post skipped: already handled with status=%s', existing.get('status', 'unknown'))
                outcome = {'post_url': post['post_url'], 'author': post['author'],
                    'status': 'already handled', 'rationale': existing.get('rationale', '')}
                outcomes.append(outcome)
                return outcome
            if existing:
                log_event('Rechecking prior unposted record with status=%s', existing.get('status', 'unknown'))
            prompt = linkedin_comment_prompt.format(candidate_context=context[:12000],
                author=post['author'] or 'Not provided', post_text=post['post_text'])
            log_event('Calling AI for current feed post')
            response = queued_call(client.model.invoke, prompt)
            log_event('AI response received for current feed post')
            raw = getattr(response, 'content', str(response))
            if isinstance(raw, list):
                raw = ''.join(str(block.get('text', '')) if isinstance(block, dict) else str(block) for block in raw)
            raw = str(raw).strip()
            record_prompt('LinkedIn feed relevance and comment', prompt, raw, client.model_name, client.provider)
            if raw.startswith('```'):
                raw = raw.strip('`').removeprefix('json').strip()
            suggestion = json.loads(raw)
            rationale = str(suggestion.get('rationale', '')).strip()[:500]
            comment = str(suggestion.get('comment', '')).strip()[:500]
            if suggestion.get('relevant') is not True or not comment:
                log_event('AI result: not relevant; rationale_chars=%d', len(rationale))
                skipped_id = add_draft(post['post_url'], post['post_text'], post['author'], rationale, '')
                update_status(skipped_id, 'skipped')
                outcome = {'post_url': post['post_url'], 'author': post['author'],
                    'status': 'not relevant', 'rationale': rationale}
                outcomes.append(outcome)
                return outcome

            if existing:
                draft_id = existing['id']
                refresh_draft(draft_id, post['post_url'], post['post_text'], post['author'], rationale, comment)
            else:
                draft_id = add_draft(post['post_url'], post['post_text'], post['author'], rationale, comment)
            log_event('AI result: relevant; comment draft saved as id=%s', draft_id)
            posted = post_comment(post['post_url'], comment,
                                  card_ref=post.get('_card_ref'), lock_held=True)
            if posted['posted']:
                update_status(draft_id, 'posted')
                posted_count += 1
                log_event('Published current post comment; posted_count=%d', posted_count)
                outcome = {'post_url': post['post_url'], 'author': post['author'],
                    'status': 'posted', 'rationale': rationale, 'comment': comment}
            else:
                log_event('Comment not confirmed as posted: %s', posted.get('error', 'unknown reason'))
                update_status(draft_id, 'error', posted.get('error', 'Could not confirm posting.'))
                outcome = {'post_url': post['post_url'], 'author': post['author'],
                    'status': 'not posted', 'rationale': rationale,
                    'error': posted.get('error', 'Could not confirm posting.'), 'comment': comment,
                    '_halt_scan': True}
            outcomes.append(outcome)
            return outcome

        try:
            result = scan_feed(limit=payload.get('limit', 15), scrolls=payload.get('scrolls', 5),
                               on_post=review_post)
            if result.get('needs_login'):
                return jsonify(result)
            log_event('Sequential feed run complete: scanned=%d posted=%d', len(result['posts']), posted_count)
            return jsonify({'needs_login': False, 'results': outcomes, 'posted_count': posted_count,
                            'scanned_count': len(result['posts']), 'halted': result.get('halted', False)})
        finally:
            close_ai_client(client)
    except (TypeError, ValueError) as error:
        return jsonify({'error': str(error)}), 400
    except RuntimeError as error:
        return jsonify({'error': str(error)}), 409
    except Exception as error:
        try:
            from modules.linkedin_feed import log_event
            log_event('Feed run failed with %s', type(error).__name__)
        except Exception:
            pass
        app.logger.exception('LinkedIn feed scan failed')
        return jsonify({'error': f'Feed scan failed: {error}'}), 500


@app.route('/api/linkedin-feed/logs', methods=['GET'])
def api_linkedin_feed_logs():
    from modules.linkedin_feed import read_log_tail
    return jsonify({'logs': read_log_tail()})


@app.route('/api/linkedin-feed/close', methods=['POST'])
def api_close_linkedin_feed():
    from modules.linkedin_feed import close_feed_browser
    close_feed_browser()
    return jsonify({'closed': True})


@app.route('/api/comment-drafts', methods=['POST'])
def api_create_comment_draft():
    """Draft a comment from post text the user supplied, without opening LinkedIn."""
    payload = request.get_json(silent=True) or {}
    post_url = str(payload.get('post_url', '')).strip()
    post_text = str(payload.get('post_text', '')).strip()
    author = str(payload.get('author', '')).strip()[:200]
    if not post_url.startswith('https://www.linkedin.com/') or len(post_url) > 2000:
        return jsonify({'error': 'Enter the LinkedIn post URL.'}), 400
    if not post_text or len(post_text) > 8000:
        return jsonify({'error': 'Paste the post text (up to 8,000 characters).'}), 400
    existing = get_by_url(post_url)
    if existing:
        return jsonify({'error': 'A draft or skip record already exists for this post.', 'draft': existing}), 409
    try:
        with open(os.path.join(ROOT, 'candidate_context.md'), encoding='utf-8') as handle:
            context = handle.read()
        user_secrets = _overrides.load_user_config().get('secrets', {})
        import config.secrets as secrets
        setting = lambda name: user_secrets.get(name, getattr(secrets, name, None))
        from modules.ai.connections import create_ai_client, close_ai_client
        from modules.ai.prompts import linkedin_comment_prompt
        from modules.ai.prompt_log import record_prompt
        if setting('local_ai_enabled'):
            client = create_ai_client(model_name=setting('local_ai_model'),
                provider=setting('local_ai_provider'), api_url=setting('local_ai_api_url'),
                api_key=setting('local_ai_api_key'), force=True)
        else:
            client = create_ai_client(force=True)
        if client is None:
            return jsonify({'error': 'The configured LLM is unavailable.'}), 503
        prompt = linkedin_comment_prompt.format(candidate_context=context[:12000],
                                                  author=author or 'Not provided', post_text=post_text)
        started = time.perf_counter()
        trace_recorded = False
        try:
            response = queued_call(client.model.invoke, prompt)
            raw = getattr(response, 'content', str(response))
            if isinstance(raw, list):
                raw = ''.join(str(block.get('text', '')) if isinstance(block, dict) else str(block) for block in raw)
            raw = str(raw).strip()
            parse_value = raw
            if raw.startswith('```'):
                parse_value = raw.strip('`').removeprefix('json').strip()
            try:
                result = json.loads(parse_value)
            except (json.JSONDecodeError, TypeError) as error:
                record_prompt('LinkedIn comment draft', prompt, raw, client.model_name, client.provider,
                    error=error, latency_ms=(time.perf_counter() - started) * 1000,
                    retry_info={'attempt': 1, 'mode': 'json'})
                trace_recorded = True
                raise
            if not isinstance(result, dict):
                error = ValueError('Model response must be a JSON object.')
                record_prompt('LinkedIn comment draft', prompt, raw, client.model_name, client.provider,
                    error=error, latency_ms=(time.perf_counter() - started) * 1000,
                    retry_info={'attempt': 1, 'mode': 'json'})
                trace_recorded = True
                raise error
            record_prompt('LinkedIn comment draft', prompt, raw, client.model_name, client.provider,
                parsed_output=result, latency_ms=(time.perf_counter() - started) * 1000,
                retry_info={'attempt': 1, 'mode': 'json'})
            trace_recorded = True
            relevant = result.get('relevant') is True
            rationale = str(result.get('rationale', '')).strip()[:500]
            comment = str(result.get('comment', '')).strip()[:500] if relevant else ''
            if not relevant or not comment:
                return jsonify({'relevant': False, 'rationale': rationale,
                                'message': 'No sufficiently grounded comment was generated.'}), 200
            draft_id = add_draft(post_url, post_text, author, rationale, comment)
            return jsonify({'relevant': True, 'draft': get_by_url(post_url) or {'id': draft_id}}), 201
        except Exception as error:
            if not trace_recorded:
                record_prompt('LinkedIn comment draft', prompt, model=client.model_name,
                    provider=client.provider, error=error,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    retry_info={'attempt': 1, 'mode': 'json'})
            raise
        finally:
            close_ai_client(client)
    except (OSError, ValueError, KeyError) as error:
        return jsonify({'error': f'Could not create a grounded draft: {error}'}), 500
    except Exception as error:
        return jsonify({'error': f'LLM request failed: {error}'}), 502


@app.route('/api/comment-drafts/<int:draft_id>/skip', methods=['POST'])
def api_skip_comment_draft(draft_id):
    if not update_status(draft_id, 'skipped'):
        return jsonify({'error': 'Draft not found or already handled.'}), 404
    return jsonify({'skipped': True})


# AI routes expose model status, prompts, and saved request traces to the panel.
@app.route('/api/model-status', methods=['GET', 'POST'])
def api_model_status():
    """Report Ollama availability and the models installed on its configured server."""
    secrets = importlib.import_module('config.secrets')
    user_secrets = _overrides.load_user_config().get('secrets', {})
    def setting(name):
        return user_secrets.get(name, getattr(secrets, name, None))

    payload = request.get_json(silent=True) if request.method == 'POST' else None
    enabled = bool(setting('local_ai_enabled'))
    model = str((payload or {}).get('model', setting('local_ai_model')) or '')
    provider = str((payload or {}).get('provider', setting('local_ai_provider')) or '').lower()
    api_url = str((payload or {}).get('api_url', setting('local_ai_api_url')) or '')
    result = {"enabled": enabled, "model": model, "provider": provider,
              "models": [], "status": "disabled" if not enabled else "unavailable"}
    if request.method == 'GET' and not enabled:
        return jsonify(result)
    if provider != 'ollama' or not api_url:
        return jsonify(result)

    parsed = urlsplit(api_url)
    base = urlunsplit((parsed.scheme, parsed.netloc, '', '', '')).rstrip('/')
    try:
        with urlopen(base + '/api/ps', timeout=1.5) as response:
            running = json.loads(response.read().decode('utf-8')).get('models', [])
        with urlopen(base + '/api/tags', timeout=1.5) as response:
            installed = json.loads(response.read().decode('utf-8')).get('models', [])
        names = lambda items: [str(item.get('name', item.get('model', ''))) for item in items]
        result['models'] = names(installed)
        # Ollama may expose a tag-qualified name even when the UI has the
        # equivalent unqualified name (and vice versa).
        matches = lambda name, candidates: name in candidates or name.split(':')[0] in [v.split(':')[0] for v in candidates]
        result['status'] = ('running' if matches(model, names(running)) else
                            'installed' if matches(model, result['models']) else 'missing')
    except Exception:
        result['status'] = 'offline'
    return jsonify(result)


@app.route('/api/ollama-test', methods=['POST'])
def api_ollama_test():
    """Run a small local inference request with the current Ollama form settings."""
    payload = request.get_json(silent=True) or {}
    provider = str(payload.get('provider', '')).strip().lower()
    model = str(payload.get('model', '')).strip()
    api_url = str(payload.get('api_url', '')).strip()
    if provider != 'ollama':
        return jsonify({'ok': False, 'error': 'Select Ollama to run this test.'}), 400
    if not model:
        return jsonify({'ok': False, 'error': 'Enter an Ollama model name first.'}), 400
    try:
        num_ctx = int(payload.get('num_ctx', 8192))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Context size must be a whole number of tokens.'}), 400
    if num_ctx < 512 or num_ctx > 262144:
        return jsonify({'ok': False, 'error': 'Context size must be between 512 and 262144 tokens.'}), 400
    keep_alive = str(payload.get('keep_alive', '5m')).strip() or '5m'
    if len(keep_alive) > 32:
        return jsonify({'ok': False, 'error': 'Keep-alive value is too long.'}), 400
    parsed = urlsplit(api_url)
    base = urlunsplit((parsed.scheme, parsed.netloc, '', '', '')).rstrip('/')
    if not base or parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return jsonify({'ok': False, 'error': 'Enter a valid Ollama API URL.'}), 400

    body = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': 'Reply with exactly: READY'}],
        'stream': False,
        'options': {'num_ctx': num_ctx},
        'keep_alive': keep_alive,
    }).encode('utf-8')
    try:
        req = Request(base + '/api/chat', data=body,
                      headers={'Content-Type': 'application/json'}, method='POST')
        with urlopen(req, timeout=120) as response:
            result = json.loads(response.read().decode('utf-8'))
        answer = str((result.get('message') or {}).get('content') or '').strip()
        if not answer:
            return jsonify({'ok': False, 'error': 'Ollama responded, but the model returned an empty answer.'}), 502
        return jsonify({'ok': True, 'model': model,
                        'message': f'Ollama connection and model test succeeded ({model}, context {num_ctx}, keep-alive {keep_alive}).'})
    except Exception as error:
        return jsonify({'ok': False, 'error': f'Ollama test failed: {error}'}), 502


@app.route('/api/ai-prompts', methods=['GET'])
def api_ai_prompts():
    """Return the newest local prompt records for the control-panel viewer."""
    path = os.path.join(ROOT, '.ai_prompt_log.jsonl')
    try:
        with open(path, encoding='utf-8') as handle:
            lines = handle.readlines()[-250:]
        records = []
        for line in reversed(lines):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return jsonify(records)
    except FileNotFoundError:
        return jsonify([])
    except OSError:
        return jsonify({'error': 'Could not read the local AI prompt log.'}), 500


@app.route('/api/ai-traces', methods=['GET'])
def api_ai_traces():
    """Return paginated, filtered summaries from the local SQLite trace store."""
    query = request.args
    filters = {
        'query': query.get('q', ''), 'decision': '__none__' if query.get('decision') == 'OTHER' else query.get('decision', ''),
        'model': query.get('model', ''), 'category': query.get('category', ''),
        'status': query.get('status', ''), 'provider': query.get('provider', ''),
        'run_id': query.get('run_id', ''), 'job_id': query.get('job_id', ''),
        'since': query.get('since', query.get('from', '')),
        'until': query.get('until', query.get('to', '')),
    }
    try:
        page = trace_store.list_traces(filters=filters,
            limit=query.get('limit', 50), offset=query.get('offset', 0))
    except (TypeError, ValueError) as error:
        return jsonify({'error': str(error)}), 400
    for item in page['items']:
        item['final_decision'] = item.get('decision')
        item['retry_count'] = _ai_trace_retry_count(item.get('retry_info'))
    page['has_more'] = page['offset'] + len(page['items']) < page['total']
    return jsonify(page)


@app.route('/api/ai-traces/<trace_id>', methods=['GET'])
def api_ai_trace_detail(trace_id):
    """Return one full trace and its ordered event timeline."""
    detail = trace_store.get_trace(trace_id)
    if detail is None:
        return jsonify({'error': 'AI trace not found.'}), 404
    detail['final_decision'] = detail.get('decision')
    detail['retry_count'] = _ai_trace_retry_count(detail.get('retry_info'))
    return jsonify(detail)


def _ai_trace_retry_count(retry_info):
    if not isinstance(retry_info, dict):
        return 0
    try:
        attempts = int(retry_info.get('attempts', retry_info.get('attempt', 1)))
    except (TypeError, ValueError):
        return 0
    return max(0, attempts - 1)


def _validate_prompt_template(name, prompt):
    if not isinstance(prompt, str) or not prompt.strip():
        return 'Prompt text cannot be empty.'
    if len(prompt) > 30000:
        return 'Prompt text must be 30,000 characters or fewer.'
    try:
        fields = [field for _, field, _, _ in string.Formatter().parse(prompt) if field is not None]
        if name == 'extract_skills_prompt':
            if not any(field == '' for field in fields):
                return 'Keep the {} placeholder for the job description.'
            prompt.format('JOB DESCRIPTION')
        elif name == 'ai_answer_prompt':
            if sum(field == '' for field in fields) < 2:
                return 'Keep both {} placeholders for applicant information and the question.'
            prompt.format('APPLICANT INFORMATION', 'APPLICATION QUESTION')
        elif name == 'job_suitability_prompt':
            required = {'candidate_context', 'title', 'company', 'location', 'job_description'}
            missing = required.difference(fields)
            if missing:
                return 'Missing required placeholders: ' + ', '.join('{' + key + '}' for key in sorted(missing))
            prompt.format(candidate_context='CANDIDATE', title='TITLE', company='COMPANY',
                          location='LOCATION', job_description='DESCRIPTION')
    except (ValueError, KeyError, IndexError) as error:
        return f'Invalid placeholder formatting: {error}'
    return None


@app.route('/api/system-prompts', methods=['GET'])
def api_get_system_prompts():
    path = ai_prompts.PROMPT_OVERRIDE_PATH
    try:
        saved = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    except (OSError, ValueError):
        saved = {}
    current = {name: saved.get(name, default) for name, default in ai_prompts.PROMPT_DEFAULTS.items()}
    return jsonify({'prompts': current, 'defaults': ai_prompts.PROMPT_DEFAULTS,
                    'has_overrides': bool(saved)})


@app.route('/api/system-prompts', methods=['POST'])
def api_save_system_prompts():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'error': 'Expected a prompts object.'}), 400
    if payload.get('reset') is True:
        try:
            ai_prompts.PROMPT_OVERRIDE_PATH.unlink(missing_ok=True)
        except OSError as error:
            return jsonify({'error': f'Could not restore built-in prompts: {error}'}), 500
        return jsonify({'saved': True, 'reset': True})

    prompts = payload.get('prompts')
    if not isinstance(prompts, dict) or set(prompts) != set(ai_prompts.PROMPT_DEFAULTS):
        return jsonify({'error': 'Provide all three prompts: extract_skills_prompt, ai_answer_prompt, and job_suitability_prompt.'}), 400
    for name, prompt in prompts.items():
        error = _validate_prompt_template(name, prompt)
        if error:
            return jsonify({'error': f'{name}: {error}'}), 400

    path = ai_prompts.PROMPT_OVERRIDE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix='.ai-prompts-', dir=path.parent)
    try:
        if os.name != 'nt': os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(prompts, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
        os.replace(temp_path, path)
    except OSError as error:
        try: os.unlink(temp_path)
        except OSError: pass
        return jsonify({'error': f'Could not save prompt overrides: {error}'}), 500
    return jsonify({'saved': True, 'reset': False})


@app.route('/api/logs', methods=['GET'])
def api_logs():
    '''
    Returns the run log starting from byte offset ?offset=N, plus the byte
    offset to read from next time. The UI polls this while the bot runs.
    '''
    try:
        offset = int(request.args.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    if offset < 0:
        offset = 0
    control = automation_control.status(CONTROL_PATH)
    run_id = control.get('run_id')
    log_path = LOG_PATH
    if run_diagnostics.valid_run_id(run_id):
        log_path = str(run_diagnostics.run_dir(run_id, DIAGNOSTICS_DIR) / 'worker.log')
    if not os.path.exists(log_path):
        return jsonify({"content": "", "next_offset": 0})
    try:
        with open(log_path, "rb") as log_file:
            log_file.seek(0, os.SEEK_END)
            size = log_file.tell()
            if offset > size:
                # Log was truncated (a new run started); start over.
                offset = 0
            log_file.seek(offset)
            data = log_file.read()
        content = data.decode("utf-8", errors="replace")
        return jsonify({"content": content, "next_offset": offset + len(data)})
    except OSError as err:
        return jsonify({"content": "", "next_offset": offset, "error": str(err)})


# ===========================================================================
# Update check (see modules/updater.py)
# ===========================================================================
def _freeze_config() -> None:
    '''
    Copy the settings the tool is using RIGHT NOW into user_config.json, before
    an update rewrites the config/*.py files.

    For control-panel users this is a no-op: their values are already in the
    JSON. For someone who configured the old way by hand-editing config/*.py it
    is what stops the update reverting them to the shipped defaults, because
    config/_overrides.py re-applies the JSON over whatever `git pull` writes.

    ponytail: this pins every schema key to today's value, so a later change to
    a shipped default stops reaching that user. Acceptable - a hand-edited file
    was already pinned. Freeze only the keys that differ from HEAD if it bites.
    '''
    current = _overrides.load_user_config()
    for section, values in _effective_config().items():
        if not isinstance(current.get(section), dict):
            current[section] = {}
        for key, value in values.items():
            current[section].setdefault(key, value)
    with open(USER_CONFIG_PATH, "w", encoding="utf-8") as file:
        json.dump(current, file, indent=2, ensure_ascii=False)


@app.route('/api/update-check', methods=['GET'])
def api_update_check():
    '''
    The local version vs the published one. Reports "no update" rather than an
    error when the check fails, so being offline is invisible to the user. The
    UI calls this after the page is interactive, so it never delays startup.
    '''
    latest = updater.latest_version()
    current = updater.current_version()
    return jsonify({"current": current, "latest": latest,
                    "update_available": updater.is_newer(latest, current)})


@app.route('/api/update', methods=['POST'])
def api_update():
    '''Saves the live settings, then fast-forwards this clone to the newest commit.'''
    try:
        _freeze_config()
    except OSError as err:
        return jsonify({"ok": False,
                        "message": "Could not save your settings first: %s" % err}), 500
    return jsonify(updater.self_update())


def _resolve_port(preferred: int = 5000) -> int:
    '''
    Pick a port to serve on. Honors the PORT environment variable (the launcher
    scripts set it). Otherwise tries `preferred`, and if that's taken - e.g. port
    5000 is used by AirPlay Receiver on macOS - asks the OS for any free port so
    the panel always starts instead of crashing with "address already in use".
    '''
    import socket
    requested = os.environ.get("PORT", "").strip()
    if requested.isdigit():
        return int(requested)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


if __name__ == '__main__':
    # SECURITY: localhost only, debug OFF. This app handles credentials.
    atexit.register(_stop_bot_on_exit)
    # The bot intentionally runs in its own process session, so forward
    # terminal and OS shutdown signals to prevent orphaned application runs.
    for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
        signal.signal(shutdown_signal, _handle_shutdown_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _handle_shutdown_signal)

    port = _resolve_port(5000)
    url = "http://127.0.0.1:%d" % port
    print(
        "\n  Control panel ready at:  %s\n"
        "  Automation keeps running if this dashboard closes. Use Stop to end it.\n" % url,
        flush=True,
    )
    # The launcher scripts set PANEL_OPEN_BROWSER=1 so the browser opens itself,
    # to the right port, cross-platform. Running `python app.py` by hand won't.
    if os.environ.get("PANEL_OPEN_BROWSER", "").strip() not in ("", "0", "false", "False"):
        import threading
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False)
