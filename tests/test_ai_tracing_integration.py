"""End-to-end trace correlation through the real AI orchestration layer."""

import json

from modules.ai import connections, prompt_log, trace_store


class _Structured:
    def __init__(self, payload):
        self.payload = payload

    def invoke(self, prompt):
        class Result:
            def __init__(self, payload): self.payload = payload
            def model_dump(self): return self.payload
        return Result(self.payload)


class _Model:
    def with_structured_output(self, schema):
        return _Structured({
            'decision': 'APPLY', 'core_skills_match': True, 'match_score': 91,
            'confidence': 0.88, 'matched_skills': ['RAG'], 'missing_skills': [],
            'experience_gap_years': None, 'hard_blockers': [], 'reason': 'Relevant experience.',
        })

    def invoke(self, prompt):
        raise AssertionError('structured output should be used')


def test_classifier_attempt_is_correlated_with_job_run_context(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_TRACE_DB_PATH', str(tmp_path / 'trace.sqlite3'))
    monkeypatch.setattr(prompt_log, 'PROMPT_LOG_PATH', tmp_path / 'prompts.jsonl')
    client = connections.AIClient(_Model(), provider='ollama', model_name='qwen-test')
    evidence = ('## Technical Skills\nLLM integration, RAG, agent/tool calling.\n'
                '## Major Professional Projects\nTalentStream recruitment SaaS with verified RAG work.')

    decision = connections.classify_job_suitability(
        client, 'AI Backend Engineer', 'Acme', 'Remote',
        'Build RAG systems and LLM integrations.', evidence,
        job_id='linkedin-123', run_id='run-test')

    page = trace_store.list_traces(filters={'job_id': 'linkedin-123', 'run_id': 'run-test'})
    assert decision['decision'] == 'APPLY'
    assert page['total'] == 1
    detail = trace_store.get_trace(page['items'][0]['trace_id'])
    assert detail['job_title'] == 'AI Backend Engineer'
    assert detail['company'] == 'Acme'
    assert detail['provider'] == 'ollama' and detail['model'] == 'qwen-test'
    assert 'RAG' in detail['candidate_context'] and 'TalentStream' in detail['candidate_context']
    assert 'Build RAG systems' in detail['prompt']
    assert json.loads(detail['raw_response'])['decision'] == 'APPLY'
    assert detail['parsed_output']['decision'] == 'APPLY'
    assert detail['decision'] == 'APPLY'
    assert detail['latency_ms'] >= 0
    assert [event['event_type'] for event in detail['events']] == ['llm_request', 'decision']


def test_final_application_answer_is_kept_separate_from_raw_model_output(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_TRACE_DB_PATH', str(tmp_path / 'trace.sqlite3'))
    monkeypatch.setattr(prompt_log, 'PROMPT_LOG_PATH', tmp_path / 'prompts.jsonl')

    class AnswerModel:
        def invoke(self, prompt):
            class Message:
                content = json.dumps({'field_type': 'single_select', 'answer': 'Beta',
                    'evidence': ['The candidate selected Beta'], 'confidence': .95,
                    'needs_human_review': False})
            return Message()

    client = connections.AIClient(AnswerModel(), provider='ollama', model_name='qwen-test')
    from modules.linkedin_modern import _generate_application_answer
    answer = _generate_application_answer(client, 'Pick one', 'single_select', ['Alpha', 'Beta'],
        'The candidate selected Beta', '', '', trace_context={'job_id': 'job-answer', 'job_title': 'Engineer',
                                   'company': 'Acme', 'run_id': 'run-answer'})

    assert answer == 'Beta'  # exact option accepted after structured validation
    item = trace_store.list_traces(filters={'job_id': 'job-answer'})['items'][0]
    detail = trace_store.get_trace(item['trace_id'])
    assert json.loads(detail['raw_response'])['answer'] == 'Beta'
    assert detail['parsed_output'] == {'final_application_answer': 'Beta'}
    assert detail['run_id'] == 'run-answer'
    assert any(event['name'] == 'final_application_answer' and event['payload']['answer'] == 'Beta'
               for event in detail['events'])


def test_structured_output_fallback_is_recorded_as_two_linked_attempts(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_TRACE_DB_PATH', str(tmp_path / 'trace.sqlite3'))
    monkeypatch.setattr(prompt_log, 'PROMPT_LOG_PATH', tmp_path / 'prompts.jsonl')

    class FallbackModel:
        def with_structured_output(self, schema, **kwargs):
            raise NotImplementedError('structured output unavailable')

        def invoke(self, prompt):
            class Message:
                content = json.dumps({
                    'decision': 'REVIEW', 'core_skills_match': False, 'match_score': 40,
                    'confidence': 0.5, 'matched_skills': [], 'missing_skills': ['Kubernetes'],
                    'experience_gap_years': None, 'hard_blockers': [], 'reason': 'Review platform depth.',
                })
            return Message()

    client = connections.AIClient(FallbackModel(), provider='ollama', model_name='qwen-test')
    evidence = '## Technical Skills\nLLM integration and agent/tool calling.'
    result = connections.classify_job_suitability(client, 'AI Platform Engineer', 'Acme', 'Remote',
        'LLM integration and Kubernetes.', evidence, job_id='job-fallback', run_id='run-fallback')
    page = trace_store.list_traces(filters={'job_id': 'job-fallback', 'run_id': 'run-fallback'})
    assert result['decision'] == 'REVIEW' and page['total'] == 2
    attempts = [trace_store.get_trace(item['trace_id']) for item in page['items']]
    assert {item['status'] for item in attempts} == {'error', 'completed'}
    failed = next(item for item in attempts if item['status'] == 'error')
    successful = next(item for item in attempts if item['status'] == 'completed')
    assert any(event['event_type'] == 'retry' for event in failed['events'])
    assert successful['retry_info']['attempt'] == 2
    assert successful['raw_response'].startswith('{')
    assert successful['parsed_output']['decision'] == 'REVIEW'


def test_deterministic_classifier_override_is_recorded_without_rewriting_model_output(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_TRACE_DB_PATH', str(tmp_path / 'trace.sqlite3'))
    monkeypatch.setattr(prompt_log, 'PROMPT_LOG_PATH', tmp_path / 'prompts.jsonl')
    trace_id = trace_store.create_trace(run_id='run-override', job_id='job-override',
        job_title='Engineer', company='Acme', category='job suitability', model='qwen-test',
        prompt='classify role')
    trace_store.complete_trace(trace_id, raw_response='model said APPLY',
                               parsed_output={'decision': 'APPLY'}, decision='APPLY')

    from modules.linkedin_modern import _trace_final_classifier_decision
    _trace_final_classifier_decision('job-override', 'Engineer', 'Acme', 'qwen-test',
        {'decision': 'REVIEW', 'failure_kind': None}, 'APPLY', 'REVIEW',
        'Reported hard blocker requires human review.', 'run-override')

    detail = trace_store.get_trace(trace_id)
    assert detail['raw_response'] == 'model said APPLY'
    assert detail['parsed_output'] == {'decision': 'APPLY'}
    assert detail['decision'] == 'REVIEW'
    final = detail['events'][-1]
    assert final['name'] == 'final_decision'
    assert final['payload']['model_decision'] == 'APPLY'
    assert final['payload']['override_reason'] == 'Reported hard blocker requires human review.'
