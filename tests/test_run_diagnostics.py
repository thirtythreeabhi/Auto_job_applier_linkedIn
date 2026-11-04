import json

from modules import run_diagnostics as diagnostics


def _run_id(number):
    return f'{number:032x}'


def test_successful_run_has_terminal_outcome_and_timestamps(tmp_path):
    run_id = _run_id(1)
    diagnostics.create_run(run_id, tmp_path)
    diagnostics.complete_stage(run_id, 'search', tmp_path)
    result = diagnostics.finish(run_id, 'completed', 'COMPLETED', 'All searches completed.', 0, tmp_path)
    assert result['status'] == 'completed'
    assert result['reason_code'] == 'COMPLETED'
    assert result['started_at'] and result['ended_at']
    assert result['last_successful_stage'] == 'search'
    assert result['process_exit_code'] == 0


def test_fatal_python_exception_persists_traceback_and_failed_stage(tmp_path):
    run_id = _run_id(2)
    diagnostics.create_run(run_id, tmp_path)
    diagnostics.begin_stage(run_id, 'search', tmp_path)
    try:
        raise RuntimeError('search exploded')
    except RuntimeError as error:
        result = diagnostics.fail(run_id, 'SEARCH_FAILED', 'Search failed.', error,
                                  failed_stage='search', root=tmp_path)
    assert result['status'] == 'failed'
    assert result['reason_code'] == 'SEARCH_FAILED'
    assert result['exception_type'] == 'RuntimeError'
    assert 'search exploded' in result['traceback']
    assert 'search exploded' in (tmp_path / run_id / 'traceback.txt').read_text()


def test_worker_exit_is_reconciled_and_terminal_failure_is_preserved(tmp_path):
    run_id = _run_id(3)
    diagnostics.create_run(run_id, tmp_path)
    diagnostics.begin_stage(run_id, 'search', tmp_path)
    (tmp_path / run_id / 'worker.log').write_text('Traceback: worker crashed\n', encoding='utf-8')
    result = diagnostics.reconcile_exit(run_id, 17, tmp_path)
    assert result['status'] == 'failed'
    assert result['reason_code'] == 'PROCESS_CRASHED'
    assert result['process_exit_code'] == 17
    assert 'worker crashed' in result['traceback']
    diagnostics.finish(run_id, 'completed', 'COMPLETED', 'late success', 0, tmp_path)
    assert diagnostics.get_run(run_id, tmp_path)['reason_code'] == 'PROCESS_CRASHED'


def test_screenshot_capture_failure_does_not_hide_original_error(tmp_path):
    run_id = _run_id(4)
    diagnostics.create_run(run_id, tmp_path)
    class BrokenDriver:
        session_id = 'active-session'
        def save_screenshot(self, path): raise OSError('screenshot unavailable')
    try:
        raise LookupError('original browser failure')
    except LookupError as error:
        result = diagnostics.fail(run_id, 'BROWSER_SESSION_LOST', 'Browser lost.', error,
                                  driver=BrokenDriver(), root=tmp_path)
    assert result['exception_type'] == 'LookupError'
    assert 'original browser failure' in result['traceback']
    events = [json.loads(line) for line in (tmp_path / run_id / 'events.jsonl').read_text().splitlines()]
    assert any(item['event'] == 'screenshot_failed' for item in events)


def test_pruning_removes_only_terminal_runs_and_log_files_are_per_run(tmp_path):
    older = _run_id(5)
    newer = _run_id(6)
    active = _run_id(7)
    for run_id in (older, newer, active):
        diagnostics.create_run(run_id, tmp_path)
        (tmp_path / run_id / 'worker.log').write_text(run_id, encoding='utf-8')
    diagnostics.finish(older, 'completed', 'COMPLETED', 'done', 0, tmp_path)
    diagnostics.finish(newer, 'stopped', 'USER_STOPPED', 'stopped', 0, tmp_path)
    diagnostics.prune_runs(tmp_path, keep=1)
    assert not (tmp_path / older).exists()
    assert (tmp_path / newer / 'worker.log').read_text() == newer
    assert (tmp_path / active / 'worker.log').read_text() == active
