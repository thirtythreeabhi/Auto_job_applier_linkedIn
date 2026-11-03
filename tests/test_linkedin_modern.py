"""Checks for the current LinkedIn adapter's answer and card safety rules."""

def test_easy_apply_locator_accepts_visible_button_text_without_aria_label():
    from modules.linkedin_modern import APPLY_XPATH, _visible_apply_buttons
    from selenium.webdriver.common.by import By

    class Button:
        def is_displayed(self): return True
        def is_enabled(self): return True

    class Driver:
        def find_elements(self, by, xpath):
            assert by == By.XPATH
            assert xpath == APPLY_XPATH
            # LinkedIn's new blue Easy Apply button has visible text; an
            # exact "Easy Apply to this job" aria-label is not guaranteed.
            assert "normalize-space(.)='Easy Apply'" in xpath
            return [Button()]

    assert len(_visible_apply_buttons(Driver())) == 1


def test_apply_locator_ignores_hidden_or_disabled_controls():
    from modules.linkedin_modern import _visible_apply_buttons

    class Button:
        def __init__(self, shown, enabled): self.shown, self.enabled = shown, enabled
        def is_displayed(self): return self.shown
        def is_enabled(self): return self.enabled

    class Driver:
        def find_elements(self, by, xpath):
            return [Button(False, True), Button(True, False), Button(True, True)]

    assert len(_visible_apply_buttons(Driver())) == 1


def test_job_selection_tracks_current_job_id_not_sidebar_links():
    from modules.linkedin_modern import _selected_job_id_from_url, _resolved_selected_job_id

    class Driver:
        current_url = 'https://www.linkedin.com/jobs/search-results/?currentJobId=111'
        def find_elements(self, *args):
            raise AssertionError('Sidebar job links must not prove panel selection')

    driver = Driver()
    assert _selected_job_id_from_url(driver.current_url) == '111'
    assert not _resolved_selected_job_id(driver, '222', '111', False)
    assert not _resolved_selected_job_id(driver, None, '111', False)
    driver.current_url = 'https://www.linkedin.com/jobs/search-results/?currentJobId=222'
    assert _resolved_selected_job_id(driver, '222', '111', False) == '222'
    assert _resolved_selected_job_id(driver, None, '111', False) == '222'
    assert _resolved_selected_job_id(driver, '222', '222', True) == '222'
    assert _selected_job_id_from_url('https://www.linkedin.com/jobs/search-results/?keywords=python') is None


def test_missing_apply_control_is_not_misreported_as_no_easy_apply(monkeypatch):
    from modules import linkedin_modern as modern
    from selenium.common.exceptions import TimeoutException

    class FastWait:
        def __init__(self, *args, **kwargs): pass
        def until(self, predicate): raise TimeoutException('test: button did not load')

    class Driver:
        current_url = 'https://www.linkedin.com/jobs/search-results/?currentJobId=222'

    monkeypatch.setattr(modern, 'WebDriverWait', FastWait)
    outcome, message = modern._apply_current_job(
        Driver(), {}, {}, '', '/tmp/no-resume.pdf', True, lambda _: None, 'Example', '222')
    assert outcome == 'skip'
    assert 'not found' in message
    assert 'does not offer' not in message


import pytest
from modules.linkedin_modern import _answer_for, _card_details, _confirmation_matches, _experience_option, _job_status_confirms, _progress, _required_years, _within_candidate_location
from modules.linkedin_modern import _replace_text, _verify_review_name, CannotCompleteApplication
from selenium.webdriver.common.keys import Keys


def test_replacement_verifies_the_value_after_typing():
    class Field:
        value = 'Singh Sandal'
        selected = False
        def click(self): pass
        def send_keys(self, *keys):
            if keys == (Keys.CONTROL, 'a'): self.selected = True
            elif keys == (Keys.BACKSPACE,) and self.selected: self.value = ''
            else: self.value += ''.join(keys)
        def get_attribute(self, name): return self.value
    field = Field()
    _replace_text(field, 'Sandal', 'Last name')
    assert field.value == 'Sandal'


def test_replacement_refuses_a_field_that_appends_instead():
    class Field:
        def click(self): pass
        def send_keys(self, *keys): pass
        def get_attribute(self, name): return 'Singh SandalSandal'
    with pytest.raises(CannotCompleteApplication):
        _replace_text(Field(), 'Sandal', 'Last name')


def test_review_rejects_repeated_surname():
    class Registry:
        def value(self, key): return 'Sandal' if key == 'last_name' else None
    _verify_review_name('Abhijeet Singh Sandal', Registry())
    with pytest.raises(CannotCompleteApplication):
        _verify_review_name('Abhijeet Singh SandalSandalSandal', Registry())


@pytest.mark.parametrize('label', [
    'How many years of experience do you have with React?',
    'Do you require sponsorship?',
    'Are you legally authorized to work here?',
    'Do you hold a security clearance?',
])
def test_missing_or_skill_specific_answers_are_not_guessed(label):
    assert _answer_for(label, {}, {'years_of_experience': '1'}, '') is None


def test_saved_salary_is_converted_to_the_units_requested():
    questions = {'desired_salary': 600000, 'current_ctc': 4200000}
    assert _answer_for('Expected CTC in LPA', {}, questions, '') == '6'
    assert _answer_for('Current salary per month', {}, questions, '') == '350000'


def test_saved_name_and_city_are_used():
    profile = {'first_name': 'A', 'middle_name': 'B', 'last_name': 'C', 'current_city': 'New Delhi'}
    assert _answer_for('Last name*', profile, {}, '') == 'B C'
    assert _answer_for('City*', profile, {}, '') == 'New Delhi'
    assert _answer_for('Are you legally authorized to work in the United States?', {'state': 'Delhi'}, {}, '') is None


def test_experience_is_only_used_for_a_total_experience_question():
    questions = {'years_of_experience': '1'}
    assert _answer_for('Years of experience', {}, questions, '') == '1'
    assert _answer_for('Please select your total years of professional experience:*', {}, questions, '') == '1'
    assert _answer_for('Years of Python experience', {}, questions, '') is None
    assert _answer_for('How many years of experience do you have with Node.js?', {}, questions, '') is None


def test_only_confirmed_skill_and_additional_month_answers_are_used():
    questions = {'years_of_experience': '1', 'additional_experience_months': '0',
                 'nodejs_experience_years': '1', 'postgresql_experience_years': '1',
                 'fullstack_experience_years': '1', 'reactjs_experience_years': '1'}
    assert _answer_for('Please select your total additional months of experience:*', {}, questions, '') == '0'
    assert _answer_for('How many years of work experience do you have with Node.js?', {}, questions, '') == '1'
    assert _answer_for('How many years of work experience do you have with PostgreSQL?', {}, questions, '') == '1'
    assert _answer_for('How many years of full-stack development experience do you have?', {}, questions, '') == '1'
    assert _answer_for('How many years of work experience do you have with React.js?', {}, questions, '') == '1'
    assert _answer_for('How many years of work experience do you have with React Native?', {}, questions, '') is None
    assert _answer_for('How many years of experience with Node.js and AWS?', {}, questions, '') is None


def test_experience_option_accepts_one_year_but_not_overlapping_ranges():
    class Option:
        def __init__(self, text): self.text = text
    one = Option('1 year')
    assert _experience_option([Option('0 years'), one, Option('2 years')], '1') is one
    assert _experience_option([Option('0-1 years'), Option('1-3 years')], '1') is None
    zero_months = Option('0 months')
    assert _experience_option([zero_months, Option('1 month')], '0') is zero_months


def test_progress_requires_a_real_page_indicator():
    class Modal:
        text = 'Apply to Example\n2/7 pages\nResume'
    assert _progress(Modal()) == 2
    Modal.text = 'Review your application'
    assert _progress(Modal()) is None


def test_experience_filter_does_not_treat_company_age_as_a_requirement():
    assert _required_years('Founded 10 years ago; 2+ years of software development experience required.') == [2]
    assert _required_years('We have been operating for 10 years.') == []


def test_confirmation_must_name_the_current_employer():
    text = 'Your application was sent to Example, Inc!'
    assert _confirmation_matches(text, 'Example Inc')
    assert not _confirmation_matches(text, 'Another Company')
    assert not _confirmation_matches('Application ready for Example Inc', 'Example Inc')


def test_job_status_requires_the_employer_and_submitted_status():
    assert _job_status_confirms('Example Inc\nApplication status\nApplication submitted', 'Example Inc')
    assert not _job_status_confirms('Another Company\nApplication status\nApplication submitted', 'Example Inc')
    assert not _job_status_confirms('Example Inc\nApplication status\nIn progress', 'Example Inc')
    assert not _job_status_confirms('Application status\nApplication submitted', '')


def test_candidate_locations_do_not_assume_relocation_or_foreign_authorization():
    assert _within_candidate_location('New Delhi (On-site)')
    assert _within_candidate_location('Gurugram (Hybrid)')
    assert _within_candidate_location('India (Remote)')
    assert not _within_candidate_location('Bengaluru (On-site)')
    assert not _within_candidate_location('United States (Remote)')
    assert _within_candidate_location('Bengaluru (On-site)', anywhere_in_india=True)
    assert _within_candidate_location('Pune, Maharashtra (Hybrid)', anywhere_in_india=True)
    assert not _within_candidate_location('United States (Remote)', anywhere_in_india=True)


def test_card_details_ignores_repeated_title_and_selected_marker():
    class Dismiss:
        def get_attribute(self, name):
            return 'Dismiss Backend Developer job'
    class Card:
        text = 'Selected, Backend Developer\nBackend Developer\nExample Inc\nNew Delhi (Hybrid)\nEasy Apply'
        def find_element(self, *args):
            return Dismiss()
    title, company, location, _ = _card_details(Card())
    assert (title, company, location) == ('Backend Developer', 'Example Inc', 'New Delhi (Hybrid)')


def test_card_details_ignores_a_verification_badge_in_the_title():
    class Dismiss:
        def get_attribute(self, name):
            return 'Dismiss Full Stack Developer job'
    class Card:
        text = 'Full Stack Developer (Verified job)\nFull Stack Developer\nExample Inc\nBengaluru (Hybrid)'
        def find_element(self, *args):
            return Dismiss()
    assert _card_details(Card())[:3] == ('Full Stack Developer', 'Example Inc', 'Bengaluru (Hybrid)')


def test_resume_upload_uses_file_input_without_opening_picker(tmp_path, monkeypatch):
    from modules import linkedin_modern as modern
    resume = tmp_path / 'resume.pdf'
    resume.write_bytes(b'%PDF-1.4\n')
    class FileInput:
        def send_keys(self, value):
            assert value == str(resume)
            modal.uploaded = True
    class Radio:
        def find_elements(self, by, query): return [modal]
        def is_selected(self): return True
    class Modal:
        uploaded = False
        @property
        def text(self): return 'resume.pdf' if self.uploaded else ''
        def find_elements(self, by, query):
            if query == 'input[type="file"]': return [FileInput()]
            if query == 'input[type="radio"]': return [Radio()] if self.uploaded else []
            return []
    modal = Modal()
    monkeypatch.setattr(modern, '_modal', lambda driver: modal)
    monkeypatch.setattr(modern, '_click_text', lambda *args: pytest.fail('File picker was opened'))
    modern._ensure_resume(object(), modal, str(resume))
    assert modal.uploaded


def test_rechecks_name_after_the_form_updates():
    from modules import linkedin_modern as modern
    class Registry:
        values = {'first_name': 'Abhijeet', 'middle_name': 'Singh', 'last_name': 'Sandal'}
        def value(self, key): return self.values.get(key)
        def classify(self, label): return {'First name': 'first_name', 'Last name': 'last_name'}.get(label)
    class Field:
        def __init__(self, label, value): self.label, self.value = label, value
        def is_displayed(self): return True
        def get_attribute(self, attr): return self.value if attr == 'value' else None
    class Modal:
        def __init__(self, last): self.fields = [Field('First name', 'Abhijeet'), Field('Last name', last)]
        def find_elements(self, *args): return self.fields
    original = modern._field_label
    try:
        modern._field_label = lambda modal, field: field.label
        modern._verify_name_fields(Modal('Singh Sandal'), Registry())
        with pytest.raises(CannotCompleteApplication):
            modern._verify_name_fields(Modal('Singh SandalSandal'), Registry())
    finally:
        modern._field_label = original


def test_error_after_submission_attempt_is_treated_as_uncertain(monkeypatch):
    from modules import linkedin_modern as modern
    audits = []
    class Audit:
        def __init__(self, *args): self.entry = {'status': 'in_progress'}; audits.append(self)
        def finish(self, status, message): self.entry['status'] = status
    def fail_after_click(*args):
        audits[0].entry['status'] = 'submitting'
        raise RuntimeError('connection lost')
    monkeypatch.setattr(modern, 'ApplicationAudit', Audit)
    monkeypatch.setattr(modern, '_apply_current_job', fail_after_click)
    with pytest.raises(modern.SubmissionUncertain):
        modern.apply_current_job(None, {}, {}, '', '', False, lambda x: None, 'Example', '123')
    assert audits[0].entry['status'] == 'uncertain'


def test_dismiss_saves_unfinished_application_instead_of_discarding(monkeypatch):
    from modules import linkedin_modern as modern
    class Driver:
        prompt = False
        closed = False
        def find_elements(self, by, query): return [dialog] if self.prompt else []
    driver = Driver()
    class Button:
        def __init__(self, text): self.text = text
        def is_displayed(self): return True
        def click(self):
            if self.text == 'Dismiss': driver.prompt = True
            elif self.text == 'Save': driver.prompt = False; driver.closed = True
            else: pytest.fail('An unfinished application was discarded')
    class ApplicationModal:
        def find_elements(self, *args): return [Button('Dismiss')]
    class Dialog:
        text = 'Save this application?'
        def is_displayed(self): return True
        def find_elements(self, *args): return [Button('Discard'), Button('Save')]
    dialog = Dialog()
    monkeypatch.setattr(modern, '_modal', lambda d: None if d.prompt or d.closed else ApplicationModal())
    monkeypatch.setattr(modern.time, 'sleep', lambda _: None)
    modern._dismiss(driver)
    assert driver.closed


def test_profile_matched_jobs_uses_linkedins_own_list():
    from modules.linkedin_modern import open_profile_matched_jobs
    class Link:
        def is_displayed(self): return True
        def get_attribute(self, name):
            return 'https://www.linkedin.com/jobs/search-results/?origin=JobSearchOrigin_QUALIFICATION_LANDING'
    class Driver:
        current_url = ''
        history = []
        def get(self, url): self.current_url = url; self.history.append(url)
        def find_elements(self, by, query):
            return [Link()] if 'QUALIFICATION_LANDING' in query else [object()]
    driver = Driver()
    assert open_profile_matched_jobs(driver, lambda message: None)
    assert driver.history[0] == 'https://www.linkedin.com/jobs/'
    assert 'QUALIFICATION_LANDING' in driver.history[1]


def test_dismiss_resolves_save_prompt_before_underlying_form(monkeypatch):
    from modules import linkedin_modern as modern
    class Driver:
        closed = False
        def find_elements(self, *args): return [] if self.closed else [underlying, prompt]
    driver = Driver()
    class Button:
        def __init__(self, text, label=''): self.text, self.label = text, label
        def is_displayed(self): return True
        def get_attribute(self, name): return self.label if name == 'aria-label' else None
        def click(self):
            if self.text == 'Save': driver.closed = True
            else: pytest.fail('The underlying form or discard was clicked')
    class Dialog:
        def __init__(self, text, buttons): self.text, self.buttons = text, buttons
        def is_displayed(self): return True
        def find_elements(self, *args): return self.buttons
    underlying = Dialog('Apply to Example', [Button('', 'Dismiss')])
    prompt = Dialog('Save this application?', [Button('Discard'), Button('Save')])
    monkeypatch.setattr(modern, '_modal', lambda d: None if d.closed else underlying)
    monkeypatch.setattr(modern.time, 'sleep', lambda _: None)
    assert modern._dismiss(driver) is True
    assert driver.closed


def test_linkedin_radio_group_without_required_attribute_uses_question_and_option_text(monkeypatch):
    from modules import linkedin_modern as modern
    class Radio:
        tag_name = 'input'
        def __init__(self, name, value): self.name, self.value, self.selected = name, value, False
        def get_attribute(self, key):
            return {'type':'radio', 'aria-label':"Are you comfortable commuting to this job's location?", 'name':self.name, 'id':self.value}.get(key)
        def is_selected(self): return self.selected
        def find_element(self, by, query):
            class Parent: pass
            p=Parent(); p.text=self.value; return p
    yes, no = Radio('commute','Yes'), Radio('commute','No')
    class Parent: text = "Are you comfortable commuting to this job's location?*\nYes\nNo"
    class Group:
        text = 'Yes\nNo'
        def get_attribute(self, key): return None
        def find_element(self, *args): return Parent()
        def find_elements(self, by, query):
            return [yes,no] if 'input[' in query else []
    class Modal:
        def find_elements(self, by, query):
            if query == './/input|.//select|.//textarea': return [yes,no]
            if query == './/fieldset|.//*[@role="radiogroup"]': return [Group()]
            return []
    class Registry:
        def unverified(self, label): return False
        def answer(self, label, job_id=None): return 'Yes' if job_id == '4485704953' else None
        def classify(self, label): return None
    monkeypatch.setattr(modern, '_click', lambda driver, option: setattr(option,'selected',True))
    modern._fill_fields(None, Modal(), {}, {}, '', lambda _: None, Registry(), job_id='4485704953')
    assert yes.selected and not no.selected


def test_optional_company_follow_is_cleared_before_submission(monkeypatch):
    from modules import linkedin_modern as modern
    class Checkbox:
        selected = True
        def get_attribute(self, name):
            return {'aria-label':'Follow Example to stay up to date with their page','id':'follow'}.get(name)
        def is_selected(self): return self.selected
    field=Checkbox()
    class Modal:
        def find_elements(self, by, query):
            return [field] if query == 'input[type="checkbox"]' else []
    monkeypatch.setattr(modern, '_click', lambda driver, control: setattr(control,'selected',False))
    modern._clear_optional_company_follow(None, Modal())
    assert not field.selected
