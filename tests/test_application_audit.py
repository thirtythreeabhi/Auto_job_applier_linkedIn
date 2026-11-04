import json
import stat

from modules.application_audit import ApplicationAudit


def test_suitability_audit_preserves_source_model_final_and_snapshot(tmp_path):
    from modules.application_audit import record_suitability_decision
    path = tmp_path / 'audit.json'
    record_suitability_decision(
        '123', 'Engineer', 'Example', 'New Delhi', 'qwen-test',
        {'decision': 'REVIEW', 'reason': 'Core fit; AWS needs confirmation.',
         'match_score': 75, 'confidence': 0.8, 'matched_skills': ['React'],
         'missing_skills': ['AWS'], 'hard_blockers': []},
        path, decision_source='QWEN_CLASSIFIER', model_decision='APPLY',
        final_decision='REVIEW', override_reason='Hard blocker requires review.',
        model_called=True, job_description='Full job posting text')

    row = json.loads(path.read_text())['suitability_decisions'][0]
    assert row['decision'] == row['final_decision'] == 'REVIEW'
    assert row['model_decision'] == 'APPLY'
    assert row['decision_source'] == 'QWEN_CLASSIFIER'
    assert row['override_reason'] == 'Hard blocker requires review.'
    assert row['model_called'] is True
    assert row['job_description'] == 'Full job posting text'
    assert row['matched_skills'] == ['React']
    assert row['missing_skills'] == ['AWS']


def test_attempts_record_values_outcomes_and_are_private(tmp_path):
    path = tmp_path / 'audit.json'
    first = ApplicationAudit('123', 'Software Engineer', 'Example', path)
    first.field('Last name', 'Singh Sandal', 'filled', 1)
    first.field('Email', 'person@example.com', 'verified prefilled', 1)
    first.field('Password', 'do-not-store', 'filled', 1)
    first.field('API key', 'also-secret', 'filled', 1)
    first.finish('submitted', 'LinkedIn confirmed the application was sent.')
    second = ApplicationAudit('123', 'Software Engineer', 'Example', path)
    second.finish('skip', 'Already applied')
    data = json.loads(path.read_text())
    assert len(data['applications']) == 2
    assert data['applications'][0]['status'] == 'submitted'
    assert [(f['label'], f['value'], f['source']) for f in data['applications'][0]['fields']] == [
        ('Last name', 'Singh Sandal', 'filled'), ('Email', 'person@example.com', 'verified prefilled')]
    assert 'do-not-store' not in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_pending_submission_is_persisted_before_confirmation(tmp_path):
    path = tmp_path / 'audit.json'
    audit = ApplicationAudit('123', 'Engineer', 'Example', path)
    audit.finish('submitting', 'Confirmation pending')
    assert json.loads(path.read_text())['applications'][0]['status'] == 'submitting'


def test_attempted_jobs_and_duplicate_submitted_roles_are_remembered(tmp_path):
    from modules.application_audit import previously_attempted_ids, previously_submitted_roles, role_key
    audit_path = tmp_path / 'audit.json'
    history = tmp_path / 'history.csv'
    history.write_text('Job ID,Title,Company\n1,Software Engineer,Example Inc\n', encoding='utf-8')
    a = ApplicationAudit('2', 'Backend Developer', 'Another Co', audit_path)
    a.finish('skip', 'Missing an answer')
    b = ApplicationAudit('3', 'Frontend Developer', 'Example Inc', audit_path)
    b.finish('submitted', 'Confirmed')
    c = ApplicationAudit('4', 'Test Job', 'Testing Co', audit_path)
    c.finish('review', 'Dry run')
    assert previously_attempted_ids(audit_path) == {'2', '3', '4'}
    roles = previously_submitted_roles(history, audit_path)
    assert role_key('EXAMPLE INC', 'Software Engineer') in roles
    assert role_key('Example Inc', 'Frontend Developer') in roles
    assert role_key('Another Co', 'Backend Developer') not in roles


def test_previously_skipped_job_is_retried_only_when_its_answer_is_now_verified(tmp_path):
    from modules.application_audit import previously_attempted_ids
    path = tmp_path / 'audit.json'
    a = ApplicationAudit('10', 'Engineer', 'Example', path)
    a.finish('skip', 'No verified answer for required question: Have you used TypeScript in a project?.')
    b = ApplicationAudit('11', 'Engineer', 'Other', path)
    b.finish('skip', 'No verified answer for required question: Do you require a visa?.')
    c = ApplicationAudit('12', 'Engineer', 'Third', path)
    c.finish('submitted', 'Confirmed')
    class Registry:
        def answer(self, question): return 'Yes' if 'TypeScript' in question else None
    assert previously_attempted_ids(path, Registry()) == {'11', '12'}
    assert previously_attempted_ids(path) == {'10', '11', '12'}


def test_status_aware_retry_policy_and_stale_interrupted_attempts(tmp_path):
    from datetime import datetime, timedelta, timezone
    from modules.application_audit import previous_attempt_reasons

    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    def row(job_id, status, age_hours=0, result=''):
        started = now - timedelta(hours=age_hours)
        return {'job_id': job_id, 'job_title': 'Engineer', 'company': 'Example',
                'status': status, 'result': result, 'started_at': started.isoformat()}

    rows = [
        row('submitted', 'submitted'),
        row('submitted_then_error', 'submitted', age_hours=2),
        row('submitted_then_error', 'error'),
        row('submitting', 'submitting'),
        row('uncertain', 'uncertain'),
        row('uncertain_then_error', 'uncertain', age_hours=2),
        row('uncertain_then_error', 'error'),
        row('fresh', 'in_progress', age_hours=1),
        row('stale', 'in_progress', age_hours=8),
        row('error_retry', 'error'),
        row('error_capped', 'error', age_hours=3),
        row('error_capped', 'error', age_hours=2),
        row('error_capped', 'error', age_hours=1),
        row('answer_ready', 'skip', result='No verified answer for required question: Have you used TypeScript?'),
        row('answer_cap', 'skip', age_hours=3, result='No verified answer for required question: Have you used TypeScript?'),
        row('answer_cap', 'skip', age_hours=2, result='No verified answer for required question: Have you used TypeScript?'),
        row('answer_cap', 'skip', age_hours=1, result='No verified answer for required question: Have you used TypeScript?'),
        row('answer_missing', 'skip', result='No verified answer for required question: Do you require sponsorship?'),
        row('other_skip', 'skip', result='The application dialog could not be closed.'),
        row('review', 'review', result='Paused for a manual check.'),
        row('not_sent', 'not_submitted', result='LinkedIn limit; no submission sent.'),
    ]
    path = tmp_path / 'audit.json'
    path.write_text(json.dumps({'version': 1, 'applications': rows}), encoding='utf-8')

    class Registry:
        def answer(self, question, company=None):
            return 'Yes' if 'TypeScript' in question else None

    reasons = previous_attempt_reasons(
        path, Registry(), max_error_attempts=3, max_eligible_skip_attempts=3,
        stale_in_progress_seconds=6 * 60 * 60, now=now)
    assert reasons == {
        'submitted': 'ALREADY_SUBMITTED',
        'submitted_then_error': 'ALREADY_SUBMITTED',
        'submitting': 'SUBMISSION_UNCERTAIN',
        'uncertain': 'SUBMISSION_UNCERTAIN',
        'uncertain_then_error': 'SUBMISSION_UNCERTAIN',
        'fresh': 'IN_PROGRESS',
        'error_capped': 'PREVIOUS_ATTEMPT',
        'answer_cap': 'PREVIOUS_ATTEMPT',
        'answer_missing': 'PREVIOUS_ATTEMPT',
        'other_skip': 'PREVIOUS_ATTEMPT',
        'review': 'REVIEW',
    }


def test_latest_review_replaces_older_missing_answer_skip(tmp_path):
    from datetime import datetime, timedelta, timezone
    from modules.application_audit import previous_attempt_reasons

    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    rows = [
        {'job_id': '123', 'status': 'skip', 'result': 'No verified answer for required question: TypeScript?',
         'started_at': (now - timedelta(days=1)).isoformat()},
        {'job_id': '123', 'status': 'review', 'result': 'Held for manual review.',
         'started_at': now.isoformat()},
    ]
    path = tmp_path / 'audit.json'
    path.write_text(json.dumps({'version': 1, 'applications': rows}), encoding='utf-8')

    class Registry:
        def answer(self, question): return 'Yes'

    assert previous_attempt_reasons(path, Registry(), now=now) == {'123': 'REVIEW'}


def test_only_confirmed_submissions_block_roles(tmp_path):
    from modules.application_audit import previously_submitted_roles, role_key
    path = tmp_path / 'audit.json'
    path.write_text(json.dumps({'version': 1, 'applications': [
        {'job_id': '1', 'job_title': 'Engineer', 'company': 'Confirmed Co', 'status': 'submitted'},
        {'job_id': '2', 'job_title': 'Engineer', 'company': 'Uncertain Co', 'status': 'uncertain'},
        {'job_id': '3', 'job_title': 'Engineer', 'company': 'Pending Co', 'status': 'submitting'},
    ]}), encoding='utf-8')
    roles = previously_submitted_roles(tmp_path / 'missing.csv', path)
    assert role_key('Confirmed Co', 'Engineer') in roles
    assert role_key('Uncertain Co', 'Engineer') not in roles
    assert role_key('Pending Co', 'Engineer') not in roles
