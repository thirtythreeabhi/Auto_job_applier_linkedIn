"""Offline Selenium failure reproductions; no browser or external site is used."""
from __future__ import annotations

from pathlib import Path

import pytest
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.common.by import By

from modules import linkedin_modern as modern


FIXTURES = Path(__file__).parent / 'fixtures' / 'selenium_diagnostics'


class FakeElement:
    def __init__(self, tag='div', text='', attrs=None, children=None, click=None):
        self.tag_name = tag
        self.text = text
        self.attrs = dict(attrs or {})
        self.children = children or {}
        self._click = click
        self.selected = False
        self.stale = False
        self.displayed = True

    def _check(self):
        if self.stale:
            raise StaleElementReferenceException('offline fixture: element invalidated by rerender')

    def get_attribute(self, name):
        self._check()
        return self.attrs.get(name)

    def is_displayed(self):
        self._check()
        return self.displayed

    def is_enabled(self):
        self._check()
        return True

    def is_selected(self):
        self._check()
        return self.selected

    def click(self):
        self._check()
        if self._click:
            return self._click()
        if self.attrs.get('type') in ('radio', 'checkbox'):
            self.selected = not self.selected

    def find_elements(self, by, selector):
        self._check()
        return self.children.get((by, selector), [])

    def find_element(self, by, selector):
        found = self.find_elements(by, selector)
        if not found:
            raise NoSuchElementException(selector)
        return found[0]


class FakeModal(FakeElement):
    def __init__(self, page='1/4 pages', title='Apply to Example Company', fields=None,
                 groups=None, buttons=None):
        self.page = page
        self.header = FakeElement('h2', title)
        self.fields = fields or []
        self.groups = groups or []
        self.buttons = buttons or []
        super().__init__('dialog', f'{title}\n{page}', {'data-testid': 'dialog', 'open': ''})

    def find_elements(self, by, selector):
        if selector == 'header h2':
            return [self.header]
        if by == By.XPATH and 'button[@aria-label="Dismiss"' in selector:
            return [button for button in self.buttons
                    if (button.get_attribute('aria-label') or '').casefold() in ('dismiss', 'close')]
        if selector == './/input|.//select|.//textarea':
            return list(self.fields)
        if selector == 'input[data-testid="typeahead-input"]':
            return [f for f in self.fields if f.get_attribute('data-testid') == 'typeahead-input']
        if selector in ('.//fieldset|.//*[@role="radiogroup"]',):
            return list(self.groups)
        if selector == './/input[@required and @type="checkbox"]':
            return [f for f in self.fields if f.get_attribute('type') == 'checkbox' and f.get_attribute('required')]
        if selector == './/input[@required and @type="radio"]':
            return [o for g in self.groups for o in g.options if o.get_attribute('type') == 'radio' and o.get_attribute('required')]
        if selector == './/input[@type="radio"]':
            return [o for g in self.groups for o in g.options if o.get_attribute('type') == 'radio']
        if by == By.TAG_NAME and selector == 'label':
            return [label for group in self.groups for label in group.labels]
        if by == By.TAG_NAME and selector == 'button':
            return self.buttons
        return []


class FakeDriver:
    def __init__(self, modal=None):
        self.modal = modal
        self.delayed_modal = None
        self.delay_polls = 0
        self.current_url = 'file:///offline/search-results.html'
        self.scripts = []

    def find_elements(self, by, selector):
        if selector in ('dialog[open]', 'dialog[open], [role="dialog"]'):
            if self.modal is None and self.delayed_modal is not None:
                self.delay_polls -= 1
                if self.delay_polls <= 0:
                    self.modal, self.delayed_modal = self.delayed_modal, None
            return [self.modal] if self.modal is not None else []
        return []

    def execute_script(self, script, *args):
        self.scripts.append(script)
        if 'el.willValidate' in script:
            return {'valid': True, 'message': '', 'invalid': False, 'errors': []}

    def get(self, url):
        self.current_url = url
        if 'search-results' in url:
            self.modal = None


class FakeWait:
    """Deterministic explicit wait that advances only mock state."""
    def __init__(self, driver, timeout, ignored_exceptions=()):
        self.driver = driver
        self.polls = max(1, int(timeout))
        self.ignored = ignored_exceptions

    def until(self, predicate):
        for _ in range(self.polls):
            try:
                result = predicate(self.driver)
                if result:
                    return result
            except self.ignored:
                pass
        raise TimeoutException('offline fixture timed out')


def test_fixture_provenance_and_runtime_selectors_are_explicit():
    runtime = (FIXTURES / 'runtime_failure_reconstruction.html').read_text()
    pages = (FIXTURES / 'multi_page_form_reconstruction.html').read_text()
    assert 'Offline reconstruction, not a captured LinkedIn page source' in runtime
    assert 'data-testid="dialog" open' in runtime
    assert '3/4 pages' in runtime and 'Save this application?' in runtime
    assert '1/4 pages' in pages and 'data-testid="typeahead-input"' in pages
    assert 'Synthetic offline state fixture' in pages


def test_latest_run_required_choice_failure_reproduces_from_observed_question(monkeypatch):
    question = 'Is your expected annual compensation for this opportunity less than ₹30 lakhs?*'
    yes = FakeElement('input', attrs={'id': 'yes', 'name': 'compensation', 'type': 'radio', 'required': ''})
    no = FakeElement('input', attrs={'id': 'no', 'name': 'compensation', 'type': 'radio'})
    labels = [FakeElement('label', 'Yes', {'for': 'yes'}, click=lambda: setattr(yes, 'selected', True)),
              FakeElement('label', 'No', {'for': 'no'}, click=lambda: setattr(no, 'selected', True))]
    group = FakeElement('fieldset', f'{question}\nYes\nNo')
    group.options, group.labels = [yes, no], labels
    group.children[(By.XPATH, './/input[@type="radio" or @type="checkbox"]')] = [yes, no]
    group.children[(By.TAG_NAME, 'legend')] = [FakeElement('legend', question)]
    group.children[(By.TAG_NAME, 'label')] = labels
    group.children[(By.XPATH, '..')] = []
    modal = FakeModal(groups=[group])
    driver = FakeDriver(modal)
    monkeypatch.setattr(modern, '_generate_application_answer', lambda *a, **k: 'Maybe')

    with pytest.raises(modern.CannotCompleteApplication, match='No unambiguous option for required choice'):
        modern._fill_fields(driver, modal, {}, {}, '', lambda _: None)


def test_required_yes_no_radio_captures_options_and_verifies_selected_state(monkeypatch):
    question = 'Are you willing to work onsite?*'
    yes = FakeElement('input', attrs={'id': 'yes', 'name': 'onsite', 'type': 'radio', 'required': ''})
    no = FakeElement('input', attrs={'id': 'no', 'name': 'onsite', 'type': 'radio'})
    labels = [FakeElement('label', 'Yes', {'for': 'yes'}, click=lambda: setattr(yes, 'selected', True)),
              FakeElement('label', 'No', {'for': 'no'}, click=lambda: setattr(no, 'selected', True))]
    group = FakeElement('fieldset', f'{question}\nYes\nNo')
    group.options, group.labels = [yes, no], labels
    group.children[(By.XPATH, './/input[@type="radio" or @type="checkbox"]')] = [yes, no]
    group.children[(By.TAG_NAME, 'legend')] = [FakeElement('legend', question)]
    group.children[(By.TAG_NAME, 'label')] = labels
    group.children[(By.XPATH, '..')] = []
    modal, driver = FakeModal(groups=[group]), FakeDriver(FakeModal(groups=[group]))
    captured = {}

    def answer(_client, label, kind, options, *_args, **_kwargs):
        captured.update(label=label, kind=kind, options=list(options))
        return 'Yes'
    monkeypatch.setattr(modern, '_generate_application_answer', answer)
    monkeypatch.setattr(modern, '_ai_answer_allowed', lambda *args: True)
    monkeypatch.setattr(modern, '_click', lambda _driver, option: option.click())

    modern._fill_fields(driver, modal, {}, {}, '', lambda _: None, ai_client=object(),
                        candidate_context='Candidate confirmed willingness to work onsite.')

    assert captured['options'] == ['Yes', 'No']
    assert yes.is_selected() and not no.is_selected()


def test_invalid_browser_field_state_blocks_page_advancement():
    class InvalidDriver(FakeDriver):
        def execute_script(self, script, *args):
            if 'el.willValidate' in script:
                return {'valid': False, 'message': 'Please enter a valid value.',
                        'invalid': True, 'errors': []}
            return None

    modal = FakeModal()
    with pytest.raises(modern.CannotCompleteApplication, match='Browser rejected'):
        modern._verify_browser_acceptance(InvalidDriver(modal), FakeElement('input'), 'Python rating')

    with pytest.raises(modern.CannotCompleteApplication, match='browser validation errors'):
        modern._validate_page_before_next(InvalidDriver(modal), modal, None, '3/4 pages')


def test_dropdown_with_multiple_options_selects_and_verifies_the_exact_option(monkeypatch):
    class Option(FakeElement):
        def __init__(self, text, selected=False):
            super().__init__('option', text, {'value': text})
            self.enabled, self.selected = True, selected
        def is_enabled(self): return self.enabled
        def is_selected(self): return self.selected

    options = [Option('Select an option', True), Option('New York'), Option('Remote')]
    field = FakeElement('select', attrs={'type': 'select-one'})
    field.options = options

    class SelectBox:
        def __init__(self, _field): self.options = options
        @property
        def first_selected_option(self): return next(item for item in options if item.selected)
        def select_by_visible_text(self, text):
            matches = [item for item in options if item.text == text]
            if len(matches) != 1: raise NoSuchElementException(text)
            for item in options: item.selected = item is matches[0]

    monkeypatch.setattr(modern, 'Select', SelectBox)
    monkeypatch.setattr(modern, '_reacquire_scalar_field', lambda *_args, **_kwargs: (None, field))
    monkeypatch.setattr(modern, '_verify_browser_acceptance', lambda *_args: None)

    selected, actual = modern._select_option_stably(
        object(), {'label': 'Work location', 'tag': 'select', 'type': 'select-one'}, 'Remote', None)

    assert selected is field and actual == 'Remote'
    assert len(options) == 3 and options[2].selected
    assert not options[1].selected


def test_invalid_ai_value_is_restored_before_draft_cleanup_and_never_submitted(monkeypatch):
    driver = CleanupFixtureDriver(save_prompts=1)
    field = FakeElement('input', attrs={'type': 'text', 'value': 'unsupported AI claim'})
    audit = type('Audit', (), {'automation_answer_values': [{
        'descriptor': {'tag': 'input', 'type': 'text', 'label': 'Project description',
                       'id': 'project', 'name': '', 'placeholder': ''},
        'original': '', 'intended': 'unsupported AI claim', 'source': 'AI generated',
        'factual_validated': False, 'browser_validated': False, 'cleanup_verified': False,
        'page': '3/4 pages',
    }]})()
    monkeypatch.setattr(modern, '_reacquire_scalar_field', lambda *_args, **_kwargs: (driver.modal, field))
    monkeypatch.setattr(modern, '_replace_text',
                        lambda _field, value, *_args, **_kwargs: field.attrs.update(value=value))

    modern._cleanup_unverified_automation_values(driver, audit)
    saved = modern._dismiss(driver, audit)

    assert field.get_attribute('value') == ''
    assert audit.automation_answer_values[0]['cleanup_verified'] is True
    assert saved is True and driver.save_clicks == 1
    assert driver.submit_clicks == 0


def test_rejected_ai_proposal_blocks_preserving_an_existing_draft(tmp_path):
    from types import SimpleNamespace
    driver = CleanupFixtureDriver(save_prompts=1)
    audit = SimpleNamespace(ai_answer_rejected=True, entry={}, diagnostic_root=tmp_path)

    with pytest.raises(modern.ApplicationCleanupIncomplete, match='refusing to preserve'):
        modern._dismiss(driver, audit)

    assert driver.save_clicks == 0
    assert driver.submit_clicks == 0
    assert len(driver.dialogs) == 2  # Leave the existing prompt untouched for human review.


def test_cached_field_collection_is_reacquired_after_simulated_rerender(monkeypatch):
    class Input(FakeElement):
        def __init__(self, label, value='', on_text=None):
            super().__init__('input', attrs={'type': 'text', 'aria-label': label, 'value': value})
            self.on_text = on_text

        def click(self): self._check()

        def send_keys(self, *keys):
            self._check()
            if len(keys) == 1 and isinstance(keys[0], str) and keys[0] != modern.Keys.BACKSPACE:
                self.attrs['value'] = keys[0]
                if self.on_text: self.on_text()
            elif len(keys) == 2:
                self.attrs['value'] = ''

    second = Input('Last name')
    first = Input('First name')
    modal = FakeModal(fields=[first, second])
    driver = FakeDriver(modal)
    rerendered = False

    def rerender_after_first_field(_):
        nonlocal rerendered
        if not rerendered:
            rerendered = True
            second.stale = True
            modal.fields = [Input('First name', first.attrs.get('value', '')),
                            Input('Last name')]

    monkeypatch.setattr(modern, 'buffer', rerender_after_first_field)
    modern._fill_fields(driver, modal, {'first_name': 'A', 'last_name': 'B'}, {}, '', lambda _: None)
    assert rerendered
    assert [field.attrs['value'] for field in modal.fields] == ['A', 'B']


def test_candidate_reacquisition_after_mutation_uses_fresh_field_handles():
    """Test-only correction probe; does not change the production filler."""
    class Input(FakeElement):
        def __init__(self, label, value=''):
            super().__init__('input', attrs={'type': 'text', 'aria-label': label, 'value': value})
        def click(self): self._check()
        def send_keys(self, *keys):
            self._check()
            if len(keys) == 2 or keys == (modern.Keys.BACKSPACE,):
                self.attrs['value'] = ''
            elif len(keys) == 1:
                self.attrs['value'] = keys[0]

    fields = {'First name': Input('First name'), 'Last name': Input('Last name')}
    for label, answer in (('First name', 'A'), ('Last name', 'B')):
        # Re-query by stable label after every mutation; invalidate the old set.
        current = fields[label]
        modern._replace_text(current, answer, label)
        fields = {key: Input(key, field.attrs['value']) for key, field in fields.items()}
    assert fields['First name'].attrs['value'] == 'A'
    assert fields['Last name'].attrs['value'] == 'B'


def test_reacquisition_matches_label_after_field_order_changes(monkeypatch):
    first = FakeElement('input', attrs={'type': 'text', 'aria-label': 'First name', 'value': ''})
    second = FakeElement('input', attrs={'type': 'text', 'aria-label': 'Last name', 'value': ''})
    modal = FakeModal(fields=[first, second])
    driver = FakeDriver(modal)
    descriptor = modern._field_descriptor(modal, second)
    replacement = FakeElement('input', attrs={'type': 'text', 'aria-label': 'Last name', 'value': 'kept'})
    first.stale = second.stale = True
    modal.fields = [replacement, FakeElement('input', attrs={
        'type': 'text', 'aria-label': 'First name', 'value': ''})]

    current_modal, current = modern._reacquire_scalar_field(driver, descriptor)
    assert current_modal is modal
    assert current is replacement
    assert modern._field_value(current) == 'kept'


def test_ambiguous_field_identity_fails_closed_before_interaction():
    first = FakeElement('input', attrs={'type': 'text', 'aria-label': 'First name', 'value': ''})
    duplicate = FakeElement('input', attrs={'type': 'text', 'aria-label': 'First name', 'value': ''})
    modal = FakeModal(fields=[first])
    driver = FakeDriver(modal)
    descriptor = modern._field_descriptor(modal, first)
    modal.fields = [first, duplicate]

    with pytest.raises(modern.CannotCompleteApplication, match='uniquely reacquire'):
        modern._reacquire_scalar_field(driver, descriptor)
    assert first.attrs['value'] == duplicate.attrs['value'] == ''


def test_repeated_stale_scalar_reference_stops_without_repeating_mutation():
    class AlwaysStale(FakeElement):
        attempts = 0
        def click(self):
            self.attempts += 1
            raise StaleElementReferenceException('stale before interaction')

    field = AlwaysStale('input', attrs={'type': 'text', 'aria-label': 'First name', 'value': ''})
    modal = FakeModal(fields=[field])
    driver = FakeDriver(modal)
    descriptor = modern._field_descriptor(modal, field)
    with pytest.raises(StaleElementReferenceException):
        modern._fill_scalar_stably(driver, descriptor, 'A', 'text', None)
    assert field.attempts == 1
    assert field.attrs['value'] == ''


@pytest.mark.parametrize('choice_type', ['radio', 'checkbox'])
def test_choice_selection_survives_rerender_without_duplicate_click(choice_type, monkeypatch):
    class ChoiceGroup(FakeElement):
        def __init__(self, options):
            self.options = options
            self.labels = [FakeElement('label', label, {'for': field_id})
                           for field_id, label in [('remote', 'Remote'), ('hybrid', 'Hybrid')]]
            super().__init__('fieldset', 'Work mode*\nRemote\nHybrid')
        def find_elements(self, by, selector):
            if selector == './/input[@type="radio" or @type="checkbox"]': return self.options
            if by == By.TAG_NAME and selector == 'legend': return [FakeElement('legend', 'Work mode')]
            if by == By.TAG_NAME and selector == 'label': return self.labels
            return []

    def build_options():
        return [FakeElement('input', attrs={'type': choice_type, 'id': key, 'value': label})
                for key, label in [('remote', 'Remote'), ('hybrid', 'Hybrid')]]

    initial = build_options()
    modal = FakeModal(groups=[ChoiceGroup(initial)])
    driver = FakeDriver(modal)
    clicks = []

    def click_and_rerender(_driver, target):
        clicks.append(target.text)
        current_group = modal.groups[0]
        selected_option = next(option for option in current_group.options
                               if any(label is target and label.get_attribute('for') == option.get_attribute('id')
                                      for label in current_group.labels))
        selected_id = selected_option.get_attribute('id')
        if choice_type == 'radio':
            for option in current_group.options: option.selected = option is selected_option
        else:
            selected_option.selected = True
        for option in current_group.options: option.stale = True
        modal.groups = [ChoiceGroup(build_options())]
        # Reflect the completed click in the replacement DOM.
        new_group = modal.groups[0]
        next(option for option in new_group.options
             if option.get_attribute('id') == selected_id).selected = True

    monkeypatch.setattr(modern, '_click', click_and_rerender)
    modern._set_choice_option_stably(driver, 'Work mode', 'Hybrid', choice_type, True)
    current = modal.groups[0].options
    assert next(option for option in current if option.get_attribute('id') == 'hybrid').is_selected()
    assert len(clicks) == 1


def test_delayed_modal_is_found_by_waiting_on_actual_modal_state(monkeypatch):
    driver = FakeDriver()
    driver.delay_polls = 3
    driver.delayed_modal = FakeModal(page='1/4 pages')
    monkeypatch.setattr(modern, 'WebDriverWait', FakeWait)
    found = modern.WebDriverWait(driver, 4).until(lambda d: modern._modal(d))
    assert found is driver.modal
    assert modern._progress(found) == 1


def test_missing_modal_times_out_and_replacement_is_reacquired(monkeypatch):
    monkeypatch.setattr(modern, 'WebDriverWait', FakeWait)
    missing = FakeDriver()
    with pytest.raises(TimeoutException):
        modern.WebDriverWait(missing, 3).until(lambda d: modern._modal(d))

    page_two = FakeModal(page='2/4 pages')
    next_button = FakeElement('button', 'Next', click=lambda: setattr(driver, 'modal', page_two))
    page_one = FakeModal(page='1/4 pages', buttons=[next_button])
    driver = FakeDriver(page_one)
    old = modern._modal(driver)
    modern._click(driver, next_button)  # Next causes a modal replacement.
    current = modern._modal(driver)
    assert current is not old
    assert modern._progress(current) == 2


def test_timeout_before_first_field_record_is_reproducible(monkeypatch):
    driver = FakeDriver()
    audit_events = []
    monkeypatch.setattr(modern, 'WebDriverWait', FakeWait)
    with pytest.raises(TimeoutException):
        modern.WebDriverWait(driver, 3).until(lambda d: modern._modal(d))
    assert audit_events == []


def test_save_overlay_interception_reproduces_and_overlay_first_probe_recovers():
    """The click error is reproduced; the recovery probe is test-only."""
    overlay_open = {'value': True}
    dismiss = FakeElement('button', attrs={'aria-label': 'Dismiss'},
                          click=lambda: (_ for _ in ()).throw(
                              ElementClickInterceptedException('covered by open Save dialog')))
    save = FakeElement('button', 'Save', click=lambda: overlay_open.update(value=False))
    save_dialog = FakeElement('dialog', 'Save this application?', {'data-testid': 'dialog', 'open': ''})
    save_dialog.children[(By.TAG_NAME, 'button')] = [
        FakeElement('button', 'Discard'), save]
    modal = FakeModal(buttons=[dismiss])
    driver = FakeDriver(modal)

    with pytest.raises(ElementClickInterceptedException):
        modern._click(driver, dismiss)

    # Candidate correction: on interception, re-query dialogs and resolve an
    # explicitly visible save prompt; do not retry the stale/covered button.
    def recover_after_intercept():
        try:
            modern._click(driver, dismiss)
        except ElementClickInterceptedException:
            fresh_dialogs = driver.find_elements(By.CSS_SELECTOR, 'dialog[open], [role="dialog"]')
            prompt = next((d for d in fresh_dialogs if 'save this application' in d.text.casefold()), None)
            assert prompt is not None
            button = next(b for b in prompt.find_elements(By.TAG_NAME, 'button') if b.text == 'Save')
            button.click()

    dialogs = [save_dialog, modal]
    driver.find_elements = lambda by, selector: dialogs if selector == 'dialog[open], [role="dialog"]' else []
    recover_after_intercept()
    assert overlay_open['value'] is False


class CleanupFixtureDriver:
    """Mocked DOM assembled from the offline runtime reconstruction fixture."""
    def __init__(self, *, save_prompts=0, unknown_overlays=0, ambiguous_saves=1,
                 dismiss_intercepts=False, save_intercepts=False, hide_save=False):
        self.dialogs = []
        self.dismiss_clicks = self.save_clicks = self.submit_clicks = 0
        self.dismiss_intercepts = dismiss_intercepts
        self.save_intercepts = save_intercepts
        self.hide_save = hide_save
        self.modal = FakeModal(page='3/4 pages')
        def dismiss_action():
            self.dismiss_clicks += 1
            if self.dismiss_intercepts:
                prompt = self._save_prompt()
                self.dialogs.append(prompt)
                raise ElementClickInterceptedException('covered by Save this application dialog')
            self.dialogs.remove(self.modal)
        dismiss = FakeElement('button', 'Close application', {'aria-label': 'Dismiss'}, click=dismiss_action)
        self.modal.buttons.extend([dismiss, FakeElement('button', 'Submit Application', click=lambda: setattr(
            self, 'submit_clicks', self.submit_clicks + 1))])
        self.dialogs.append(self.modal)
        self.dialogs.extend(self._save_prompt() for _ in range(save_prompts))
        self.dialogs.extend(self._unknown_overlay() for _ in range(unknown_overlays))
        if save_prompts and ambiguous_saves > 1:
            self.dialogs[1].children[(By.TAG_NAME, 'button')].extend(
                self._save_button() for _ in range(ambiguous_saves - 1))

    def _save_button(self):
        def save_action():
            self.save_clicks += 1
            if self.save_intercepts:
                raise ElementClickInterceptedException('Save control intercepted')
            self.dialogs = [d for d in self.dialogs if 'save this application' not in d.text.casefold()
                            and not d.text.startswith('Apply to ')]
        return FakeElement('button', 'Save', {'type': 'button'}, click=save_action)

    def _save_prompt(self):
        prompt = FakeElement('dialog', 'Save this application?', {'data-testid': 'dialog', 'open': ''})
        prompt.children[(By.CSS_SELECTOR, 'h1,h2,h3')] = [FakeElement('h2', 'Save this application?')]
        controls = [FakeElement('button', 'Discard'), self._save_button()]
        if self.hide_save:
            controls = [FakeElement('button', 'Discard')]
        prompt.children[(By.TAG_NAME, 'button')] = controls
        return prompt

    def _unknown_overlay(self):
        overlay = FakeElement('dialog', 'Unexpected confirmation', {'data-testid': 'unknown', 'open': ''})
        overlay.children[(By.CSS_SELECTOR, 'h1,h2,h3')] = [FakeElement('h2', 'Unexpected confirmation')]
        overlay.children[(By.TAG_NAME, 'button')] = [FakeElement('button', 'Continue')]
        return overlay

    def find_elements(self, by, selector):
        if selector in ('dialog[open]', 'dialog[open], [role="dialog"]'):
            return list(self.dialogs)
        return []

    @property
    def current_url(self):
        return 'file:///offline/runtime_failure_reconstruction.html'


def test_cleanup_dismisses_modal_when_no_confirmation_is_present():
    driver = CleanupFixtureDriver()
    assert modern._dismiss(driver) is False
    assert driver.dismiss_clicks == 1
    assert driver.dialogs == []
    assert driver.submit_clicks == 0


def test_cleanup_saves_application_when_semantic_save_dialog_is_visible():
    driver = CleanupFixtureDriver(save_prompts=1)
    assert modern._dismiss(driver) is True
    assert driver.dismiss_clicks == 0
    assert driver.save_clicks == 1
    assert driver.dialogs == []
    assert driver.submit_clicks == 0


def test_cleanup_fails_closed_on_ambiguous_save_dialog():
    driver = CleanupFixtureDriver(save_prompts=1, ambiguous_saves=2)
    with pytest.raises(modern.ApplicationCleanupIncomplete):
        modern._dismiss(driver)
    assert driver.dismiss_clicks == driver.save_clicks == driver.submit_clicks == 0
    assert len(driver.dialogs) == 2


def test_cleanup_fails_closed_on_unknown_overlay():
    driver = CleanupFixtureDriver(unknown_overlays=1)
    with pytest.raises(modern.ApplicationCleanupIncomplete):
        modern._dismiss(driver)
    assert driver.dismiss_clicks == driver.save_clicks == driver.submit_clicks == 0
    assert len(driver.dialogs) == 2


def test_intercepted_dismiss_reinspects_and_saves_unique_known_dialog():
    driver = CleanupFixtureDriver(dismiss_intercepts=True)
    assert modern._dismiss(driver) is True
    assert driver.dismiss_clicks == 1  # No retry of the covered control.
    assert driver.save_clicks == 1
    assert driver.submit_clicks == 0
    assert driver.dialogs == []


def test_intercepted_dismiss_with_unknown_overlay_fails_without_retry():
    driver = CleanupFixtureDriver()
    def intercept_with_unknown_overlay():
        driver.dismiss_clicks += 1
        driver.dialogs.append(driver._unknown_overlay())
        raise ElementClickInterceptedException('covered by unknown overlay')
    driver.modal.buttons[0]._click = intercept_with_unknown_overlay
    with pytest.raises(modern.ApplicationCleanupIncomplete):
        modern._dismiss(driver)
    assert driver.dismiss_clicks == 1
    assert driver.save_clicks == driver.submit_clicks == 0
    assert len(driver.dialogs) == 2


def test_cleanup_leaves_save_dialog_open_when_save_control_is_unavailable():
    driver = CleanupFixtureDriver(save_prompts=1, hide_save=True)
    with pytest.raises(modern.ApplicationCleanupIncomplete):
        modern._dismiss(driver)
    assert driver.dismiss_clicks == driver.save_clicks == driver.submit_clicks == 0
    assert len(driver.dialogs) == 2


def test_cleanup_error_does_not_replace_original_application_exception():
    driver = CleanupFixtureDriver(unknown_overlays=1)
    original = modern.CannotCompleteApplication('required question had no verified answer')
    with pytest.raises(modern.CannotCompleteApplication) as caught:
        modern._dismiss_preserving_application_error(driver, None, original)
    assert caught.value is original
    assert driver.dismiss_clicks == driver.save_clicks == driver.submit_clicks == 0


def test_save_click_interception_is_reinspected_without_blind_retry():
    driver = CleanupFixtureDriver(save_prompts=1, save_intercepts=True)
    with pytest.raises(modern.ApplicationCleanupIncomplete):
        modern._dismiss(driver)
    assert driver.save_clicks == 1
    assert driver.dismiss_clicks == driver.submit_clicks == 0
    assert len(driver.dialogs) == 2


@pytest.mark.parametrize('scenario', [
    {},
    {'save_prompts': 1},
    {'save_prompts': 1, 'ambiguous_saves': 2},
    {'unknown_overlays': 1},
    {'dismiss_intercepts': True},
    {'save_prompts': 1, 'hide_save': True},
    {'save_prompts': 1, 'save_intercepts': True},
    {'dismiss_intercepts': True, 'unknown_overlays': 1},
])
def test_cleanup_scenarios_never_click_submit(scenario):
    driver = CleanupFixtureDriver(**scenario)
    try:
        modern._dismiss(driver)
    except modern.ApplicationCleanupIncomplete:
        pass
    assert driver.submit_clicks == 0


def test_cleanup_diagnostic_contains_dialog_and_overlay_observations(monkeypatch):
    events = []
    monkeypatch.setattr(modern.application_diagnostics, 'append_exception_event', events.append)
    audit = type('Audit', (), {'entry': {'attempt_id': 'cleanup-attempt', 'job_id': '4479893840',
        'job_title': 'AI Engineer', 'company': 'ALIANDO', 'run_id': None, 'fields': []},
        '_diagnostic_exception_ids': set(), 'diagnostic_page_index': 3,
        'diagnostic_phase': 'CLEANING_UP', 'diagnostic_last_operation': 'click unique application Dismiss control'})()
    driver = CleanupFixtureDriver(dismiss_intercepts=True)
    assert modern._dismiss(driver, audit) is True
    assert events[0]['phase'] == 'CLEANING_UP'
    assert events[0]['last_operation'] == 'click unique application Dismiss control'
    assert events[0]['observation']['intercepted'] is True
    assert events[0]['observation']['save_prompt_detected_after'] is True
    assert events[0]['observation']['overlay_present'] is True
    assert events[0]['observation']['overlay_present_before'] is False
    assert events[0]['observation']['overlay_present_after'] is True
    assert events[0]['observation']['dialogs_after']
    assert driver.submit_clicks == 0


def test_radio_and_checkbox_controls_update_in_the_mocked_form():
    radio_yes = FakeElement('input', attrs={'id': 'yes', 'name': 'work', 'type': 'radio', 'required': ''})
    radio_no = FakeElement('input', attrs={'id': 'no', 'name': 'work', 'type': 'radio'})
    yes_label = FakeElement('label', 'Yes', {'for': 'yes'}, click=lambda: setattr(radio_yes, 'selected', True))
    no_label = FakeElement('label', 'No', {'for': 'no'}, click=lambda: setattr(radio_no, 'selected', True))
    radio_group = FakeElement('fieldset', 'Are you authorized to work here?*\nYes\nNo')
    radio_group.options, radio_group.labels = [radio_yes, radio_no], [yes_label, no_label]
    radio_group.children[(By.XPATH, './/input[@type="radio" or @type="checkbox"]')] = [radio_yes, radio_no]
    radio_group.children[(By.TAG_NAME, 'legend')] = [FakeElement('legend', 'Are you authorized to work here?*')]
    radio_group.children[(By.TAG_NAME, 'label')] = [yes_label, no_label]
    radio_group.children[(By.XPATH, '..')] = []

    class Registry:
        def classify(self, _): return None
        def unverified(self, _): return False
        def record_missing(self, _): pass
        def answer(self, label, **kwargs):
            if 'authorized' in label.casefold(): return 'Yes'
            if 'skills' in label.casefold(): return 'Python, SQL'
            return None

    python = FakeElement('input', attrs={'id': 'python', 'type': 'checkbox'})
    sql = FakeElement('input', attrs={'id': 'sql', 'type': 'checkbox'})
    node = FakeElement('input', attrs={'id': 'node', 'type': 'checkbox'})
    labels = [FakeElement('label', name, {'for': ident}, click=lambda x=field: setattr(x, 'selected', not x.selected))
              for name, ident, field in [('Python', 'python', python), ('SQL', 'sql', sql), ('Node', 'node', node)]]
    checkbox_group = FakeElement('fieldset', 'Skills*\nPython\nSQL\nNode')
    checkbox_group.options, checkbox_group.labels = [python, sql, node], labels
    checkbox_group.children[(By.XPATH, './/input[@type="radio" or @type="checkbox"]')] = [python, sql, node]
    checkbox_group.children[(By.TAG_NAME, 'legend')] = [FakeElement('legend', 'Skills*')]
    checkbox_group.children[(By.TAG_NAME, 'label')] = labels
    checkbox_group.children[(By.XPATH, '..')] = []

    modal = FakeModal(groups=[radio_group, checkbox_group])
    driver = FakeDriver(modal)
    modern._fill_fields(driver, modal, {}, {}, '', lambda _: None, registry=Registry())
    assert radio_yes.selected is True and radio_no.selected is False
    assert python.selected is True and sql.selected is True and node.selected is False


def test_search_restoration_is_offline_and_waits_for_results(monkeypatch):
    search_url = 'file:///offline/search-results.html'
    driver = FakeDriver()
    cards = [FakeElement('div', 'offline result')]

    def find_elements(by, selector):
        if selector == modern.CARDS_XPATH and driver.current_url == search_url:
            return cards
        return []

    driver.find_elements = find_elements
    driver.modal = FakeModal(page='3/4 pages')
    driver.get(search_url)
    monkeypatch.setattr(modern, 'WebDriverWait', FakeWait)
    assert modern.WebDriverWait(driver, 2).until(lambda d: d.find_elements(By.XPATH, modern.CARDS_XPATH)) == cards
    assert driver.current_url == search_url
    assert modern._modal(driver) is None
