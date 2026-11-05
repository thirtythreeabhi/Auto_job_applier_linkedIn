import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

import pytest

from modules.ai import prompt_log, trace_store


def _write_trace(index):
    trace_id = trace_store.create_trace(run_id='run-1', job_id=f'job-{index}',
        category='classifier', provider='local', model='stub', prompt=f'prompt {index}')
    trace_store.append_event(trace_id, 'workflow', 'opened_job', {'index': index})
    trace_store.complete_trace(trace_id, raw_response='OK', parsed_output={'decision': 'REVIEW'},
        latency_ms=12, decision='REVIEW')
    return trace_id


@pytest.fixture(autouse=True)
def trace_db(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_TRACE_DB_PATH', str(tmp_path / 'traces.sqlite3'))
    monkeypatch.setattr(prompt_log, 'PROMPT_LOG_PATH', tmp_path / '.ai_prompt_log.jsonl')


def test_concurrent_process_writes_are_persisted_in_ordered_traces():
    context = multiprocessing.get_context('spawn')
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as pool:
        trace_ids = list(pool.map(_write_trace, range(12)))
    assert len(set(trace_ids)) == 12
    page = trace_store.list_traces(limit=50)
    assert page['total'] == 12
    assert all(trace_store.get_trace(trace_id)['events'][0]['event_type'] == 'workflow'
               for trace_id in trace_ids)
    with trace_store._connection() as conn:
        assert conn.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal'


def test_llm_tool_calls_are_distinct_from_workflow_events():
    trace_id = trace_store.create_trace(category='answer', prompt='question')
    trace_store.append_event(trace_id, 'llm_tool_call', 'search_candidate', {'query': 'Python'})
    trace_store.append_event(trace_id, 'workflow', 'fill_form_field', {'field': 'skills'})
    events = trace_store.get_trace(trace_id)['events']
    assert [event['event_type'] for event in events] == ['llm_tool_call', 'workflow']
    with pytest.raises(ValueError):
        trace_store.append_event(trace_id, 'tool', 'click_apply')


def test_redacts_credentials_in_text_and_structured_payloads():
    trace_id = trace_store.create_trace(prompt='Authorization: Bearer abcdefghijklmnop api_key=secretvalue')
    trace_store.append_event(trace_id, 'workflow', 'request', {'session_cookie': 'private', 'safe': 'yes'})
    detail = trace_store.get_trace(trace_id)
    assert 'abcdefghijklmnop' not in detail['prompt']
    assert 'secretvalue' not in detail['prompt']
    assert detail['events'][0]['payload'] == {'session_cookie': '[REDACTED]', 'safe': 'yes'}


def test_completion_failure_retry_correlation_filters_and_pagination():
    ids = [trace_store.create_trace(job_id=f'job-{n}', run_id='run-a', category='classifier',
                                    model='model-a', prompt=f'role {n}') for n in range(5)]
    trace_store.complete_trace(ids[0], error='timeout', retry_info={'attempts': 2})
    trace_store.append_event(ids[0], 'retry', 'model_retry', {'attempt': 2})
    trace_store.correlate_trace(ids[0], decision='REVIEW', application_outcome='not_submitted')
    assert trace_store.correlate_job('job-1', run_id='run-a', decision='APPLY') == 1
    page = trace_store.list_traces(filters={'category': 'classifier', 'model': 'model-a', 'run_id': 'run-a'},
                                   limit=2, offset=1)
    assert page['total'] == 5 and len(page['items']) == 2 and page['offset'] == 1
    filtered = trace_store.list_traces(status='error', decision='REVIEW')
    assert [item['trace_id'] for item in filtered['items']] == [ids[0]]
    detail = trace_store.get_trace(ids[0])
    assert detail['status'] == 'error'
    assert detail['retry_info'] == {'attempts': 2}
    assert detail['application_outcome'] == 'not_submitted'
    assert detail['events'][0]['event_type'] == 'retry'
    with pytest.raises(KeyError):
        trace_store.complete_trace('missing-id', raw_response='x')


def test_job_correlation_can_attach_run_to_traces_without_run_id():
    trace_id = trace_store.create_trace(job_id='job-unassigned-run')
    assert trace_store.correlate_job('job-unassigned-run', run_id='run-new',
                                     application_outcome='reviewed') == 1
    detail = trace_store.get_trace(trace_id)
    assert detail['run_id'] == 'run-new'
    assert detail['application_outcome'] == 'reviewed'


def test_timestamp_filters_and_query_search():
    trace_store.create_trace(timestamp='2026-01-01T00:00:00+00:00', job_id='old-job', prompt='old role')
    trace_store.create_trace(timestamp='2026-02-01T00:00:00+00:00', job_id='new-job', prompt='new role')
    page = trace_store.list_traces(since='2026-01-15T00:00:00+00:00', until='2026-02-15T00:00:00+00:00',
                                   query='new role')
    assert page['total'] == 1
    assert page['items'][0]['job_id'] == 'new-job'


def test_legacy_import_skips_malformed_lines_and_is_idempotent(tmp_path):
    path = tmp_path / 'legacy.jsonl'
    path.write_text('{"category":"answer","prompt":"hello","response":"hi"}\nnot-json\n[]\n',
                    encoding='utf-8')
    assert trace_store.import_legacy_prompt_log(path) == {'imported': 1, 'malformed': 2}
    assert trace_store.import_legacy_prompt_log(path) == {'imported': 0, 'malformed': 2}
    page = trace_store.list_traces()
    assert page['total'] == 1 and page['items'][0]['category'] == 'answer'


def test_record_prompt_keeps_legacy_jsonl_shape_and_writes_sqlite_trace():
    trace_id = prompt_log.record_prompt('answer', 'My password=supersecret?', 'yes', 'stub', 'local')
    record = json.loads(prompt_log.PROMPT_LOG_PATH.read_text(encoding='utf-8'))
    assert {'timestamp', 'category', 'provider', 'model', 'prompt', 'response', 'error'} <= record.keys()
    assert 'supersecret' not in record['prompt']
    detail = trace_store.get_trace(trace_id)
    assert detail['raw_response'] == 'yes'
    assert detail['events'][0]['event_type'] == 'llm_request'


def test_prompt_jsonl_write_survives_sqlite_storage_failure(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_TRACE_DB_PATH', str(tmp_path))  # A directory cannot be opened as a database.
    trace_id = prompt_log.record_prompt('answer', 'prompt', 'response')
    assert trace_id
    record = json.loads(prompt_log.PROMPT_LOG_PATH.read_text(encoding='utf-8'))
    assert record['response'] == 'response'
