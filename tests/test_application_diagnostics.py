import json
from types import SimpleNamespace

import pytest
from selenium.common.exceptions import (StaleElementReferenceException,
                                        TimeoutException, WebDriverException)

from modules import application_diagnostics, linkedin_modern as modern


RUN_ID = 'a' * 32


class FakeDriver:
    current_url = 'https://www.linkedin.com/jobs/search-results/?currentJobId=4486767446'


def _audit():
    return SimpleNamespace(
        entry={'attempt_id': 'attempt-123', 'job_id': '4486767446',
               'job_title': 'Backend Engineer', 'company': 'Arohana Tech Solutions',
               'run_id': RUN_ID,
               'fields': [{'label': 'Expected salary', 'value': 'secret-salary'}]},
        diagnostic_phase='FILLING_FIELDS', diagnostic_last_operation='type answer',
        diagnostic_page_index=2, _diagnostic_exception_ids=set())


@pytest.mark.parametrize('error', [
    StaleElementReferenceException('element became stale'),
    TimeoutException('form did not load'),
    WebDriverException('browser command failed'),
])
def test_selenium_exceptions_are_structured_and_correlated(monkeypatch, error):
    events = []
    monkeypatch.setattr(application_diagnostics, 'append_exception_event', events.append)
    monkeypatch.setattr(modern, '_modal', lambda driver: object())
    audit = _audit()
    try:
        raise error
    except type(error) as caught:
        modern._record_attempt_exception(audit, FakeDriver(), caught)

    event = events[0]
    assert event['run_id'] == RUN_ID
    assert event['attempt_id'] == 'attempt-123'
    assert event['job_id'] == '4486767446'
    assert event['job_title'] == 'Backend Engineer'
    assert event['company'] == 'Arohana Tech Solutions'
    assert event['exception_class'] == type(error).__name__
    assert event['exception_message'] == str(error)
    assert type(error).__name__ in event['traceback']
    assert event['current_url'] == FakeDriver.current_url
    assert event['phase'] == 'FILLING_FIELDS'
    assert event['form_page_index'] == 2
    assert event['easy_apply_modal_exists'] is True
    assert event['last_operation'] == 'type answer'
    assert 'secret-salary' not in json.dumps(event)


def test_submission_verification_exception_is_recorded_in_verification_phase(monkeypatch):
    events = []
    monkeypatch.setattr(application_diagnostics, 'append_exception_event', events.append)
    monkeypatch.setattr(modern, 'WebDriverWait', lambda *args, **kwargs: SimpleNamespace(
        until=lambda predicate: (_ for _ in ()).throw(TimeoutException('status wait expired'))))

    class Driver(FakeDriver):
        def get(self, url): self.current_url = url
        def find_element(self, *args): return SimpleNamespace(text='Application status: In progress')

    audit = _audit()
    with pytest.raises(modern.SubmissionUncertain):
        modern._confirm_submission_and_restore_search(
            Driver(), '4486767446', 'Arohana Tech Solutions', FakeDriver.current_url,
            audit, lambda _: None)
    assert events[0]['phase'] == 'VERIFYING_SUBMISSION'
    assert events[0]['last_operation'] == 'verify exact-job application status'
    assert events[0]['exception_class'] == 'TimeoutException'


def test_missing_url_and_unavailable_modal_are_safe_in_diagnostic_capture(monkeypatch):
    events = []
    monkeypatch.setattr(application_diagnostics, 'append_exception_event', events.append)
    monkeypatch.setattr(modern, '_modal', lambda driver: (_ for _ in ()).throw(WebDriverException('closed')))

    class BrokenDriver:
        @property
        def current_url(self): raise WebDriverException('no window')

    modern._record_attempt_exception(_audit(), BrokenDriver(), TimeoutException('wait ended'))
    assert events[0]['current_url'] is None
    assert events[0]['easy_apply_modal_exists'] is None
    assert events[0]['exception_class'] == 'TimeoutException'


def test_diagnostic_writer_failure_does_not_mask_original_exception(monkeypatch, capsys):
    original = StaleElementReferenceException('original stale failure')
    monkeypatch.setattr(application_diagnostics, 'append_exception_event',
                        lambda event: (_ for _ in ()).throw(OSError('disk unavailable')))
    with pytest.raises(StaleElementReferenceException) as caught:
        try:
            raise original
        except StaleElementReferenceException as error:
            modern._record_attempt_exception(_audit(), FakeDriver(), error)
            raise
    assert caught.value is original
    assert 'Could not persist application diagnostic event' in capsys.readouterr().err


def test_attempt_diagnostics_append_without_overwriting_prior_evidence(tmp_path):
    root = tmp_path / 'runs'
    event = {'run_id': RUN_ID, 'attempt_id': 'attempt-1', 'job_id': '4486767446',
             'exception_class': 'TimeoutException', 'phase': 'WAITING_FOR_MODAL'}
    application_diagnostics.append_exception_event(event, root)
    application_diagnostics.append_exception_event({**event, 'attempt_id': 'attempt-2'}, root)
    path = root / RUN_ID / 'application_attempts.jsonl'
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row['attempt_id'] for row in records] == ['attempt-1', 'attempt-2']
    assert all(row['schema_version'] == 2 and row['event'] == 'application_exception' for row in records)


def test_worker_run_context_is_validated_and_never_substituted(monkeypatch):
    monkeypatch.setenv('BOT_RUN_ID', RUN_ID)
    monkeypatch.setattr(application_diagnostics.run_diagnostics, 'get_run',
                        lambda run_id, root: {'run_id': run_id, 'status': 'running'})
    context = application_diagnostics.run_context_from_environment()
    assert context.run_id == RUN_ID
    assert context.source == 'environment'
    monkeypatch.setenv('BOT_RUN_ID', 'b' * 32)
    monkeypatch.setattr(application_diagnostics.run_diagnostics, 'get_run', lambda *_: None)
    with pytest.raises(application_diagnostics.ApplicationRunContextError):
        application_diagnostics.run_context_from_environment()


def test_direct_attempt_without_run_context_is_explicitly_unassigned(tmp_path):
    audit = _audit()
    audit.entry['run_id'] = None
    audit.diagnostic_root = tmp_path / 'runs'
    events = []
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(application_diagnostics, 'append_exception_event',
                        lambda event, root: events.append((event, root)))
    monkeypatch.setattr(modern, '_modal', lambda driver: None)
    modern._record_attempt_exception(audit, FakeDriver(), TimeoutException('wait ended'))
    assert events[0][0]['run_id'] is None
    assert events[0][0]['run_id_source'] == 'direct'
    assert events[0][1] == audit.diagnostic_root
    monkeypatch.undo()


def test_run_scoped_and_unassigned_jsonl_destinations(tmp_path):
    root = tmp_path / 'runs'
    run_path = application_diagnostics.ensure_attempt_log(RUN_ID, root)
    assert run_path == root / RUN_ID / 'application_attempts.jsonl'
    application_diagnostics.append_exception_event(
        {'run_id': RUN_ID, 'attempt_id': 'at-1', 'job_id': '55'}, root)
    assert json.loads(run_path.read_text().splitlines()[0])['run_id'] == RUN_ID
    unassigned_path = application_diagnostics.ensure_attempt_log(None, root)
    assert unassigned_path == tmp_path / 'application_attempts_unassigned.jsonl'
    application_diagnostics.append_exception_event(
        {'run_id': None, 'attempt_id': 'at-2', 'job_id': '56'}, root)
    assert json.loads(unassigned_path.read_text().splitlines()[0])['run_id'] is None


def test_application_audit_create_and_update_preserve_run_id(tmp_path):
    from modules.application_audit import ApplicationAudit, record_suitability_decision
    path = tmp_path / 'application_audit.json'
    audit = ApplicationAudit('55', 'Engineer', 'Example', path=path, run_id=RUN_ID)
    audit.field('Question label', 'redacted-answer', 'verified', 1)
    audit.finish('error', 'failure')
    entry = json.loads(path.read_text())['applications'][0]
    assert entry['run_id'] == RUN_ID
    assert entry['status'] == 'error'
    record_suitability_decision('56', 'Engineer', 'Other', 'Remote', 'classifier',
        {'decision': 'APPLY'}, path=path, final_decision='APPLY', run_id=RUN_ID)
    decisions = json.loads(path.read_text())['suitability_decisions']
    assert decisions[0]['run_id'] == RUN_ID


def test_diagnostic_wait_records_exact_predicate_timeout_and_locator(monkeypatch):
    events = []
    monkeypatch.setattr(application_diagnostics, 'append_exception_event', events.append)
    monkeypatch.setattr(modern, '_modal', lambda driver: None)
    class Wait:
        def __init__(self, driver, timeout): self.timeout = timeout
        def until(self, predicate): raise TimeoutException('not visible')
    monkeypatch.setattr(modern, 'WebDriverWait', Wait)
    audit = _audit()
    with pytest.raises(TimeoutException):
        modern._diagnostic_wait(FakeDriver(), audit, 8, lambda _: False,
            phase='FILLING_FIELDS', operation='wait for city suggestion',
            predicate_name='visible role=option matches requested city and country',
            locator_strategy='xpath', selector='.//*[@role="option"]', page=2)
    event = events[0]
    assert event['wait_timeout_seconds'] == 8
    assert event['wait_predicate'] == 'visible role=option matches requested city and country'
    assert event['locator_strategy'] == 'xpath'
    assert event['selector'] == './/*[@role="option"]'
    assert event['form_page_index'] == 2


def test_field_failure_includes_label_but_redacts_entered_value(monkeypatch):
    events = []
    monkeypatch.setattr(application_diagnostics, 'append_exception_event', events.append)
    monkeypatch.setattr(modern, '_modal', lambda driver: None)
    audit = _audit()
    audit.diagnostic_field_label = 'Annual compensation'
    audit.diagnostic_sensitive_values = ['salary-sensitive-value']
    error = StaleElementReferenceException('field salary-sensitive-value was detached')
    modern._record_attempt_exception(audit, FakeDriver(), error)
    event = events[0]
    assert event['field_label'] == 'Annual compensation'
    assert 'salary-sensitive-value' not in json.dumps(event)
    assert '[REDACTED_FORM_VALUE]' in event['exception_message']


def test_propagated_exception_is_written_once(monkeypatch):
    events = []
    monkeypatch.setattr(application_diagnostics, 'append_exception_event', events.append)
    monkeypatch.setattr(modern, '_modal', lambda driver: None)
    audit = _audit()
    error = TimeoutException('one failure')
    modern._record_attempt_exception(audit, FakeDriver(), error)
    modern._record_attempt_exception(audit, FakeDriver(), error, phase='OPENING_APPLICATION')
    assert len(events) == 1


def test_cleanup_failure_is_separate_and_keeps_original_error(monkeypatch):
    events = []
    monkeypatch.setattr(application_diagnostics, 'append_exception_event', events.append)
    monkeypatch.setattr(modern, '_modal', lambda driver: None)
    monkeypatch.setattr(modern, '_dismiss', lambda *args, **kwargs:
                        (_ for _ in ()).throw(WebDriverException('dismiss intercepted')))
    audit = _audit()
    original = modern.CannotCompleteApplication('field did not validate')
    with pytest.raises(modern.CannotCompleteApplication) as caught:
        modern._dismiss_preserving_application_error(FakeDriver(), audit, original)
    assert caught.value is original
    assert len(events) == 2
    assert events[0]['exception_message'] == 'field did not validate'
    assert events[1]['phase'] == 'CLEANING_UP'


def test_intercepted_cleanup_click_records_dialog_target_and_overlay(monkeypatch):
    from selenium.common.exceptions import ElementClickInterceptedException
    events = []
    monkeypatch.setattr(application_diagnostics, 'append_exception_event', events.append)
    monkeypatch.setattr(modern, '_modal', lambda driver: None)
    class Button:
        text = 'Dismiss'
        def get_attribute(self, name): return 'Dismiss' if name == 'aria-label' else None
        def is_displayed(self): return True
        def click(self): raise ElementClickInterceptedException('open save dialog intercepted click')
    class Dialog:
        def is_displayed(self): return True
        def get_attribute(self, name): return 'save-application' if name == 'data-testid' else None
        def find_elements(self, by, selector):
            if selector == 'h1,h2,h3': return [type('Header', (), {'text': 'Save this application?'})()]
            if selector == 'button': return [Button()]
            return []
    class Driver(FakeDriver):
        def find_elements(self, by, selector): return [Dialog(), Dialog()]
    audit = _audit()
    audit.diagnostic_cleanup_reason = 'application flow could not continue'
    audit.diagnostic_save_prompt_detected = True
    with pytest.raises(ElementClickInterceptedException) as caught:
        modern._cleanup_click(Driver(), Button(), audit,
            'click application Dismiss control', './/button[@aria-label="Dismiss"]')
    event = events[0]
    assert event['last_operation'] == 'click application Dismiss control'
    assert event['locator_strategy'] == 'xpath'
    assert event['selector'] == './/button[@aria-label="Dismiss"]'
    assert event['observation']['target'] == 'Dismiss/Close control'
    assert event['observation']['overlay_present'] is True
    assert event['observation']['save_prompt_detected'] is True
    assert 'Save this application?' in event['observation']['dialogs_before'][0]['header_labels']


def test_field_filling_operation_metadata_survives_stale_exception(monkeypatch):
    events = []
    monkeypatch.setattr(application_diagnostics, 'append_exception_event', events.append)
    monkeypatch.setattr(modern, '_modal', lambda driver: None)
    audit = _audit()
    class Field:
        tag_name = 'input'
        def get_attribute(self, name):
            return {'type': 'text', 'required': None, 'value': '', 'maxlength': None}.get(name)
    class Modal:
        def find_elements(self, by, selector):
            if selector == 'input[data-testid="typeahead-input"]': return []
            if selector == './/input|.//select|.//textarea': return [Field()]
            return []
    modal = Modal()
    monkeypatch.setattr(modern, '_modal', lambda driver: modal)
    monkeypatch.setattr(modern, '_progress', lambda current: 3)
    error = StaleElementReferenceException('field secret-form-value is detached')
    monkeypatch.setattr(modern, '_field_label', lambda modal, field: 'City')
    monkeypatch.setattr(modern, '_answer_for', lambda *args: 'secret-form-value')
    monkeypatch.setattr(modern, '_replace_text', lambda *args: (_ for _ in ()).throw(error))
    with pytest.raises(StaleElementReferenceException) as caught:
        modern._fill_fields(FakeDriver(), Modal(), {}, {}, '', lambda _: None,
                            audit=audit, page=3)
    modern._record_attempt_exception(audit, FakeDriver(), caught.value)
    assert events[0]['phase'] == 'FILLING_FIELDS'
    assert events[0]['last_operation'] == 'replace text in application field'
    assert events[0]['field_label'] == 'City'
    assert events[0]['locator_strategy'] == 'xpath'
    assert 'secret-form-value' not in json.dumps(events[0])
