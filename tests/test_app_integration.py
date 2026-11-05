'''
Integration tests for app.py (the local control panel) via Flask's test client.
These exercise real request/response behaviour: schema exposure, config save
coercion + round-trip, unknown-key rejection, and the applied-jobs history
CSV -> JSON mapping and mark-applied flow.

All tests isolate their writes to a tmp_path, so the user's real user_config.json
and "all excels/" folder are never touched.

License: MIT  (https://opensource.org/license/mit)
'''

import csv
import json
import os


# --------------------------------- schema -----------------------------------
def test_schema_endpoint_returns_nonempty_list(client):
    resp = client.get("/api/schema")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list) and len(data) > 0
    assert "section" in data[0] and "fields" in data[0]


# ------------------------------- config save/get ----------------------------
def test_config_save_coerces_and_roundtrips(client, tmp_path, monkeypatch):
    import app
    import config._overrides as overrides
    cfg_path = str(tmp_path / "user_config.json")
    monkeypatch.setattr(app, "USER_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(overrides, "USER_CONFIG_PATH", cfg_path)

    # "true" (string) must be coerced to a real bool for the use_AI field.
    resp = client.post("/api/config", json={"secrets": {"use_AI": "true"}})
    assert resp.status_code == 200

    with open(cfg_path, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["secrets"]["use_AI"] is True

    # GET reflects the saved value.
    got = client.get("/api/config").get_json()
    assert got["secrets"]["use_AI"] is True


def test_config_save_rejects_unknown_key(client, tmp_path, monkeypatch):
    import app
    import config._overrides as overrides
    cfg_path = str(tmp_path / "user_config.json")
    monkeypatch.setattr(app, "USER_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(overrides, "USER_CONFIG_PATH", cfg_path)

    resp = client.post("/api/config", json={"secrets": {"definitely_not_a_field": 1}})
    assert resp.status_code == 400
    assert not os.path.exists(cfg_path)  # nothing written on rejection


def test_config_save_rejects_unknown_section(client, tmp_path, monkeypatch):
    import app
    import config._overrides as overrides
    cfg_path = str(tmp_path / "user_config.json")
    monkeypatch.setattr(app, "USER_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(overrides, "USER_CONFIG_PATH", cfg_path)

    resp = client.post("/api/config", json={"not_a_section": {"x": 1}})
    assert resp.status_code == 400


# ------------------------------ applied-jobs CSV ----------------------------
_CSV_COLUMNS = ['Job ID', 'Title', 'Company', 'HR Name', 'HR Link',
                'Job Link', 'External Job link', 'Date Applied']


def _write_history_csv(folder):
    path = os.path.join(folder, "all_applied_applications_history.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        writer.writerow({
            'Job ID': 'J1', 'Title': 'Engineer', 'Company': 'Acme',
            'HR Name': 'Unknown', 'HR Link': '', 'Job Link': 'http://x',
            'External Job link': 'http://ext', 'Date Applied': 'Pending',
        })
    return path


def test_applied_jobs_get_maps_columns_to_json_keys(client, tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "PATH", str(tmp_path) + os.sep)
    _write_history_csv(str(tmp_path))

    resp = client.get("/applied-jobs")
    assert resp.status_code == 200
    row = resp.get_json()[0]
    assert row["Job_ID"] == "J1"
    assert row["Title"] == "Engineer"
    assert row["External_Job_link"] == "http://ext"
    assert row["Date_Applied"] == "Pending"


def test_applied_jobs_mark_applied_updates_date(client, tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "PATH", str(tmp_path) + os.sep)
    _write_history_csv(str(tmp_path))

    resp = client.put("/applied-jobs/J1")
    assert resp.status_code == 200

    row = client.get("/applied-jobs").get_json()[0]
    assert row["Date_Applied"] != "Pending"


def test_applied_jobs_missing_file_returns_404(client, tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "PATH", str(tmp_path) + os.sep)  # empty dir, no CSV
    assert client.get("/applied-jobs").status_code == 404


def test_applied_jobs_mark_unknown_id_returns_404(client, tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "PATH", str(tmp_path) + os.sep)
    _write_history_csv(str(tmp_path))
    assert client.put("/applied-jobs/does-not-exist").status_code == 404


# ------------------------------- bot status ---------------------------------
def test_status_reports_not_running(client):
    resp = client.get("/api/status")
    assert resp.status_code == 200
    assert resp.get_json()["running"] is False


class _FakeBotProcess:
    def __init__(self, pid=43210):
        self.pid = pid
        self.alive = True
        self.returncode = None
    def poll(self): return None if self.alive else self.returncode
    def wait(self, timeout=None):
        self.alive = False
        if self.returncode is None: self.returncode = -15
        return self.returncode
    def terminate(self): self.alive = False; self.returncode = -15
    def kill(self): self.alive = False; self.returncode = -9


def _mock_bot_launch(monkeypatch, tmp_path):
    import app
    monkeypatch.setattr(app, '_bot_proc', None)
    control_path = str(tmp_path / '.bot_control.json')
    monkeypatch.setattr(app, 'CONTROL_PATH', control_path)
    monkeypatch.setattr(app, 'DIAGNOSTICS_DIR', str(tmp_path / 'runs'))
    monkeypatch.setattr(app, 'PID_PATH', str(tmp_path / '.bot_run.pid'))
    monkeypatch.setattr(app, 'LOG_PATH', str(tmp_path / '.bot_run.log'))
    monkeypatch.setattr(app, '_bot_command', lambda: ['python', 'runAiBot.py'])
    monkeypatch.setattr(app, '_bot_environment', lambda: os.environ.copy())
    process = _FakeBotProcess()
    monkeypatch.setattr(app.subprocess, 'Popen', lambda *args, **kwargs: process)
    return app, process, control_path


def test_pause_resume_ack_and_repeated_pause(client, tmp_path, monkeypatch):
    import time
    from modules import automation_control
    app, _process, control_path = _mock_bot_launch(monkeypatch, tmp_path)
    started = client.post('/api/run').get_json()
    run_id = started['run_id']
    assert started['state'] == 'running'

    assert client.post('/api/pause').get_json()['state'] == 'pausing'
    assert client.post('/api/pause').get_json()['state'] == 'pausing'

    controller = automation_control.PauseController(run_id, control_path, poll_interval=0.005)
    import threading
    resumed = threading.Event()
    worker = threading.Thread(target=lambda: (controller.checkpoint(validate=lambda: True), resumed.set()))
    worker.start()
    deadline = time.time() + 2
    while time.time() < deadline and client.get('/api/status').get_json()['state'] != 'paused':
        time.sleep(0.005)
    assert client.get('/api/status').get_json()['state'] == 'paused'
    assert client.post('/api/pause').get_json()['state'] == 'paused'
    assert client.post('/api/resume').get_json()['state'] == 'resuming'
    worker.join(timeout=2)
    assert resumed.is_set()
    assert client.get('/api/status').get_json()['state'] == 'running'


def test_pause_stop_and_crash_state(client, tmp_path, monkeypatch):
    from modules import automation_control
    app, process, control_path = _mock_bot_launch(monkeypatch, tmp_path)
    started = client.post('/api/run').get_json()
    automation_control.request('pause', started['run_id'], control_path)
    automation_control.report('paused', started['run_id'], control_path)
    assert client.get('/api/status').get_json()['state'] == 'paused'
    stopped = client.post('/api/stop').get_json()
    assert stopped['running'] is False
    assert client.get('/api/status').get_json()['state'] == 'stopped'

    monkeypatch.setattr(app, '_bot_proc', None)
    process.alive = True
    process.returncode = None
    restarted = client.post('/api/run').get_json()
    from modules import run_diagnostics
    run_diagnostics.begin_stage(restarted['run_id'], 'search', app.DIAGNOSTICS_DIR)
    process.alive = False
    process.returncode = 1
    status = client.get('/api/status').get_json()
    assert status['state'] == 'failed'
    assert status['run']['reason_code'] == 'PROCESS_CRASHED'


def test_new_run_resets_stale_pause_request(client, tmp_path, monkeypatch):
    import json
    app, _process, control_path = _mock_bot_launch(monkeypatch, tmp_path)
    with open(control_path, 'w', encoding='utf-8') as stream:
        json.dump({'run_id': 'old-run', 'command': 'pause', 'state': 'paused'}, stream)
    started = client.post('/api/run').get_json()
    with open(control_path, encoding='utf-8') as stream:
        state = json.load(stream)
    assert state == {'run_id': started['run_id'], 'command': 'resume', 'state': 'running'}
    assert started['run_id'] != 'old-run'
    from modules import automation_control
    assert automation_control.request('pause', 'old-run', control_path) is None
    assert automation_control.status(control_path)['command'] == 'resume'


def test_status_preserves_worker_failure_and_reports_unexpected_exit(client, tmp_path, monkeypatch):
    import app
    from modules import run_diagnostics
    app, process, _control_path = _mock_bot_launch(monkeypatch, tmp_path)
    started = client.post('/api/run').get_json()
    run_id = started['run_id']
    run_diagnostics.begin_stage(run_id, 'search', app.DIAGNOSTICS_DIR)
    run_diagnostics.fail(run_id, 'SEARCH_FAILED', 'Search failed.',
                         RuntimeError('selector failure'), 'search',
                         root=app.DIAGNOSTICS_DIR)
    process.alive = False
    process.returncode = 1

    status = client.get('/api/status').get_json()
    assert status['state'] == 'failed'
    assert status['run']['reason_code'] == 'SEARCH_FAILED'
    assert status['run']['process_exit_code'] == 1
    # Polling again must not replace the worker's specific failure with a generic crash.
    again = client.get('/api/status').get_json()
    assert again['run']['reason_code'] == 'SEARCH_FAILED'


def test_manual_stop_has_a_distinct_terminal_reason(client, tmp_path, monkeypatch):
    import app
    _app, _process, _control_path = _mock_bot_launch(monkeypatch, tmp_path)
    started = client.post('/api/run').get_json()
    client.post('/api/pause')
    stopped = client.post('/api/stop').get_json()
    assert stopped['state'] == 'stopped'
    result = client.get('/api/status').get_json()['run']
    assert result['status'] == 'stopped'
    assert result['reason_code'] == 'USER_STOPPED'


def test_run_detail_and_log_endpoints_validate_run_id(client, tmp_path, monkeypatch):
    app, _process, _control_path = _mock_bot_launch(monkeypatch, tmp_path)
    from modules import run_diagnostics
    started = client.post('/api/run').get_json()
    run_id = started['run_id']
    log_path = run_diagnostics.run_dir(run_id, app.DIAGNOSTICS_DIR) / 'worker.log'
    log_path.write_text('diagnostic output\n', encoding='utf-8')
    assert client.get('/api/runs/' + run_id).status_code == 200
    assert client.get('/api/runs/' + run_id + '/log').get_json()['content'] == 'diagnostic output\n'
    assert client.get('/api/runs/not-a-run-id').status_code == 400


def test_dashboard_shutdown_does_not_stop_worker(client, tmp_path, monkeypatch):
    import app
    app, process, _control_path = _mock_bot_launch(monkeypatch, tmp_path)
    started = client.post('/api/run').get_json()
    app._stop_bot_on_exit()
    assert process.alive
    assert app._is_running()
    assert app.automation_control.status(app.CONTROL_PATH)['run_id'] == started['run_id']


def test_restarted_dashboard_reconnects_to_validated_worker(client, tmp_path, monkeypatch):
    import app
    app, process, _control_path = _mock_bot_launch(monkeypatch, tmp_path)
    started = client.post('/api/run').get_json()
    with open(app.PID_PATH, 'w', encoding='utf-8') as stream:
        import json
        json.dump({'pid': process.pid, 'run_id': started['run_id'], 'start_ticks': '42',
                   'script': os.path.realpath(os.path.join(app.ROOT, 'runAiBot.py'))}, stream)
    monkeypatch.setattr(app, '_process_identity', lambda _pid: {
        'start_ticks': '42', 'script': os.path.realpath(os.path.join(app.ROOT, 'runAiBot.py')),
        'argv': ['python', 'runAiBot.py']})
    monkeypatch.setattr(app, '_bot_proc', None)
    response = client.get('/api/status').get_json()
    assert response['running'] is True
    assert response['pid'] == process.pid
    assert response['run_id'] == started['run_id']


def test_reused_or_stale_pid_is_rejected(client, tmp_path, monkeypatch):
    import app
    app, process, _control_path = _mock_bot_launch(monkeypatch, tmp_path)
    started = client.post('/api/run').get_json()
    with open(app.PID_PATH, 'w', encoding='utf-8') as stream:
        import json
        json.dump({'pid': process.pid, 'run_id': started['run_id'], 'start_ticks': 'old',
                   'script': os.path.realpath(os.path.join(app.ROOT, 'runAiBot.py'))}, stream)
    monkeypatch.setattr(app, '_process_identity', lambda _pid: {
        'start_ticks': 'new', 'script': os.path.realpath(os.path.join(app.ROOT, 'runAiBot.py')),
        'argv': ['python', 'runAiBot.py']})
    monkeypatch.setattr(app, '_bot_proc', None)
    assert client.get('/api/status').get_json()['running'] is False
    assert not os.path.exists(app.PID_PATH)


def test_duplicate_run_request_reconnects_instead_of_launching_again(client, tmp_path, monkeypatch):
    import app
    app, _process, _control_path = _mock_bot_launch(monkeypatch, tmp_path)
    launches = []
    monkeypatch.setattr(app.subprocess, 'Popen', lambda *args, **kwargs: (launches.append(1) or _process))
    first = client.post('/api/run').get_json()
    second = client.post('/api/run').get_json()
    assert second['running'] is True
    assert second['run_id'] == first['run_id']
    assert len(launches) == 1


def test_explicit_stop_can_terminate_reconnected_worker(client, tmp_path, monkeypatch):
    import app
    app, process, _control_path = _mock_bot_launch(monkeypatch, tmp_path)
    started = client.post('/api/run').get_json()
    with open(app.PID_PATH, 'w', encoding='utf-8') as stream:
        import json
        json.dump({'pid': process.pid, 'run_id': started['run_id'], 'start_ticks': '42',
                   'script': os.path.realpath(os.path.join(app.ROOT, 'runAiBot.py'))}, stream)
    monkeypatch.setattr(app, '_process_identity', lambda _pid: {
        'start_ticks': '42', 'script': os.path.realpath(os.path.join(app.ROOT, 'runAiBot.py')),
        'argv': ['python', 'runAiBot.py']})
    monkeypatch.setattr(app, '_bot_proc', None)
    monkeypatch.setattr(app.os, 'killpg', lambda *_args: None)
    monkeypatch.setattr(app.os, 'kill', lambda *_args: (_ for _ in ()).throw(ProcessLookupError()))
    stopped = client.post('/api/stop').get_json()
    assert stopped['running'] is False
    assert stopped['run_id'] == started['run_id']


# ------------------------------- pages render -------------------------------
def test_control_panel_and_history_pages_render(client):
    assert client.get("/").status_code == 200
    assert client.get("/history").status_code == 200
