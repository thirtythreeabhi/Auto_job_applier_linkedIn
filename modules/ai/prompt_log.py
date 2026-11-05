"""Append local, inspectable records of prompts sent to an AI model."""

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import uuid

from modules.ai import trace_store

PROMPT_LOG_PATH = Path(__file__).resolve().parents[2] / '.ai_prompt_log.jsonl'
_LOCK = threading.Lock()


def record_prompt(category, prompt, response='', model='', provider='', error='', *,
                  trace_id=None, run_id=None, job_id=None, candidate_context='',
                  parsed_output=None, latency_ms=None, retry_info=None,
                  decision=None, application_outcome=None):
    """Store a model call in SQLite and the legacy JSONL log.

    Extra keyword-only fields are optional so existing call sites remain
    compatible. Logging failures never interrupt automation.
    """
    safe = trace_store.redact
    trace_id = str(trace_id or uuid.uuid4())
    record = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'category': str(safe(category)),
        'provider': str(safe(provider)),
        'model': str(safe(model)),
        'prompt': str(safe(prompt)),
        'response': str(safe(response)),
        'error': str(safe(error)),
    }
    record['trace_id'] = str(safe(trace_id))
    if run_id is not None: record['run_id'] = str(safe(run_id))
    if job_id is not None: record['job_id'] = str(safe(job_id))
    try:
        with _LOCK, PROMPT_LOG_PATH.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + '\n')
    except OSError:
        pass
    try:
        tid = trace_id
        if not tid or trace_store.get_trace(tid) is None:
            tid = trace_store.create_trace(trace_id=trace_id, run_id=run_id, job_id=job_id,
                timestamp=record['timestamp'], category=record['category'], provider=record['provider'],
                model=record['model'], prompt=record['prompt'], candidate_context=candidate_context,
                retry_info=retry_info)
        trace_store.append_event(tid, 'llm_request', record['category'], {
            'provider': record['provider'], 'model': record['model'],
            'error': record['error'] or None, 'latency_ms': latency_ms,
            'retry_info': retry_info,
        })
        trace_store.complete_trace(tid, raw_response=record['response'], parsed_output=parsed_output,
            latency_ms=latency_ms, error=record['error'], retry_info=retry_info,
            decision=decision, application_outcome=application_outcome)
        return tid
    except Exception:
        # Trace persistence is diagnostic and must not change model/application behavior.
        return trace_id
