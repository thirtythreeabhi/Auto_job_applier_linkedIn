"""Support LinkedIn's current AI-powered job search and Easy Apply screens.

The older LinkedIn page and its form markup are still handled by runAiBot.py.
This adapter uses accessible names rather than LinkedIn's generated CSS classes.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from selenium.common.exceptions import (NoSuchElementException, StaleElementReferenceException,
                                        TimeoutException, WebDriverException)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select, WebDriverWait
from modules.application_audit import ApplicationAudit, role_key

CARDS_XPATH = "//div[@role='button'][.//button[starts-with(@aria-label,'Dismiss ') and contains(@aria-label,' job')]]"
APPLY_XPATH = (
    "//button[not(ancestor::nav) and ("
    "contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'easy apply')"
    " or contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'continue applying')"
    " or normalize-space(.)='Easy Apply'"
    " or .//span[normalize-space(.)='Easy Apply']"
    " or normalize-space(.)='Continue applying'"
    ")]"
    " | //a[normalize-space(.)='Continue' and contains(@href,'/jobs/view/')]"
)


def _selected_job_id_from_url(url):
    """The URL identifies the selected details panel, unlike links in result cards."""
    ids = parse_qs(urlparse(url).query).get('currentJobId', [])
    return ids[0] if len(ids) == 1 and re.fullmatch(r'\d+', ids[0]) else None


def _resolved_selected_job_id(driver, hinted_id, previous_id, was_selected):
    current_id = _selected_job_id_from_url(driver.current_url)
    if not current_id or (hinted_id and current_id != hinted_id):
        return False
    if not hinted_id and not was_selected and current_id == previous_id:
        return False
    return current_id


def _visible_apply_buttons(driver):
    return [button for button in driver.find_elements(By.XPATH, APPLY_XPATH)
            if button.is_displayed() and button.is_enabled()]


class CannotCompleteApplication(Exception):
    """The current application needs information or controls we cannot safely handle."""


class SubmissionUncertain(Exception):
    """A submission was attempted, but LinkedIn did not confirm the outcome."""


def is_modern_page(driver):
    return '/jobs/search-results/' in driver.current_url


def _modal(driver):
    for element in driver.find_elements(By.CSS_SELECTOR, 'dialog[open]'):
        try:
            headers = element.find_elements(By.CSS_SELECTOR, 'header h2')
            if element.is_displayed() and headers and headers[0].text.strip().lower().startswith('apply to '):
                return element
        except StaleElementReferenceException:
            continue
    return None


def _click(driver, element):
    driver.execute_script("arguments[0].scrollIntoView({block:'center',inline:'nearest'})", element)
    element.click()


def _click_text(driver, modal, text):
    buttons = [b for b in modal.find_elements(By.TAG_NAME, 'button')
               if b.text.strip().casefold() == text.casefold() and b.is_displayed()]
    if len(buttons) != 1:
        raise CannotCompleteApplication(f'Could not uniquely find the {text} button.')
    _click(driver, buttons[0])


def _progress(modal):
    match = re.search(r'\b(\d+)\s*/\s*(\d+)\s+pages\b', modal.text)
    return int(match.group(1)) if match else None


def _confirmation_matches(text, company):
    text = text.casefold()
    expected = re.sub(r'\W+', '', company.casefold())
    return bool(expected and ('application was sent' in text or 'application submitted' in text)
                and expected in re.sub(r'\W+', '', text))


def _job_status_confirms(text, company):
    normalized = re.sub(r'\W+', '', text.casefold())
    expected = re.sub(r'\W+', '', company.casefold())
    return bool(expected and expected != 'unknown' and 'application status' in text.casefold()
                and 'application submitted' in text.casefold() and expected in normalized)


def _field_label(modal, field):
    label = (field.get_attribute('aria-label') or '').strip()
    field_id = field.get_attribute('id')
    if not label and field_id:
        for candidate in modal.find_elements(By.TAG_NAME, 'label'):
            if candidate.get_attribute('for') == field_id:
                label = candidate.text.strip()
                break
    return label or (field.get_attribute('placeholder') or '').strip()


def _answer_for(label, profile, questions, email, registry=None):
    """Return only answers explicitly saved by the user; never guess a skill or attestation."""
    if registry is not None:
        return registry.answer(label)
    name = re.sub(r'\s+', ' ', label.lower()).strip(' *?:')
    # Classify work authorization before address fields: "United States" is not
    # a request for the applicant's state of residence.
    if any(x in name for x in ('authorized to work', 'authorised to work', 'work authorization',
                               'work authorisation', 'employment eligibility', 'right to work')):
        return questions.get('legally_authorized')
    if 'sponsorship' in name or 'require a visa' in name: return questions.get('require_visa')
    if 'additional' in name and 'month' in name and 'experience' in name:
        return questions.get('additional_experience_months')
    compact = re.sub(r'[^a-z0-9]+', '', name)
    if 'year' in name and 'experience' in name:
        if re.search(r'\b(?:and|or)\b|[,/]', name): return None
        if 'nodejs' in compact: return questions.get('nodejs_experience_years')
        if 'postgresql' in compact or 'postgres' in compact: return questions.get('postgresql_experience_years')
        if 'fullstack' in compact: return questions.get('fullstack_experience_years')
        if ('reactjs' in compact or re.search(r'\breact\b', name)) and 'native' not in name:
            return questions.get('reactjs_experience_years')
    if 'first name' in name: return profile.get('first_name')
    if 'last name' in name or 'surname' in name:
        return ' '.join(str(profile.get(k, '')).strip() for k in ('middle_name', 'last_name')).strip()
    if 'email' in name: return email
    if 'phone' in name or 'mobile' in name: return profile.get('phone_number')
    if 'linkedin' in name: return questions.get('linkedIn')
    if 'portfolio' in name or 'website' in name: return questions.get('website')
    if 'postal' in name or 'zip' in name: return profile.get('zipcode')
    if 'street' in name or 'address' in name: return profile.get('street')
    if 'city' in name or 'location (city)' in name: return profile.get('current_city')
    if 'state' in name or 'province' in name: return profile.get('state')
    if name == 'country' or 'country of residence' in name: return profile.get('country')
    if 'notice period' in name and questions.get('notice_period') is not None:
        days = int(questions['notice_period'])
        if 'week' in name: return f'{days / 7:g}'
        if 'month' in name: return f'{days / 30:g}'
        return str(days)
    if ('expected' in name or 'desired' in name) and ('salary' in name or 'ctc' in name):
        value = questions.get('desired_salary')
        if value is None: return None
        if 'lakh' in name or 'lpa' in name: return f'{float(value) / 100000:g}'
        if 'month' in name: return f'{float(value) / 12:g}'
        return value
    if ('current' in name or 'present' in name) and ('salary' in name or 'ctc' in name):
        value = questions.get('current_ctc')
        if value is None: return None
        if 'lakh' in name or 'lpa' in name: return f'{float(value) / 100000:g}'
        if 'month' in name: return f'{float(value) / 12:g}'
        return value
    if 'current job title' in name: return None  # no explicit title field in the saved profile
    if 'current company' in name or 'recent employer' in name: return questions.get('recent_employer')
    if 'cover letter' in name: return questions.get('cover_letter')
    if 'summary' in name: return questions.get('linkedin_summary')
    if 'gender' in name: return profile.get('gender')
    if 'disability' in name: return profile.get('disability_status')
    if 'veteran' in name: return profile.get('veteran_status')
    # A total experience answer must not be reused for a particular technology.
    total_years = re.search(r'\byears?\s+of\s+(?:(?:professional|work|overall|total)\s+){0,2}experience\b', name)
    if (name in ('years of experience', 'total work experience') or total_years) and not (
        total_years and re.search(r'\b(with|using|in|on)\b', name[total_years.end():])
    ):
        return questions.get('years_of_experience')
    return None


def _fill_city_typeahead(driver, modal, field, profile):
    city = str(profile.get('current_city') or '').strip()
    if not city:
        raise CannotCompleteApplication('The required city is not saved in your profile.')
    existing = (field.get_attribute('value') or '').strip()
    if existing and city.casefold() in existing.casefold():
        return
    if existing: field.clear()
    field.send_keys(city)
    def city_choice(_):
        m = _modal(driver)
        if not m: return False
        choices = [e for e in m.find_elements(By.XPATH, './/*[@role="option"]') if e.is_displayed()]
        country = str(profile.get('country') or '').lower()
        return next((e for e in choices if city.lower() in e.text.lower()
                     and (not country or country in e.text.lower())), False)
    try:
        choice = WebDriverWait(driver, 8).until(city_choice)
        _click(driver, choice)
    except TimeoutException as error:
        raise CannotCompleteApplication(f'Could not select a LinkedIn location for {city}.') from error


def _experience_option(options, answer):
    if not str(answer).strip().isdigit(): return None
    years = int(str(answer).strip())
    exact = [o for o in options if re.fullmatch(rf'\s*{years}\s*(?:years?|yrs?|months?|mos?)\s*', o.text, re.I)]
    if len(exact) == 1: return exact[0]
    ranges = []
    for option in options:
        bounds = re.fullmatch(r'\s*(\d+)\s*(?:-|–|to)\s*(\d+)\s*(?:years?)?\s*', option.text, re.I)
        if bounds and int(bounds.group(1)) <= years <= int(bounds.group(2)):
            ranges.append(option)
    return ranges[0] if len(ranges) == 1 else None


def _matching_choice(options, answer, experience=False):
    """Match one option without choosing an arbitrary or ambiguous answer."""
    value = str(answer).strip().casefold()
    matches = [o for o in options if o.text.strip().casefold() == value]
    if len(matches) == 1: return matches[0]
    if value in ('yes', 'no'):
        matches = [o for o in options if re.match(r'^' + value + r'(?:\b|[,;])', o.text.strip(), re.I)]
        if len(matches) == 1: return matches[0]
    if experience and value.isdigit(): return _experience_option(options, value)
    return None


def _replace_text(field, answer, label, driver=None):
    """Replace controlled input text and verify what the page actually retained."""
    expected = str(answer)
    field.click()
    field.send_keys(Keys.CONTROL, 'a')
    field.send_keys(Keys.BACKSPACE)
    field.send_keys(expected)
    actual = (field.get_attribute('value') or '').strip()
    if actual.casefold() != expected.strip().casefold() and driver is not None:
        # Controlled inputs may restore their previous value after keyboard
        # clearing. Use the native setter and notify the page, then recheck.
        driver.execute_script('''
            const el = arguments[0], value = arguments[1];
            const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(el, value);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        ''', field, expected)
        actual = (field.get_attribute('value') or '').strip()
    if actual.casefold() != expected.strip().casefold():
        raise CannotCompleteApplication(f'Could not verify the entered answer for: {label}.')


def _replace_date(field, answer, label):
    """Enter an ISO date and verify the browser's normalized date value."""
    match = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})', str(answer))
    if not match: raise CannotCompleteApplication('A verified date is required for: ' + label)
    year, month, day = match.groups()
    field.click()
    field.send_keys(Keys.CONTROL, 'a')
    field.send_keys(Keys.BACKSPACE)
    field.send_keys(month + day + year)
    if (field.get_attribute('value') or '').strip() != str(answer):
        raise CannotCompleteApplication('Could not verify the entered date for: ' + label)


def _verify_review_name(text, registry):
    if registry is None: return
    surname = str(registry.value('last_name') or '').strip()
    if surname and re.search(r'(?:' + re.escape(surname) + r'[\s,;]*){2,}', text, re.I):
        raise CannotCompleteApplication('The review contains a repeated surname; the application was not submitted.')


def _clear_optional_company_follow(driver, modal):
    for field in modal.find_elements(By.CSS_SELECTOR, 'input[type="checkbox"]'):
        label = _field_label(modal, field).casefold()
        if label.startswith('follow ') and field.is_selected():
            field_id = field.get_attribute('id')
            labels = [x for x in modal.find_elements(By.TAG_NAME, 'label') if x.get_attribute('for') == field_id] if field_id else []
            _click(driver, labels[0] if labels else field)
            if field.is_selected():
                raise CannotCompleteApplication('Could not clear the optional company follow setting.')


def _verify_name_fields(modal, registry, audit=None, page=None):
    if registry is None: return
    fields = modal.find_elements(By.XPATH, './/input|.//textarea')
    separate_middle = any('middle name' in _field_label(modal, field).casefold() for field in fields)
    for field in fields:
        if not field.is_displayed(): continue
        label = _field_label(modal, field)
        key = registry.classify(label)
        if key not in ('first_name', 'middle_name', 'last_name', 'full_name'): continue
        expected = str(registry.value(key) or '').strip()
        if key == 'last_name' and not separate_middle:
            expected = ' '.join(x for x in (str(registry.value('middle_name') or '').strip(), expected) if x)
        actual = (field.get_attribute('value') or '').strip()
        if not expected or actual.casefold() != expected.casefold():
            raise CannotCompleteApplication('The name field changed or does not match your confirmed name: ' + label)
        if audit: audit.field(label, actual, 'verified after form update', page)


def _fill_fields(driver, modal, profile, questions, email, log, registry=None, audit=None, page=None, job_id=None):
    # A LinkedIn city typeahead does not always carry HTML's required attribute.
    for field in modal.find_elements(By.CSS_SELECTOR, 'input[data-testid="typeahead-input"]'):
        if 'city' in (field.get_attribute('placeholder') or '').lower():
            _fill_city_typeahead(driver, modal, field, profile)

    fields = modal.find_elements(By.XPATH, './/input|.//select|.//textarea')
    has_middle_name_field = any('middle name' in _field_label(modal, f).casefold() for f in fields)
    for field in fields:
        kind = (field.get_attribute('type') or field.tag_name).lower()
        if kind in ('hidden', 'file', 'radio', 'checkbox'):
            continue
        label = _field_label(modal, field)
        required = bool(field.get_attribute('required')) or label.rstrip().endswith('*')
        if field.tag_name == 'select':
            selected = Select(field).first_selected_option.text.strip()
            has_value = bool(selected and selected.lower() not in ('select an option', 'select', 'choose an option'))
            existing = selected if has_value else ''
        else:
            existing = (field.get_attribute('value') or '').strip()
            has_value = bool(existing)
        key = registry.classify(label) if registry is not None else None
        if registry is not None and registry.unverified(label) and (required or has_value):
            registry.record_missing(label)
            raise CannotCompleteApplication(f'Unverified application question: {label or "unlabelled field"}.')
        answer = _answer_for(label, profile, questions, email, registry)
        if kind == 'number' and key == 'ai_development_duration' and answer == '1 year':
            answer = '1'
        if registry is not None and key == 'last_name' and answer is not None and not has_middle_name_field:
            # LinkedIn's two-name forms need the middle and family names together.
            middle = str(registry.value('middle_name') or '').strip()
            answer = ' '.join(x for x in (middle, str(answer).strip()) if x)
        if registry is not None and required and answer is None:
            registry.record_missing(label or 'Unlabelled required field')
            raise CannotCompleteApplication(f'No verified answer for required question: {label or "unlabelled field"}.')
        if has_value and (registry is None or key is None):
            if registry is not None:
                registry.record_missing(label or 'Unlabelled required field')
                raise CannotCompleteApplication(f'Cannot verify the prefilled answer: {label or "unlabelled field"}.')
            continue
        if has_value and answer is not None:
            same = existing.casefold() == str(answer).strip().casefold()
            if key == 'current_city' and str(answer).casefold() in existing.casefold(): same = True
            if key == 'phone_country_code' and str(answer) in existing: same = True
            if same:
                if audit and kind != 'password': audit.field(label, existing, 'verified prefilled', page)
                continue
        if answer is None or str(answer).strip() == '':
            if required:
                if registry is not None: registry.record_missing(label or 'Unlabelled required field')
                raise CannotCompleteApplication(f'No saved answer for required question: {label or "unlabelled field"}.')
            continue
        if field.tag_name == 'select':
            options = Select(field).options
            match = _matching_choice(options, answer, 'experience' in label.lower())
            if not match and key == 'education_institution' and registry is not None:
                institution = str(answer).casefold()
                if institution == 'guru tegh bahadur institute of technology':
                    matches = [o for o in options if (o.text.strip().casefold() == 'gtbit' or
                               re.sub(r'[^a-z0-9]', '', o.text.casefold()) in
                               ('guruteghbahadurinstituteoftechnology', 'guruteghbahadurinstituteoftechnologygtbit'))]
                    if len(matches) == 1: match = matches[0]
            if not match and key == 'phone_country_code':
                matches = [o for o in options if str(answer) in o.text]
                if len(matches) == 1: match = matches[0]
            if not match:
                if required or has_value:
                    if registry is not None: registry.record_missing(label or 'Unlabelled required field')
                    raise CannotCompleteApplication(f'No unambiguous option for required question: {label}.')
                continue
            Select(field).select_by_visible_text(match.text)
            actual = Select(field).first_selected_option.text.strip()
        else:
            max_length = field.get_attribute('maxlength')
            if max_length and str(max_length).isdigit() and len(str(answer)) > int(max_length):
                if registry is not None: registry.record_missing(label)
                raise CannotCompleteApplication(f'The verified answer is too long for: {label}.')
            if kind == 'date':
                _replace_date(field, answer, label)
            else:
                _replace_text(field, answer, label, driver)
            actual = (field.get_attribute('value') or '').strip()
        if audit and kind != 'password': audit.field(label, actual, 'filled', page)
        log(f'Filled a saved answer for: {label}')

    # Do not choose arbitrary answers or accept a required certification automatically.
    for group in modal.find_elements(By.XPATH, './/fieldset|.//*[@role="radiogroup"]'):
        options = group.find_elements(By.XPATH, './/input[@type="radio" or @type="checkbox"]')
        if not options: continue
        legends = group.find_elements(By.TAG_NAME, 'legend')
        lines = group.text.splitlines()
        label = (legends[0].text if legends else (group.get_attribute('aria-label') or options[0].get_attribute('aria-label') or (lines[0] if lines else 'Unlabelled choice'))).strip()
        try:
            parent_lines = group.find_element(By.XPATH, '..').text.splitlines()
        except (NoSuchElementException, StaleElementReferenceException):
            parent_lines = []
        required = ('*' in label or any(option.get_attribute('required') for option in options) or
                    any(line.strip().rstrip('*').strip().casefold() == label.casefold() and line.strip().endswith('*') for line in parent_lines))
        selected = [option for option in options if option.is_selected()]
        if 'resume' in label.lower(): continue  # verified separately by _ensure_resume
        if registry is not None and registry.unverified(label) and (required or selected):
            registry.record_missing(label)
            raise CannotCompleteApplication('Unverified application question: ' + label)
        if registry is not None and required:
            answer = registry.answer(label, job_id=job_id)
            if answer is None:
                registry.record_missing(label)
                raise CannotCompleteApplication('No verified answer for required choice: ' + label[:180])
            def option_text(option):
                text = option.get_attribute('aria-label') or ''
                if text.strip().casefold() == label.casefold(): text = ''
                field_id = option.get_attribute('id')
                if not text and field_id:
                    text = next((x.text for x in group.find_elements(By.TAG_NAME, 'label')
                                 if x.get_attribute('for') == field_id), '')
                if not text:
                    try: text = option.find_element(By.XPATH, '../..').text.strip()
                    except (NoSuchElementException, StaleElementReferenceException): pass
                return (text or option.get_attribute('value') or '').strip()
            expected = str(answer).strip().casefold()
            matched = [option for option in options if option_text(option).casefold() == expected]
            if not matched and expected in ('yes', 'no'):
                matched = [option for option in options if re.match(r'^' + expected + r'(?:\b|[,;])', option_text(option), re.I)]
            if len(matched) != 1:
                registry.record_missing(label)
                raise CannotCompleteApplication('No unambiguous option for required choice: ' + label[:180])
            if not matched[0].is_selected():
                field_id = matched[0].get_attribute('id')
                labels = [x for x in group.find_elements(By.TAG_NAME, 'label') if x.get_attribute('for') == field_id]
                _click(driver, labels[0] if labels else matched[0])
            if not matched[0].is_selected():
                raise CannotCompleteApplication('Could not verify the selected answer: ' + label[:180])
            if audit: audit.field(label, option_text(matched[0]), 'selected', page)
        elif required and not selected:
            raise CannotCompleteApplication('A required choice needs an answer: ' + label[:180])
    for field in modal.find_elements(By.XPATH, './/input[@required and @type="checkbox"]'):
        if not field.is_selected():
            label = _field_label(modal, field) or 'Required checkbox or choice'
            answer = registry.answer(label) if registry is not None else None
            if (str(answer).strip().casefold() != 'yes' or
                    re.search(r'\b(?:agree|agreement|consent|terms|policy|certify|certification|declare|declaration|affirm|acknowledge|confirm)\b', label, re.I)):
                if registry is not None: registry.record_missing(label)
                raise CannotCompleteApplication('A required checkbox or choice needs your answer.')
            field_id = field.get_attribute('id')
            labels = [x for x in modal.find_elements(By.TAG_NAME, 'label') if x.get_attribute('for') == field_id] if field_id else []
            _click(driver, labels[0] if labels else field)
            if not field.is_selected():
                raise CannotCompleteApplication('Could not verify the selected checkbox: ' + label)
            if audit: audit.field(label, 'Yes', 'selected', page)
    required_radios = modal.find_elements(By.XPATH, './/input[@required and @type="radio"]')
    all_radios = modal.find_elements(By.XPATH, './/input[@type="radio"]') if required_radios else []
    for field in required_radios:
        name = field.get_attribute('name')
        group = [other for other in all_radios if other.get_attribute('name') == name] if name else [field]
        if not any(option.is_selected() for option in group):
            if registry is not None: registry.record_missing(_field_label(modal, field) or 'Required radio choice')
            raise CannotCompleteApplication('A required radio choice needs your answer.')


def _ensure_resume(driver, modal, resume_path):
    path = Path(resume_path).expanduser().resolve()
    if not path.is_file():
        raise CannotCompleteApplication('The configured resume file could not be found.')
    name = path.name
    def matching_radio(m):
        # Locate the radio within the same resume card as the filename. A
        # selected old resume elsewhere in the dialog is not sufficient.
        for radio in m.find_elements(By.CSS_SELECTOR, 'input[type="radio"]'):
            for parent in radio.find_elements(By.XPATH, './ancestor::*[position() <= 5]'):
                if name.casefold() in parent.text.casefold():
                    radios = parent.find_elements(By.CSS_SELECTOR, 'input[type="radio"]')
                    if len(radios) == 1:
                        return radio, parent
        return None

    def choose(m):
        match = matching_radio(m)
        if not match: return False
        radio, container = match
        if not radio.is_selected():
            labels = container.find_elements(By.TAG_NAME, 'label')
            _click(driver, labels[0] if labels else radio)
        return radio.is_selected()

    if choose(modal): return
    # Send the path directly to the existing file input. Clicking the upload
    # button opens an operating-system picker, which Selenium cannot dismiss.
    inputs = modal.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
    if not inputs:
        # LinkedIn can place its hidden upload input outside the dialog.
        inputs = driver.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
    if not inputs:
        buttons = [b for b in modal.find_elements(By.TAG_NAME, 'button')
                   if b.text.strip().casefold() == 'upload resume' and b.is_displayed()]
        if len(buttons) == 1:
            # Some versions create the input in the upload button's click
            # handler. Capture the input's click instead of opening a picker.
            created = driver.execute_script('''
                const original = HTMLInputElement.prototype.click;
                let input = null;
                HTMLInputElement.prototype.click = function() {
                    if (this.type === 'file') { input = this; return; }
                    return original.call(this);
                };
                try { arguments[0].click(); }
                finally { HTMLInputElement.prototype.click = original; }
                if (input && !input.isConnected) document.body.appendChild(input);
                return input;
            ''', buttons[0])
            if created: inputs = [created]
    if len(inputs) != 1:
        raise CannotCompleteApplication('Could not find an unambiguous resume upload input without opening a file picker.')
    file_input = inputs[0]
    file_input.send_keys(str(path))
    def selected(_):
        m = _modal(driver)
        if not m or name.lower() not in m.text.lower(): return False
        return choose(m)
    try:
        WebDriverWait(driver, 20).until(selected)
    except TimeoutException as error:
        raise CannotCompleteApplication('Could not verify that the configured resume is selected.') from error


def _verify_work_experience(modal, registry):
    if registry is None: return
    employer = str(registry.value('current_employer') or '').strip()
    role = str(registry.value('linkedin_profile_current_role') or registry.value('current_role') or '').strip()
    if not employer or not role: return
    for button in modal.find_elements(By.XPATH, './/button[contains(@aria-label,"Edit, Work Experience")]'):
        text = button.find_element(By.XPATH, '../..').text
        if employer.casefold() in text.casefold() and role.casefold() not in text.casefold():
            question = f'LinkedIn work experience for {employer} differs from the verified current role.'
            registry.record_missing(question)
            raise CannotCompleteApplication(question + ' Update the profile before submitting this application.')


def _dismiss(driver):
    try:
        m = _modal(driver)
        initial_dialogs = [x for x in driver.find_elements(By.CSS_SELECTOR, 'dialog[open], [role="dialog"]') if x.is_displayed()]
        save_prompt_open = any(any('discard' in b.text.casefold() for b in dialog.find_elements(By.TAG_NAME, 'button') if b.is_displayed()) for dialog in initial_dialogs)
        if m and not save_prompt_open:
            buttons = m.find_elements(By.XPATH, './/button[@aria-label="Dismiss" or @aria-label="Close"]')
            if buttons: buttons[0].click()
        time.sleep(1)
        def confirmation_or_closed(d):
            dialogs = [x for x in d.find_elements(By.CSS_SELECTOR, 'dialog[open], [role="dialog"]') if x.is_displayed()]
            # A confirmation can overlay the application dialog. Resolve it
            # before trying to click any control on the underlying form.
            for dialog in reversed(dialogs):
                buttons = dialog.find_elements(By.TAG_NAME, 'button')
                visible = [button for button in buttons if button.is_displayed()]
                has_discard = any('discard' in button.text.casefold() for button in visible)
                is_save_prompt = has_discard or 'save this application' in dialog.text.casefold()
                if is_save_prompt:
                    save = [button for button in visible if button.text.strip().casefold() in
                            ('save', 'save application', 'save and exit', 'save for later', 'save draft')]
                    if len(save) == 1: return save[0]
                    return False
            for dialog in reversed(dialogs):
                visible = [button for button in dialog.find_elements(By.TAG_NAME, 'button') if button.is_displayed()]
                # Some application dialogs lack the expected header; close
                # their explicit close control and wait for the save prompt.
                close = [button for button in visible if (button.get_attribute('aria-label') or '').casefold() in ('dismiss', 'close')]
                if len(close) == 1:
                    close[0].click()
                    return False
            return True if not dialogs and not _modal(d) else False
        try:
            result = WebDriverWait(driver, 5).until(confirmation_or_closed)
            saved = result is not True
            if result is not True:
                result.click()
            WebDriverWait(driver, 5).until(lambda d: not _modal(d) and not any(
                x.is_displayed() for x in d.find_elements(By.CSS_SELECTOR, 'dialog[open], [role="dialog"]')))
            return saved
        except TimeoutException as error:
            raise CannotCompleteApplication('The application dialog could not be closed; stop and inspect the browser.') from error
    except WebDriverException as error:
        raise CannotCompleteApplication('Could not save or close the incomplete application.') from error


def _apply_current_job(driver, profile, questions, email, resume_path, dry_run, log, company, job_id, registry=None, audit=None):
    # The details pane loads asynchronously even after the job URL changes.
    # A visible Easy Apply button may have text but no stable aria-label.
    try:
        buttons = WebDriverWait(driver, 12, ignored_exceptions=(StaleElementReferenceException,)).until(
            lambda d: _visible_apply_buttons(d)
            if _selected_job_id_from_url(d.current_url) == str(job_id) else False)
    except TimeoutException:
        return 'skip', 'Easy Apply control not found for the selected job after waiting; the panel may still be loading or LinkedIn changed its markup.'
    if len(buttons) != 1:
        return 'skip', 'Multiple application controls matched; refusing to click an ambiguous button.'
    _click(driver, buttons[0])
    try:
        WebDriverWait(driver, 12).until(lambda d: _modal(d) and _progress(_modal(d)))
        for _ in range(15):
            modal = _modal(driver)
            if not modal: raise CannotCompleteApplication('The application dialog closed unexpectedly.')
            page = _progress(modal)
            body = modal.text
            _verify_work_experience(modal, registry)
            if re.search(r'\bResume\*', body) and 'Upload resume' in body:
                _ensure_resume(driver, modal, resume_path)
                if audit: audit.field('Resume', Path(resume_path).name, 'selected', page)
                modal = _modal(driver)
            _fill_fields(driver, modal, profile, questions, email, log, registry, audit, page, job_id)
            modal = _modal(driver)
            _verify_name_fields(modal, registry, audit, page)
            submit = [b for b in modal.find_elements(By.TAG_NAME, 'button')
                      if b.text.strip().casefold() == 'submit application']
            if submit:
                _verify_review_name(modal.text, registry)
                _clear_optional_company_follow(driver, modal)
                if Path(resume_path).name.lower() not in modal.text.lower():
                    raise CannotCompleteApplication('The final review does not show the configured resume.')
                if dry_run:
                    _dismiss(driver)
                    return 'review', 'Reached the final review with the configured resume.'
                if audit: audit.finish('submitting', 'Submission is being attempted; confirmation is pending.')
                _click(driver, submit[0])
                try:
                    def confirmed(d):
                        for dialog in d.find_elements(By.CSS_SELECTOR, 'dialog[open]'):
                            if not dialog.is_displayed(): continue
                            if _confirmation_matches(dialog.text, company): return True
                        return False
                    WebDriverWait(driver, 15).until(confirmed)
                except TimeoutException as error:
                    # Some LinkedIn screens close the confirmation without naming the
                    # company. Check this exact job's application status before retrying.
                    search_url = driver.current_url
                    try:
                        driver.get(f'https://www.linkedin.com/jobs/view/{job_id}/')
                        WebDriverWait(driver, 15).until(lambda d: f'/jobs/view/{job_id}' in d.current_url
                            and _job_status_confirms(d.find_element(By.TAG_NAME, 'body').text, company))
                        driver.get(search_url)
                        WebDriverWait(driver, 15).until(lambda d: d.find_elements(By.XPATH, CARDS_XPATH))
                    except (TimeoutException, WebDriverException) as status_error:
                        raise SubmissionUncertain('Submit was clicked, but the application status could not be confirmed. Check Applied jobs before retrying.') from status_error
                for button in driver.find_elements(By.XPATH, '//button[normalize-space(.)="Not now" or normalize-space(.)="Done"]'):
                    if button.is_displayed():
                        try: button.click()
                        except WebDriverException: pass
                        break
                return 'submitted', 'LinkedIn confirmed the application was sent.'
            buttons = [b for b in modal.find_elements(By.TAG_NAME, 'button')
                       if b.text.strip() in ('Next', 'Review') and b.is_displayed()]
            if len(buttons) != 1:
                raise CannotCompleteApplication('Could not find the next step in the application.')
            _click(driver, buttons[0])
            try:
                WebDriverWait(driver, 10).until(lambda d: _modal(d) and
                    _progress(_modal(d)) is not None and _progress(_modal(d)) != page)
            except TimeoutException as error:
                m = _modal(driver)
                text = m.text if m else ''
                messages = [line.strip() for line in text.splitlines() if 'required' in line.lower() or 'please enter' in line.lower()]
                raise CannotCompleteApplication('The form did not advance' + (': ' + '; '.join(messages[:3]) if messages else '.')) from error
        raise CannotCompleteApplication('The application exceeded the maximum number of steps.')
    except CannotCompleteApplication as error:
        saved = _dismiss(driver)
        return 'skip', str(error) + (' Draft saved on LinkedIn.' if saved else '')


def apply_current_job(driver, profile, questions, email, resume_path, dry_run, log, company, job_id, registry=None, title=''):
    audit = ApplicationAudit(job_id, title, company)
    try:
        outcome, message = _apply_current_job(driver, profile, questions, email, resume_path, dry_run, log, company, job_id, registry, audit)
    except Exception as error:
        if audit.entry.get('status') == 'submitting' and not isinstance(error, SubmissionUncertain):
            audit.finish('uncertain', 'Submit was attempted but could not be confirmed.')
            raise SubmissionUncertain('Submit was attempted but could not be confirmed; check LinkedIn before retrying.') from error
        audit.finish('uncertain' if isinstance(error, SubmissionUncertain) else 'error', str(error))
        raise
    audit.finish(outcome, message)
    return outcome, message


def _select_easy_apply(driver, log):
    if parse_qs(urlparse(driver.current_url).query).get('f_AL') == ['true']:
        return
    try:
        label = WebDriverWait(driver, 15).until(lambda d: next(
            (x for x in d.find_elements(By.XPATH, "//label[normalize-space(.)='Easy Apply']")
             if x.is_displayed()), False))
    except TimeoutException as error:
        raise CannotCompleteApplication('LinkedIn did not show the Easy Apply search filter.') from error
    _click(driver, label)
    WebDriverWait(driver, 15).until(lambda d: parse_qs(urlparse(d.current_url).query).get('f_AL') == ['true'])
    log('Easy Apply filter enabled.')


def open_profile_matched_jobs(driver, log):
    """Open LinkedIn's own profile-matched results, if that list is available."""
    driver.get('https://www.linkedin.com/jobs/')
    try:
        link = WebDriverWait(driver, 20, ignored_exceptions=(StaleElementReferenceException,)).until(lambda d: next(
            (a for a in d.find_elements(By.XPATH, "//a[contains(@href,'QUALIFICATION_LANDING')]")
             if a.is_displayed() and 'currentJobId' not in parse_qs(urlparse(a.get_attribute('href') or '').query)), False))
        url = link.get_attribute('href')
        if not url or urlparse(url).hostname not in ('www.linkedin.com', 'linkedin.com'):
            log('LinkedIn did not provide a usable profile-matched list; continuing with the saved searches.')
            return False
        log('Found the profile-matched jobs link; opening the results.')
        driver.get(url)
        WebDriverWait(driver, 20).until(lambda d: is_modern_page(d) and d.find_elements(By.XPATH, CARDS_XPATH))
        log('Opened LinkedIn jobs that match your profile.')
        return True
    except (TimeoutException, WebDriverException) as error:
        log('Profile-matched list lookup failed: ' + type(error).__name__)
        try:
            headings = [h.text.strip() for h in driver.find_elements(By.XPATH, '//h2') if 'job' in h.text.casefold()]
            log('LinkedIn jobs sections visible: ' + (', '.join(headings[:8]) or 'none'))
            links = driver.find_elements(By.XPATH, "//a[contains(@href,'QUALIFICATION_LANDING')]")
            log('Profile-match navigation links found: ' + str(len(links)))
            for link in links[:8]:
                query = parse_qs(urlparse(link.get_attribute('href') or '').query)
                log('Profile link: origin=' + str(query.get('origin', [])) +
                    ', individual_job=' + str('currentJobId' in query) +
                    ', raw_has_job_id=' + str('currentJobId=' in (link.get_dom_attribute('href') or '')) +
                    ', visible=' + str(link.is_displayed()) +
                    ', label=' + (link.get_attribute('aria-label') or link.text or '')[:90])
        except WebDriverException:
            pass
        log('LinkedIn did not provide an accessible profile-matched list; continuing with the saved searches.')
        return False


def _card_details(card):
    dismiss = card.find_element(By.XPATH, ".//button[starts-with(@aria-label,'Dismiss ') and contains(@aria-label,' job')]")
    title = re.sub(r'^Dismiss\s+|\s+job$', '', dismiss.get_attribute('aria-label') or '').strip()
    lines = [line.strip() for line in card.text.splitlines() if line.strip()]
    def is_title_line(line):
        normalized = re.sub(r'^Selected,\s*|\s*\(Verified job\)$', '', line).strip()
        return normalized.casefold() == title.casefold()
    lines = [line for line in lines if not is_title_line(line)]
    company = lines[0] if lines else 'Unknown'
    location = lines[1] if len(lines) > 1 else 'Unknown'
    return title, company, location, lines


def _card_job_id(card):
    nodes = [card] + card.find_elements(By.CSS_SELECTOR, '[componentkey^="job-card-component-ref-"]')
    for node in nodes:
        match = re.search(r'job-card-component-ref-(\d+)', node.get_attribute('componentkey') or '')
        if match: return match.group(1)
    return None


def _required_years(description):
    pattern = r'\b(\d{1,2})\s*(?:\+|[-–]\s*\d+)?\s*years?\s*(?:of\s+)?(?:[\w-]+\s+){0,4}experience\b'
    return [int(n) for n in re.findall(pattern, description, re.I) if int(n) <= 12]


def _within_candidate_location(location, anywhere_in_india=False):
    """The candidate has not confirmed relocation or foreign work authorization."""
    text = location.casefold()
    ncr = any(place in text for place in ('delhi', 'ncr', 'noida', 'gurugram', 'gurgaon', 'ghaziabad', 'faridabad'))
    if ncr: return True
    if anywhere_in_india:
        indian_places = ('india', 'bengaluru', 'bangalore', 'mumbai', 'pune', 'hyderabad', 'chennai',
                         'kolkata', 'ahmedabad', 'surat', 'jaipur', 'lucknow', 'chandigarh', 'mohali',
                         'indore', 'bhopal', 'kochi', 'cochin', 'coimbatore', 'thiruvananthapuram',
                         'trivandrum', 'visakhapatnam', 'bhubaneswar', 'nagpur', 'vadodara', 'mysuru',
                         'mysore', 'gujarat', 'maharashtra', 'karnataka', 'tamil nadu', 'telangana',
                         'kerala', 'rajasthan', 'punjab', 'haryana', 'uttar pradesh', 'madhya pradesh',
                         'west bengal', 'odisha', 'andhra pradesh', 'goa', 'bihar', 'uttarakhand')
        return any(re.search(r'(?<!\w)' + re.escape(place) + r'(?!\w)', text) for place in indian_places)
    return 'remote' in text and 'india' in text


def _next_results_page(driver, page):
    buttons = driver.find_elements(By.XPATH, f"//button[@aria-label='Page {page + 1}']")
    if not buttons:
        buttons = driver.find_elements(By.XPATH, "//button[normalize-space(.)='Next' and not(@disabled)]")
    button = next((b for b in buttons if b.is_displayed() and b.is_enabled()), None)
    if not button: return False
    _click(driver, button)
    try:
        WebDriverWait(driver, 12).until(lambda d: any(
            b.get_attribute('aria-current') == 'true'
            for b in d.find_elements(By.XPATH, f"//button[@aria-label='Page {page + 1}']")))
        WebDriverWait(driver, 12).until(lambda d: d.find_elements(By.XPATH, CARDS_XPATH))
        return True
    except TimeoutException:
        return False


def run_search(driver, *, profile, questions, email, resume_path, dry_run, limit,
               applied_ids, bad_words, company_bad_words, company_good_words, max_years,
               find_bad_word, on_submitted, log, registry=None, max_pages=5, require_js_or_ts=False,
               attempted_ids=None, submitted_roles=None, profile_matches=False):
    """Process current search results, stopping if a submission cannot be confirmed."""
    submitted = skipped = reviewed = 0
    if attempted_ids is None: attempted_ids = set()
    if submitted_roles is None: submitted_roles = set()
    _select_easy_apply(driver, log)
    WebDriverWait(driver, 15).until(lambda d: d.find_elements(By.XPATH, CARDS_XPATH))
    seen = set()
    for page in range(1, max_pages + 1):
        for index in range(100):
            if submitted >= limit: break
            try:
                cards = WebDriverWait(driver, 10).until(lambda d: d.find_elements(By.XPATH, CARDS_XPATH))
                if index >= len(cards): break
                card = cards[index]
                title, company, location, lines = _card_details(card)
                hinted_id = _card_job_id(card)
                identity = hinted_id or (title.casefold(), company.casefold(), location.casefold())
                if not title or identity in seen: continue
                seen.add(identity)
                if hinted_id in applied_ids or hinted_id in attempted_ids or any(line.strip().lower() == 'applied' for line in lines): continue
                if profile_matches and find_bad_word(title, ['marketing', 'sales', 'recruiter', 'account manager', 'business development', 'talent acquisition']):
                    log(f'Skipping {title}: the role is outside your software engineering search.'); skipped += 1; continue
                if role_key(company, title) in submitted_roles:
                    log(f'Skipping {title}: this company and title have already been submitted.'); continue
                if registry is not None and not _within_candidate_location(location, registry.value('relocation_within_india') == 'Yes'):
                    log(f'Skipping {title}: the location is outside the confirmed search area.'); skipped += 1; continue
                if not require_js_or_ts and find_bad_word(title, ['senior', 'staff', 'principal', 'lead', 'manager', 'director', 'architect']):
                    log(f'Skipping {title}: the title indicates a senior role.'); skipped += 1; continue
                if find_bad_word(company, company_bad_words):
                    log(f'Skipping {title}: company matches an exclusion.'); skipped += 1; continue
                was_selected = any(line.startswith('Selected, ') for line in card.text.splitlines())
                previous_job_id = _selected_job_id_from_url(driver.current_url)
                driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click()", card)
                # A sidebar job link exists before its details load. Waiting on that
                # link can make us inspect the previous job and miss its Apply button.
                try:
                    job_id = WebDriverWait(driver, 12).until(
                        lambda d: _resolved_selected_job_id(d, hinted_id, previous_job_id, was_selected))
                except TimeoutException as error:
                    raise CannotCompleteApplication(f'Could not verify the selected job details for {title}.') from error
                if job_id in applied_ids or job_id in attempted_ids: continue
                try:
                    heading = WebDriverWait(driver, 10).until(lambda d: next(
                        (h for h in d.find_elements(By.XPATH, "//h2[normalize-space(.)='About the job']")
                         if h.is_displayed()), False))
                    description = heading.find_element(By.XPATH, '../..').get_attribute('textContent') or ''
                except TimeoutException:
                    log(f'Skipping {title}: job description is unavailable.'); skipped += 1; continue
                if len(description.strip()) < 80:
                    log(f'Skipping {title}: job description is incomplete.'); skipped += 1; continue
                if require_js_or_ts and not re.search(r'\b(?:java[ -]?script|type[ -]?script)\b', title + ' ' + description, re.I):
                    log(f'Skipping {title}: the posting does not mention JavaScript or TypeScript.'); skipped += 1; continue
                company_headings = driver.find_elements(By.XPATH, "//h2[normalize-space(.)='About the company']")
                about = company_headings[0].find_element(By.XPATH, '../..').get_attribute('textContent') if company_headings else ''
                if find_bad_word(about or '', company_bad_words) and not find_bad_word(about or '', company_good_words):
                    log(f'Skipping {title}: company description matches an exclusion.'); skipped += 1; continue
                bad = find_bad_word(description, bad_words)
                if bad:
                    log(f'Skipping {title}: description matches exclusion {bad}.'); skipped += 1; continue
                years = _required_years(description)
                if not require_js_or_ts and years and max_years >= 0 and max(years) > max_years:
                    log(f'Skipping {title}: the posting asks for {max(years)} years.'); skipped += 1; continue
                log(f'Opening Easy Apply for {title} at {company}.')
                attempted_ids.add(job_id)
                outcome, message = apply_current_job(driver, profile, questions, email, resume_path, dry_run, log, company, job_id, registry, title)
                log(f'{title}: {message}')
                if outcome == 'submitted':
                    on_submitted(job_id, title, company, location, description, max(years) if years else 'Unknown')
                    applied_ids.add(job_id); submitted += 1
                    key = role_key(company, title)
                    if key: submitted_roles.add(key)
                elif outcome == 'review':
                    reviewed += 1
                    break
                else:
                    skipped += 1
            except SubmissionUncertain as error:
                raise SubmissionUncertain(f'{title} at {company} (job ID {job_id}): {error}') from error
            except (CannotCompleteApplication, NoSuchElementException, StaleElementReferenceException, TimeoutException, WebDriverException) as error:
                detail = str(error).splitlines()
                log(f'Skipping a result because LinkedIn changed the page: {type(error).__name__}: {detail[0] if detail else ""}')
                skipped += 1
                try:
                    _dismiss(driver)
                except CannotCompleteApplication as error:
                    # Do not deliberately discard an application the user asked to save.
                    raise CannotCompleteApplication('Could not confirm the incomplete application was saved; stopping.') from error
        if submitted >= limit or reviewed or page == max_pages: break
        if not _next_results_page(driver, page): break
        log(f'Continuing to search results page {page + 1}.')
    return submitted, skipped, reviewed
