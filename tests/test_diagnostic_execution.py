import json
from types import SimpleNamespace

import pytest

from modules import diagnostic_execution as diagnostic
from modules import linkedin_modern as modern


JOB_ID = '4486767446'


def test_target_card_selection_only_returns_the_requested_job():
    cards = [SimpleNamespace(job_id='111'), SimpleNamespace(job_id=JOB_ID),
             SimpleNamespace(job_id='333')]
    selected = diagnostic.unique_target_card(cards, JOB_ID, lambda card: card.job_id)
    assert selected is cards[1]
    assert diagnostic.unique_target_card(cards, 'missing', lambda card: card.job_id) is None


def test_duplicate_target_cards_fail_closed():
    cards = [SimpleNamespace(job_id=JOB_ID), SimpleNamespace(job_id=JOB_ID)]
    with pytest.raises(diagnostic.DiagnosticJobAmbiguous):
        diagnostic.unique_target_card(cards, JOB_ID, lambda card: card.job_id)


def test_missing_diagnostic_job_id_is_rejected_by_cli():
    import runAiBot
    with pytest.raises(SystemExit):
        runAiBot._parse_execution_args(['--diagnostic'])
    with pytest.raises(SystemExit):
        runAiBot._parse_execution_args(['--diagnostic-job-id', 'not-a-linkedin-id'])
    assert runAiBot._parse_execution_args(['--diagnostic-job-id', JOB_ID]).diagnostic_job_id == JOB_ID


def test_attempt_budget_is_consumed_once_and_bound_to_job():
    budget = diagnostic.DiagnosticAttemptBudget(JOB_ID)
    assert budget.consume(JOB_ID) == 1
    with pytest.raises(diagnostic.DiagnosticAttemptLimitExceeded):
        budget.consume(JOB_ID)
    with pytest.raises(diagnostic.DiagnosticConfigurationError):
        diagnostic.DiagnosticAttemptBudget(JOB_ID).consume('another-job')


def test_budget_is_consumed_before_easy_apply_and_blocks_reopen(monkeypatch):
    from selenium.common.exceptions import StaleElementReferenceException

    class Button: pass
    class Driver:
        current_url = f'https://www.linkedin.com/jobs/search-results/?currentJobId={JOB_ID}'
    button = Button()
    driver = Driver()
    budget = diagnostic.DiagnosticAttemptBudget(JOB_ID)
    clicks = []
    monkeypatch.setattr(modern, '_raise_if_limited', lambda *args: None)
    monkeypatch.setattr(modern, '_visible_apply_buttons', lambda _: [button])
    monkeypatch.setattr(modern, '_selected_job_id_from_url', lambda _: JOB_ID)
    monkeypatch.setattr(modern, 'WebDriverWait', lambda *args, **kwargs: SimpleNamespace(
        until=lambda predicate: predicate(driver)))
    def stale_click(*args):
        clicks.append('easy-apply')
        raise StaleElementReferenceException('click target went stale')
    monkeypatch.setattr(modern, '_click', stale_click)

    for _ in range(2):
        with pytest.raises((StaleElementReferenceException, diagnostic.DiagnosticAttemptLimitExceeded)):
            modern._apply_current_job(
                driver, {}, {}, '', '', True, lambda _: None, 'Example', JOB_ID,
                diagnostic_budget=budget)
    assert budget.consumed == 1
    assert clicks == ['easy-apply']


def test_stale_field_retry_is_disabled_only_in_diagnostic_mode():
    budget = diagnostic.DiagnosticAttemptBudget(JOB_ID)
    assert not modern._diagnostic_should_retry_stale(0, budget)
    assert not modern._diagnostic_should_retry_stale(1, budget)
    assert modern._diagnostic_should_retry_stale(0, None)
    assert not modern._diagnostic_should_retry_stale(1, None)


def test_submit_click_guard_blocks_modern_and_legacy_adapters_in_diagnostic(monkeypatch):
    calls = []
    monkeypatch.setenv('BOT_DIAGNOSTIC_JOB_ID', JOB_ID)
    for adapter in ('modern', 'legacy'):
        with pytest.raises(diagnostic.DiagnosticSubmissionBlocked):
            diagnostic.perform_submission_click(
                lambda *a, **k: calls.append(adapter), diagnostic=False)
    assert calls == []


def test_explicit_diagnostic_session_blocks_submit_without_environment(monkeypatch):
    monkeypatch.delenv('BOT_DIAGNOSTIC_JOB_ID', raising=False)
    with pytest.raises(diagnostic.DiagnosticSubmissionBlocked):
        diagnostic.perform_submission_click(lambda: pytest.fail('Submit callback must not run'),
                                            diagnostic=True)


def test_production_submit_adapter_and_retry_policy_remain_unchanged(monkeypatch):
    monkeypatch.delenv('BOT_DIAGNOSTIC_JOB_ID', raising=False)
    calls = []
    result = diagnostic.perform_submission_click(lambda value: calls.append(value) or True, 'production')
    assert result is True
    assert calls == ['production']
    assert modern._diagnostic_should_retry_stale(0, None)


def test_diagnostic_dispatch_cannot_fall_through_to_normal_run(monkeypatch):
    import runAiBot
    calls = []
    monkeypatch.setattr(runAiBot, 'apply_to_jobs', lambda *a, **k: calls.append(('diagnostic', k['diagnostic_job_id'])))
    monkeypatch.setattr(runAiBot, 'run', lambda *a, **k: calls.append(('normal', None)))
    runAiBot._dispatch_initial_execution(JOB_ID, diagnostic.DiagnosticAttemptBudget(JOB_ID), None, None)
    assert calls == [('diagnostic', JOB_ID)]


def test_diagnostic_dispatch_preserves_normal_execution(monkeypatch):
    import runAiBot
    calls = []
    monkeypatch.setattr(runAiBot, 'apply_to_jobs', lambda *a, **k: calls.append('diagnostic'))
    monkeypatch.setattr(runAiBot, 'run', lambda *a, **k: calls.append('normal') or 2)
    assert runAiBot._dispatch_initial_execution(None, None, None, None) == 2
    assert calls == ['normal']


def test_diagnostic_run_creates_correlated_run_artifacts(tmp_path, monkeypatch):
    import runAiBot
    from modules import application_diagnostics, run_diagnostics
    monkeypatch.delenv('BOT_RUN_ID', raising=False)
    monkeypatch.delenv('BOT_DIAGNOSTIC_JOB_ID', raising=False)
    monkeypatch.setattr(runAiBot, 'print_lg', lambda *args, **kwargs: None)
    monkeypatch.setattr(runAiBot, 'setup_logging', lambda **kwargs: None)
    run_id = runAiBot._prepare_diagnostic_run(JOB_ID, tmp_path, configure_logging=True)
    directory = run_diagnostics.run_dir(run_id, tmp_path)
    assert run_diagnostics.valid_run_id(run_id)
    assert json.loads((directory / 'result.json').read_text())['run_id'] == run_id
    assert (directory / 'events.jsonl').is_file()
    assert (directory / 'worker.log').is_file()
    assert (directory / 'application_attempts.jsonl').is_file()
    assert __import__('os').environ['BOT_RUN_ID'] == run_id
    assert __import__('os').environ['BOT_DIAGNOSTIC_JOB_ID'] == JOB_ID
    assert application_diagnostics.ensure_attempt_log(run_id, tmp_path).is_file()


def test_diagnostic_application_audit_is_correlated_to_run_and_job(monkeypatch):
    import os
    audits = []
    class Audit:
        def __init__(self, job_id, title, company, run_id=None):
            self.entry = {'attempt_id': 'diagnostic-attempt', 'job_id': str(job_id),
                          'job_title': title, 'company': company, 'status': 'in_progress', 'run_id': run_id}
            audits.append(self)
        def _save(self): pass
        def finish(self, status, message): self.entry.update(status=status, result=message)
    monkeypatch.setattr(modern, 'ApplicationAudit', Audit)
    monkeypatch.setattr(modern, '_apply_current_job', lambda *args, **kwargs: ('skip', 'draft saved'))
    monkeypatch.setattr(modern.run_diagnostics, 'get_run', lambda *_: {'status': 'running'})
    outcome, _ = modern.apply_current_job(
        object(), {}, {}, '', '', True, lambda _: None, 'Example', JOB_ID,
        title='Backend Engineer', diagnostic_budget=diagnostic.DiagnosticAttemptBudget(JOB_ID),
        run_context=modern.application_diagnostics.ApplicationRunContext('b' * 32))
    assert outcome == 'skip'
    assert audits[0].entry['attempt_id'] == 'diagnostic-attempt'
    assert audits[0].entry['run_id'] == 'b' * 32
    assert audits[0].entry['job_id'] == JOB_ID


def test_every_submit_callsite_uses_the_shared_hard_guard():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    modern_source = (root / 'modules' / 'linkedin_modern.py').read_text()
    legacy_source = (root / 'runAiBot.py').read_text()
    assert modern_source.count('perform_submission_click(') == 1
    assert legacy_source.count('perform_submission_click(') == 1
