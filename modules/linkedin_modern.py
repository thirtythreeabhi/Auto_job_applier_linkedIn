"""Support LinkedIn's current AI-powered job search and Easy Apply screens.

The older LinkedIn page and its form markup are still handled by runAiBot.py.
This adapter uses accessible names rather than LinkedIn's generated CSS classes.
"""
from __future__ import annotations

import os
import re
import time
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from selenium.common.exceptions import (NoSuchElementException, NoSuchWindowException,
                                        StaleElementReferenceException, TimeoutException,
                                        WebDriverException)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select, WebDriverWait
from modules.application_audit import ApplicationAudit, role_key, record_suitability_decision
from modules.helpers import buffer

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


class EasyApplyLimitReached(Exception):
    """LinkedIn visibly refused further Easy Apply submissions."""
    def __init__(self, visible_message, stage, submission_state='not_submitted'):
        super().__init__(visible_message)
        self.visible_message = visible_message
        self.stage = stage
        self.submission_state = submission_state


_LIMIT_PATTERNS = (
    re.compile(r"\b(?:you|we)(?:['’]ve| have) reached (?:the )?(?:maximum|daily|weekly) (?:number of )?(?:easy apply )?applications?\b", re.I),
    re.compile(r"\b(?:you|we)(?:['’]ve| have) reached (?:your )?(?:application|easy apply)(?: application)? limit(?: for (?:today|this week|now))?\b", re.I),
    re.compile(r"\blimit for the number of applications? you can submit\b", re.I),
    re.compile(r"\b(?:daily )?easy apply application limit (?:has been )?reached\b", re.I),
    re.compile(r"\bunable to submit (?:any )?(?:more )?applications? (?:at this time|right now)\b", re.I),
    re.compile(r"\btemporarily restricted from (?:submitting|applying to) (?:more )?(?:jobs?|applications?)\b", re.I),
    re.compile(r"\btemporarily unable to submit applications?\b", re.I),
)


def _visible_limit_warning(driver):
    """Return a narrowly matched visible alert/message; generic warnings are ignored."""
    try:
        candidates = driver.find_elements(By.CSS_SELECTOR,
            '[role="alert"], [role="status"], [aria-live="assertive"], [aria-live="polite"], '
            'dialog[open], [role="dialog"], .jobs-easy-apply-modal')
        for element in candidates:
            try:
                if not element.is_displayed():
                    continue
                text = re.sub(r'\s+', ' ', element.text or '').strip()
                if text and any(pattern.search(text) for pattern in _LIMIT_PATTERNS):
                    return text[:1000]
            except StaleElementReferenceException:
                continue
    except (WebDriverException, AttributeError):
        pass
    return None


def _raise_if_limited(driver, stage, submission_state='not_submitted'):
    message = _visible_limit_warning(driver)
    if message:
        error = EasyApplyLimitReached(message, stage, submission_state)
        error.job_url = getattr(driver, 'current_url', '')
        raise error


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
    buffer(0)


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
        if _is_skill_summary_question(label):
            return registry.value('technical_skill_summary')
        normalized = re.sub(r'[^a-z0-9]+', ' ', str(label or '').casefold()).strip()
        if normalized == 'share the most impressive thing that you have built or achieved':
            return registry.value('achievement_description')
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
        if re.search(r'\b(?:and|or)\b|[,/]', name):
            if 'nextjs' in compact and 'react' in name:
                return questions.get('nextjs_experience_years')
            return None
        if 'nextjs' in compact: return questions.get('nextjs_experience_years')
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


def _is_skill_summary_question(label):
    normalized = re.sub(r'[^a-z0-9]+', ' ', str(label or '').casefold()).strip()
    return normalized in {'skill set', 'technical skill set', 'technical skills',
                          'core technical skills', 'primary skills'}


def _question_expects_numeric_answer(label):
    question = re.sub(r'\s+', ' ', str(label or '').casefold()).strip()
    if re.match(r'^(?:do you have|are you|have you|can you|would you|will you|is it)\b', question):
        return False
    if re.search(r'\b(?:how many|how much|number of|salary|ctc|lpa|percentage|on a scale|rate your)\b', question):
        return True
    return bool(re.search(r'\byears?\b', question) and
                re.search(r'\bexperience\b', question) and
                not re.search(r'\b(?:at least|minimum|more than|less than)\b', question))


def _is_narrative_question(label, kind):
    if kind not in ('text', 'textarea'):
        return False
    question = re.sub(r'\s+', ' ', str(label or '').casefold())
    return bool(re.search(r'\b(?:describe|tell us|explain|why|share|summary|cover|achievement|experience with|experience in|introduce|motivation|strengths?|project|elaborate|additional information|anything else|comments?|message|about you)\b', question))


_AI_SKILL_DURATION_QUESTION = re.compile(
    r'\b(?:how many|number of)\s+(?:(?:full|professional|work|hands-on)\s+){0,2}(?:years?|months?)\b|'
    r'\b(?:years?|months?)\s+(?:of\s+)?(?:professional\s+|work\s+)?experience\b|'
    r'\b(?:professional|work)\s+experience\b.{0,40}\b(?:years?|months?)\b|'
    r'\bhow long\b.{0,40}\b(?:experience|worked|using|with)\b', re.I)


def _is_skill_duration_question(label):
    question = re.sub(r'\s+', ' ', str(label or '')).strip()
    if not _AI_SKILL_DURATION_QUESTION.search(question):
        return False
    # A requested total tenure is a profile fact; only estimate a named skill's duration.
    return not re.search(r'\b(?:total|overall|professional|work)\s+(?:years?\s+of\s+)?experience\b', question, re.I)


def _skill_duration_fallback(candidate_context):
    """Use the documented overall tenure when the model confuses unknown with zero."""
    context = re.sub(r'\s+', ' ', str(candidate_context or ''))
    patterns = (
        r'total professional experience[^.]{0,120}?(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\b',
        r'overall (?:professional )?experience[^.]{0,100}?(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\b',
    )
    for pattern in patterns:
        match = re.search(pattern, context, re.I)
        if match and float(match.group(1)) > 0:
            return match.group(1)
    # The user's saved profile confirms one year overall; never turn an unknown
    # skill duration into zero merely because the model lacks a keyword match.
    return '1'


def _ai_answer_allowed(label, kind, options=None):
    """Allow best-effort inference for supported application field types."""
    question = re.sub(r'\s+', ' ', str(label or '')).strip().rstrip('*?: ')
    if not question or question.casefold() == 'unknown':
        return False
    if options:
        return kind in ('select', 'single_select', 'radio', 'checkbox', 'text', 'textarea')
    return kind in ('text', 'textarea', 'number', 'email', 'tel', 'url', 'date',
                    'select', 'single_select', 'radio', 'checkbox')


def _generate_application_answer(client, label, kind, options, candidate_context,
                                 job_description, about_company, max_length=None):
    if not _ai_answer_allowed(label, kind, options):
        return None
    answer = ''
    if client:
        from modules.ai.connections import answer_question
        question_type = 'single_select' if options else kind
        answer = answer_question(
            client, label, options=options, question_type=question_type,
            job_description=job_description, about_company=about_company,
            user_information_all=candidate_context)
    answer = answer.strip()
    if not answer or answer.casefold() == 'cannot_answer':
        if options:
            answer = str(options[0])
        elif kind == 'number':
            answer = (_skill_duration_fallback(candidate_context)
                      if _is_skill_duration_question(label) else '0')
        elif kind in ('text', 'textarea', 'email', 'tel', 'url'):
            answer = 'Not provided'
        else:
            return None
    if _is_skill_duration_question(label):
        numeric = re.match(r'^\s*(\d+(?:\.\d+)?)\b', answer)
        if numeric and float(numeric.group(1)) > 0:
            answer = numeric.group(1)
        else:
            answer = _skill_duration_fallback(candidate_context)
    if options and answer not in options:
        answer = str(options[0])
    if max_length and str(max_length).isdigit() and len(answer) > int(max_length):
        answer = answer[:int(max_length)]
    return answer


def _generate_application_selections(client, label, options, candidate_context,
                                     job_description, about_company):
    """Choose matching checkbox options, honoring an explicit requested count."""
    options = [str(option).strip() for option in options if str(option).strip()]
    if not options:
        return []
    count_match = re.search(r'\b(?:top|select|choose)\s+(\d+)\b', str(label or ''), re.I)
    target = min(int(count_match.group(1)), len(options)) if count_match else None
    answer = ''
    if client:
        from modules.ai.connections import answer_question
        answer = answer_question(
            client, label, options=options, question_type='multiple_select',
            job_description=job_description, about_company=about_company,
            user_information_all=candidate_context)
    selected = []
    for item in str(answer or '').splitlines():
        item = item.strip().lstrip('-•* ').strip()
        match = next((option for option in options if item.casefold() == option.casefold()), None)
        if match and match not in selected:
            selected.append(match)
    if not selected:
        context = str(candidate_context or '').casefold()
        selected = [option for option in options if option.casefold() in context]
    if target is None:
        target = len(selected) or 1
    for option in options:
        if len(selected) >= target:
            break
        if option not in selected:
            selected.append(option)
    return selected[:target]


def _radio_option_text(group, option, question_label):
    text = option.get_attribute('aria-label') or ''
    if text.strip().casefold() == question_label.casefold(): text = ''
    field_id = option.get_attribute('id')
    if not text and field_id:
        text = next((x.text for x in group.find_elements(By.TAG_NAME, 'label')
                     if x.get_attribute('for') == field_id), '')
    if not text:
        try: text = option.find_element(By.XPATH, '../..').text.strip()
        except (NoSuchElementException, StaleElementReferenceException): pass
    return (text or option.get_attribute('value') or '').strip()


def _fill_city_typeahead(driver, modal, field, profile):
    city = str(profile.get('current_city') or '').strip()
    if not city:
        raise CannotCompleteApplication('The required city is not saved in your profile.')
    field.click()
    field.send_keys(Keys.CONTROL, 'a')
    field.send_keys(Keys.BACKSPACE)
    try:
        WebDriverWait(driver, 3).until(
            lambda _: not (field.get_attribute('value') or '').strip())
    except TimeoutException as error:
        raise CannotCompleteApplication('Could not clear the existing LinkedIn location before entering a new one.') from error
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


def _pause_checkpoint(controller, validate=None):
    if controller is None:
        return
    try:
        controller.checkpoint(validate=validate)
    except RuntimeError as error:
        raise CannotCompleteApplication(str(error)) from error


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


def _application_answer_cache_key(label, options=()):
    normalize = lambda value: re.sub(r'\s+', ' ', str(value or '')).strip().casefold()
    return normalize(label), tuple(normalize(option) for option in options)


def _fill_fields(driver, modal, profile, questions, email, log, registry=None, audit=None, page=None, job_id=None,
                 ai_client=None, candidate_context='', job_description='', about_company='', ai_model='', company=None,
                 answer_cache=None):
    answer_cache = answer_cache if answer_cache is not None else {}
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
            cache_options = [option.text.strip() for option in Select(field).options
                             if option.text.strip().casefold() not in ('select an option', 'select', 'choose an option')]
        else:
            existing = (field.get_attribute('value') or '').strip()
            has_value = bool(existing)
            cache_options = ()
        key = registry.classify(label) if registry is not None else None
        if registry is not None and registry.unverified(label) and (required or has_value):
            can_infer_answer = (required and
                                _ai_answer_allowed(label, kind))
            if not can_infer_answer:
                registry.record_missing(label)
                raise CannotCompleteApplication(f'Unverified application question: {label or "unlabelled field"}.')
        answer = _answer_for(label, profile, questions, email, registry)
        cache_key = _application_answer_cache_key(label, cache_options)
        if answer is None and cache_key in answer_cache:
            answer = answer_cache[cache_key]
        answer_source = 'verified candidate data' if answer is not None else 'saved profile/configuration'
        nonnumeric_hint = None
        if answer is not None and (kind == 'number' or _question_expects_numeric_answer(label)) and not re.search(r'\d', str(answer)):
            nonnumeric_hint = str(answer)
            answer = None
        if kind == 'number' and answer is not None:
            numeric = re.search(r'-?\d[\d,]*(?:\.\d+)?', str(answer))
            answer = numeric.group(0).replace(',', '') if numeric else None
        field_context = str(candidate_context or '')
        if nonnumeric_hint:
            field_context += f'\n\nEarlier saved response to this numeric question (not itself a number): {nonnumeric_hint}'
        if kind in ('text', 'textarea') and _is_skill_summary_question(label) and ai_client is not None:
            tailored_skills = _generate_application_answer(
                ai_client,
                label + '. Review the full applicant context, including professional work and personal projects. Create a concise but comprehensive skills list across the candidate\'s demonstrated frontend, backend, databases, AI, cloud, testing, and integration work. Expand shorthand such as JS to JavaScript. Do not reduce the answer to one keyword, and do not claim unsupported proficiency levels or years.',
                kind, None, field_context, job_description, about_company,
                field.get_attribute('maxlength'))
            if tailored_skills:
                answer = tailored_skills
                answer_source = f'AI tailored ({ai_model})'
        elif (answer and ai_client is not None and _is_narrative_question(label, kind)
              and answer.strip().casefold() not in {'yes', 'no', 'decline'}
              and not re.fullmatch(r'\d+(?:\.\d+)?', answer.strip()) and len(answer.strip()) < 180):
            narrative_context = (field_context + '\n\nSaved draft for this question: ' + answer)
            enriched = _generate_application_answer(
                ai_client,
                label + '. Turn the saved draft into a polished, specific answer using the applicant context. Preserve its factual meaning and add only relevant details supported by professional work or projects.',
                kind, None, narrative_context, job_description, about_company,
                field.get_attribute('maxlength'))
            if enriched:
                answer = enriched
                answer_source = f'AI enriched ({ai_model})'
        if kind == 'number' and key == 'ai_development_duration' and answer == '1 year':
            answer = '1'
        if registry is not None and key == 'last_name' and answer is not None and not has_middle_name_field:
            # LinkedIn's two-name forms need the middle and family names together.
            middle = str(registry.value('middle_name') or '').strip()
            answer = ' '.join(x for x in (middle, str(answer).strip()) if x)
        if (answer is None or not str(answer).strip()) and required and _ai_answer_allowed(label, kind):
            choices = None
            if field.tag_name == 'select':
                choices = [option.text.strip() for option in Select(field).options
                           if option.text.strip().casefold() not in ('select an option', 'select', 'choose an option')]
            answer = _generate_application_answer(
                ai_client, label, kind, choices, field_context, job_description,
                about_company, field.get_attribute('maxlength'))
            if answer is not None:
                answer_source = f'AI generated ({ai_model})'
                answer_cache[cache_key] = answer
        if registry is not None and required and answer is None:
            registry.record_missing(label or 'Unlabelled required field')
            raise CannotCompleteApplication(f'No verified answer for required question: {label or "unlabelled field"}.')
        if has_value and (registry is None or key is None):
            if registry is not None and answer is None:
                registry.record_missing(label or 'Unlabelled required field')
                raise CannotCompleteApplication(f'Cannot verify the prefilled answer: {label or "unlabelled field"}.')
            if registry is None:
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
        if audit and kind != 'password': audit.field(label, actual, answer_source, page)
        buffer(0)
        log(f'Filled {"an AI drafted" if answer_source.startswith("AI") else "a saved"} answer for: {label}')

    # Handle radio groups and AI-selected checkbox groups separately from scalar fields.
    grouped_checkbox_ids = set()
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
        required = ('*' in label or any(line.strip().endswith('*') for line in lines) or
                    any(option.get_attribute('required') for option in options) or
                    any(line.strip().rstrip('*').strip().casefold() == label.casefold() and line.strip().endswith('*') for line in parent_lines))
        selected = [option for option in options if option.is_selected()]
        if 'resume' in label.lower(): continue  # verified separately by _ensure_resume
        is_checkbox_group = any(option.get_attribute('type') == 'checkbox' for option in options)
        if is_checkbox_group:
            grouped_checkbox_ids.update(option.get_attribute('id') for option in options if option.get_attribute('id'))
        option_labels = [_radio_option_text(group, option, label) for option in options]
        if registry is not None and registry.unverified(label) and (required or selected):
            can_infer_choice = (required and not is_checkbox_group and
                                _ai_answer_allowed(label, 'single_select', option_labels))
            if not can_infer_choice:
                registry.record_missing(label)
                raise CannotCompleteApplication('Unverified application question: ' + label)
        if is_checkbox_group:
            if required:
                instruction_lines = [line.strip() for line in lines
                                     if line.strip().endswith('*') and line.strip() != label]
                selection_question = ' '.join([label, *instruction_lines]).strip()
                verified_choice = registry.answer(selection_question, job_id=job_id, company=company) if registry is not None else None
                if not verified_choice:
                    if registry is not None: registry.record_missing(label)
                    raise CannotCompleteApplication('A required checkbox group needs a verified answer: ' + label[:180])
                chosen = [item.strip() for item in str(verified_choice).split(',') if item.strip()]
                if not chosen or any(not any(item.casefold() == option.casefold() for option in option_labels) for item in chosen):
                    if registry is not None: registry.record_missing(label)
                    raise CannotCompleteApplication('No unambiguous verified options for: ' + label[:180])
                chosen_set = {item.casefold() for item in chosen}
                for option, option_label in zip(options, option_labels):
                    if option.is_selected() == (option_label.casefold() in chosen_set):
                        continue
                    field_id = option.get_attribute('id')
                    labels = [x for x in group.find_elements(By.TAG_NAME, 'label')
                              if x.get_attribute('for') == field_id] if field_id else []
                    _click(driver, labels[0] if labels else option)
                selected_labels = [text for option, text in zip(options, option_labels)
                                   if option.is_selected()]
                if not selected_labels:
                    raise CannotCompleteApplication('Could not select any options for: ' + label[:180])
                if audit: audit.field(label, ', '.join(selected_labels), 'verified candidate data', page)
            continue
        if registry is not None and required:
            try:
                answer = registry.answer(label, job_id=job_id, company=company)
            except TypeError:
                try:
                    answer = registry.answer(label, job_id=job_id)
                except TypeError:
                    answer = registry.answer(label)
        else:
            answer = None
        cache_key = _application_answer_cache_key(label, option_labels)
        if answer is None:
            answer = answer_cache.get(cache_key)
        answer_source = 'verified candidate data'
        if required and (answer is None or not str(answer).strip()) and not is_checkbox_group:
            answer = _generate_application_answer(
                ai_client, label, 'single_select', option_labels, candidate_context,
                job_description, about_company)
            if answer is not None:
                answer_source = f'AI generated ({ai_model})'
                answer_cache[cache_key] = answer
        if required and answer is None and registry is not None:
            registry.record_missing(label)
            raise CannotCompleteApplication('No verified answer for required choice: ' + label[:180])
        if required and answer is not None:
            expected = str(answer).strip().casefold()
            matched = [option for option, text in zip(options, option_labels) if text.casefold() == expected]
            if not matched and expected in ('yes', 'no'):
                matched = [option for option, text in zip(options, option_labels)
                           if re.match(r'^' + expected + r'(?:\b|[,;])', text, re.I)]
            if len(matched) != 1:
                if registry is not None: registry.record_missing(label)
                raise CannotCompleteApplication('No unambiguous option for required choice: ' + label[:180])
            if not matched[0].is_selected():
                field_id = matched[0].get_attribute('id')
                labels = [x for x in group.find_elements(By.TAG_NAME, 'label') if x.get_attribute('for') == field_id]
                _click(driver, labels[0] if labels else matched[0])
            if not matched[0].is_selected():
                raise CannotCompleteApplication('Could not verify the selected answer: ' + label[:180])
            if audit: audit.field(label, _radio_option_text(group, matched[0], label), answer_source, page)
        elif required and not selected:
            if answer is None:
                raise CannotCompleteApplication('A required choice needs an answer: ' + label[:180])
    for field in modal.find_elements(By.XPATH, './/input[@required and @type="checkbox"]'):
        field_id = field.get_attribute('id') if hasattr(field, 'get_attribute') else None
        if field_id in grouped_checkbox_ids:
            continue
        label = _field_label(modal, field) or 'Required checkbox or choice'
        if not field.is_selected():
            if registry is not None: registry.record_missing(label)
            raise CannotCompleteApplication('A required checkbox needs your explicit review: ' + label)
        answer = registry.answer(label, job_id=job_id, company=company) if registry is not None else None
        if str(answer or '').strip().casefold() not in {'yes', 'true', 'agree', 'accepted'}:
            if registry is not None: registry.record_missing(label)
            raise CannotCompleteApplication('The selected required checkbox has no verified affirmative answer: ' + label)
    required_radios = modal.find_elements(By.XPATH, './/input[@required and @type="radio"]')
    all_radios = modal.find_elements(By.XPATH, './/input[@type="radio"]') if required_radios else []
    for field in required_radios:
        name = field.get_attribute('name')
        group = [other for other in all_radios if other.get_attribute('name') == name] if name else [field]
        if not any(option.is_selected() for option in group):
            if registry is not None: registry.record_missing(_field_label(modal, field) or 'Required radio choice')
            raise CannotCompleteApplication('A required radio choice needs your answer.')


def _repair_invalid_fields(driver, modal, ai_client, candidate_context,
                          job_description, about_company, ai_model, log):
    """Use LinkedIn's validation feedback to revise rejected required fields."""
    try:
        fields = driver.execute_script(
            "return Array.from(arguments[0].querySelectorAll('input:invalid,select:invalid,textarea:invalid'))",
            modal)
    except WebDriverException:
        fields = []
    if not fields:
        fields = modal.find_elements(
            By.XPATH, './/*[@aria-invalid="true"]')
    repaired = 0
    for field in fields:
        try:
            if not field.is_displayed():
                continue
            kind = (field.get_attribute('type') or field.tag_name).lower()
            label = _field_label(modal, field) or 'Required application field'
            message = driver.execute_script(
                'return arguments[0].validationMessage || ""', field) or ''
            question = label + (f' (LinkedIn validation: {message})' if message else '')
            options = None
            if field.tag_name == 'select':
                options = [option.text.strip() for option in Select(field).options
                           if option.text.strip().casefold() not in
                           ('select an option', 'select', 'choose an option')]
            answer = _generate_application_answer(
                ai_client, question, kind, options, candidate_context,
                job_description, about_company, field.get_attribute('maxlength'))
            if kind == 'checkbox':
                continue
            elif field.tag_name == 'select':
                match = _matching_choice(Select(field).options, answer, 'experience' in label.lower())
                if match is not None:
                    Select(field).select_by_visible_text(match.text)
                    repaired += 1
            elif answer is not None:
                if kind == 'date':
                    _replace_date(field, answer, question)
                else:
                    _replace_text(field, answer, question, driver)
                repaired += 1
            if answer is not None:
                log(f'Revised rejected field from LinkedIn validation: {label} ({ai_model or "best-effort fallback"}).')
        except (WebDriverException, CannotCompleteApplication, ValueError):
            continue
    return repaired


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


def _apply_current_job(driver, profile, questions, email, resume_path, dry_run, log, company, job_id,
                       registry=None, audit=None, ai_client=None, candidate_context='',
                       job_description='', about_company='', ai_model='', pause_before_submit=False,
                       confirm_before_submit=None, job_title='', pause_controller=None):
    # The details pane loads asynchronously even after the job URL changes.
    # A visible Easy Apply button may have text but no stable aria-label.
    _raise_if_limited(driver, 'before_easy_apply')
    try:
        buttons = WebDriverWait(driver, 12, ignored_exceptions=(StaleElementReferenceException,)).until(
            lambda d: _visible_apply_buttons(d)
            if _selected_job_id_from_url(d.current_url) == str(job_id) else False)
    except TimeoutException:
        return 'skip', 'Easy Apply control not found for the selected job after waiting; the panel may still be loading or LinkedIn changed its markup.'
    if len(buttons) != 1:
        return 'skip', 'Multiple application controls matched; refusing to click an ambiguous button.'
    _click(driver, buttons[0])
    _raise_if_limited(driver, 'after_easy_apply_click')
    try:
        _raise_if_limited(driver, 'opening_modal')
        def application_ready(d):
            # Normal Easy Apply form
            modal = _modal(d)
            if modal and _progress(modal):
                return True
            # LinkedIn job search safety reminder
            dialogs = d.find_elements(
                By.CSS_SELECTOR, 'dialog[open], [role="dialog"]'
            )
            for dialog in dialogs:
                if not dialog.is_displayed():
                    continue
                if 'job search safety reminder' not in dialog.text.casefold():
                    continue
                buttons = [
                    b for b in dialog.find_elements(By.TAG_NAME, 'button')
                    if b.is_displayed()
                    and b.is_enabled()
                    and b.text.strip().casefold() == 'continue applying'
                ]
                if len(buttons) == 1:
                    _click(d, buttons[0])
                    log('Dismissed LinkedIn job search safety reminder.')
                    return False
            return False
        WebDriverWait(
            driver,
            15,
            ignored_exceptions=(StaleElementReferenceException,)
        ).until(application_ready)
        application_answer_cache = {}
        for _ in range(15):
            _raise_if_limited(driver, 'application_form')
            _pause_checkpoint(pause_controller, validate=lambda: bool(_modal(driver)))
            modal = _modal(driver)
            if not modal: raise CannotCompleteApplication('The application dialog closed unexpectedly.')
            page = _progress(modal)
            body = modal.text
            _verify_work_experience(modal, registry)
            if re.search(r'\bResume\*', body) and 'Upload resume' in body:
                _ensure_resume(driver, modal, resume_path)
                if audit: audit.field('Resume', Path(resume_path).name, 'selected', page)
                modal = _modal(driver)
            for field_attempt in range(2):
                try:
                    modal = _modal(driver)
                    if not modal:
                        raise CannotCompleteApplication('The application dialog closed unexpectedly.')
                    answer_cache = application_answer_cache
                    _fill_fields(driver, modal, profile, questions, email, log, registry, audit, page, job_id,
                                 ai_client, candidate_context, job_description, about_company, ai_model,
                                 company=company, answer_cache=answer_cache)
                    modal = _modal(driver)
                    _verify_name_fields(modal, registry, audit, page)
                    break
                except StaleElementReferenceException:
                    if field_attempt:
                        raise
                    log('LinkedIn refreshed the application fields mid-fill; reacquiring the form and retrying once.')
                    WebDriverWait(driver, 8, ignored_exceptions=(StaleElementReferenceException,)).until(
                        lambda d: _modal(d) and _progress(_modal(d)) == page)
            submit = [b for b in modal.find_elements(By.TAG_NAME, 'button')
                      if b.text.strip().casefold() == 'submit application']
            if submit:
                _raise_if_limited(driver, 'before_submit')
                _verify_review_name(modal.text, registry)
                _clear_optional_company_follow(driver, modal)
                if Path(resume_path).name.lower() not in modal.text.lower():
                    raise CannotCompleteApplication('The final review does not show the configured resume.')
                if dry_run:
                    _dismiss(driver)
                    return 'review', 'Reached the final review with the configured resume.'
                if pause_before_submit:
                    choice = confirm_before_submit(title=job_title, company=company) if confirm_before_submit else None
                    if choice != 'Submit Application':
                        _dismiss(driver)
                        return 'review', 'Paused at the final review; the application was not submitted.'
                _pause_checkpoint(pause_controller, validate=lambda: bool(_modal(driver)))
                if audit: audit.finish('submitting', 'Submission is being attempted; confirmation is pending.')
                _click(driver, submit[0])
                _raise_if_limited(driver, 'after_submit', 'unconfirmed')
                try:
                    def confirmed(d):
                        for dialog in d.find_elements(By.CSS_SELECTOR, 'dialog[open]'):
                            if not dialog.is_displayed(): continue
                            if _confirmation_matches(dialog.text, company): return True
                        return False
                    WebDriverWait(driver, 15).until(confirmed)
                except TimeoutException as error:
                    _raise_if_limited(driver, 'after_submit', 'unconfirmed')
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
                repaired = _repair_invalid_fields(
                    driver, m, ai_client, candidate_context, job_description,
                    about_company, ai_model, log) if m else 0
                if repaired and m:
                    retry_buttons = [b for b in m.find_elements(By.TAG_NAME, 'button')
                                     if b.text.strip() in ('Next', 'Review') and b.is_displayed()]
                    if len(retry_buttons) == 1:
                        _click(driver, retry_buttons[0])
                        try:
                            WebDriverWait(driver, 10).until(lambda d: _modal(d) and
                                _progress(_modal(d)) is not None and _progress(_modal(d)) != page)
                            continue
                        except TimeoutException:
                            pass
                raise CannotCompleteApplication('The form did not advance' + (': ' + '; '.join(messages[:3]) if messages else '.')) from error
        raise CannotCompleteApplication('The application exceeded the maximum number of steps.')
    except EasyApplyLimitReached:
        raise
    except CannotCompleteApplication as error:
        saved = _dismiss(driver)
        return 'skip', str(error) + (' Draft saved on LinkedIn.' if saved else '')


def apply_current_job(driver, profile, questions, email, resume_path, dry_run, log, company, job_id,
                      registry=None, title='', ai_client=None, candidate_context='',
                      job_description='', about_company='', ai_model='', pause_before_submit=False,
                      confirm_before_submit=None, pause_controller=None):
    audit = ApplicationAudit(job_id, title, company)
    try:
        outcome, message = _apply_current_job(
            driver, profile, questions, email, resume_path, dry_run, log, company, job_id,
            registry, audit, ai_client, candidate_context, job_description, about_company,
            ai_model, pause_before_submit, confirm_before_submit, title, pause_controller)
    except EasyApplyLimitReached as error:
        audit.finish(error.submission_state, 'LinkedIn reported an Easy Apply application limit.')
        raise
    except Exception as error:
        try:
            _raise_if_limited(driver, 'application_failure',
                              'unconfirmed' if audit.entry.get('status') == 'submitting' else 'not_submitted')
        except EasyApplyLimitReached as limit:
            audit.finish(limit.submission_state, 'LinkedIn reported an Easy Apply application limit.')
            raise
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


def _trace_job_iteration(log, message):
    """Temporary positional-list diagnostic; enable with JOB_ITERATION_TRACE=1."""
    if os.environ.get('JOB_ITERATION_TRACE', '').strip().lower() in {'1', 'true', 'yes', 'on'}:
        log('[JOB_ITERATION_TRACE] ' + message)


def _visible_card_ids(cards):
    """Return visible result-card IDs with their positions in the current DOM list."""
    result = []
    for position, card in enumerate(cards):
        try:
            is_displayed = getattr(card, 'is_displayed', None)
            if is_displayed is None or is_displayed():
                result.append((position, _card_job_id(card) or '?'))
        except StaleElementReferenceException:
            result.append((position, '<stale>'))
    return result


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
               attempted_ids=None, submitted_roles=None, profile_matches=False,
               attempted_reasons=None,
               classifier_enabled=False, classifier_client=None, candidate_context='',
               classifier_model='qwen3-vl:4b-instruct', pause_before_submit=False,
               confirm_before_submit=None, pause_controller=None, diagnostics=None):
    """Process current search results, stopping if a submission cannot be confirmed."""
    submitted = skipped = reviewed = 0
    if attempted_ids is None: attempted_ids = set()
    if submitted_roles is None: submitted_roles = set()
    if attempted_reasons is None: attempted_reasons = {}

    def record_rejection(title, company, location, source, reason, job_id=None, description=''):
        nonlocal skipped
        stable_id = str(job_id or ('card:' + hashlib.sha256(
            f'{company}|{title}|{location}'.casefold().encode('utf-8')).hexdigest()[:20]))
        record_suitability_decision(
            stable_id, title, company, location, '',
            {'decision': 'SKIP', 'reason': reason, 'matched_skills': [],
             'missing_skills': [], 'hard_blockers': []},
            decision_source=source, model_decision=None, final_decision='SKIP',
            override_reason='', model_called=False, job_description=description)
        if source in ('ALREADY_SUBMITTED', 'PREVIOUS_ATTEMPT', 'SUBMISSION_UNCERTAIN', 'IN_PROGRESS', 'REVIEW'):
            log(f'{source}: Skipping {title}: {reason}')
        else:
            log(f'Skipping {title}: {reason}')
        _trace_job_iteration(log, f'FILTER job={job_id or stable_id} source={source} reason={reason}')
        skipped += 1
        if diagnostics:
            diagnostics.increment('jobs_skipped')

    _select_easy_apply(driver, log)
    if diagnostics:
        diagnostics.begin_stage('search')
    WebDriverWait(driver, 15).until(lambda d: d.find_elements(By.XPATH, CARDS_XPATH))
    seen = set()
    previous_trace = None
    for page in range(1, max_pages + 1):
        for index in range(100):
            if submitted >= limit: break
            _pause_checkpoint(pause_controller, validate=lambda: bool(driver.current_url))
            try:
                cards = WebDriverWait(driver, 10).until(lambda d: d.find_elements(By.XPATH, CARDS_XPATH))
                current_visible_ids = _visible_card_ids(cards)
                if previous_trace is not None:
                    _trace_job_iteration(
                        log, f"AFTER page={page} processed_index={previous_trace['index']} "
                        f"processed_job={previous_trace['job_id']} visible={current_visible_ids}"
                    )
                if index >= len(cards):
                    _trace_job_iteration(log, f'END page={page} next_index={index} visible={current_visible_ids}')
                    break
                card = cards[index]
                _trace_job_iteration(
                    log, f'BEFORE page={page} visible={current_visible_ids}; '
                    f'NEXT index={index} job={_card_job_id(card) or "?"} reason=positional index'
                )
                title, company, location, lines = _card_details(card)
                hinted_id = _card_job_id(card)
                identity = hinted_id or (title.casefold(), company.casefold(), location.casefold())
                previous_trace = {'index': index, 'job_id': hinted_id or '?'}
                if not title:
                    _trace_job_iteration(log, f'FILTER index={index} job={hinted_id or "?"} source=INVALID_CARD reason=missing title')
                    continue
                if identity in seen:
                    _trace_job_iteration(log, f'FILTER index={index} job={hinted_id or "?"} source=DEDUPLICATION reason=identity already seen')
                    continue
                seen.add(identity)
                if diagnostics:
                    diagnostics.increment('jobs_inspected')
                if hinted_id in applied_ids or any(line.strip().lower() == 'applied' for line in lines):
                    record_rejection(title, company, location, 'ALREADY_SUBMITTED',
                                     'A prior application is confirmed as submitted.', hinted_id)
                    continue
                if hinted_id in attempted_ids:
                    block_source = attempted_reasons.get(hinted_id, 'PREVIOUS_ATTEMPT')
                    if block_source not in ('ALREADY_SUBMITTED', 'SUBMISSION_UNCERTAIN', 'IN_PROGRESS', 'REVIEW'):
                        block_source = 'PREVIOUS_ATTEMPT'
                    block_message = {
                        'ALREADY_SUBMITTED': 'A prior application is confirmed as submitted.',
                        'SUBMISSION_UNCERTAIN': 'A prior submit may have reached LinkedIn; verify Applied jobs before retrying.',
                        'IN_PROGRESS': 'A previous application attempt is still marked in progress.',
                        'REVIEW': 'A previous attempt is held for manual review.',
                    }.get(block_source, 'A prior attempt is blocked by the retry policy.')
                    record_rejection(title, company, location, block_source, block_message, hinted_id)
                    continue
                if profile_matches and find_bad_word(title, ['marketing', 'sales', 'recruiter', 'account manager', 'business development', 'talent acquisition']):
                    record_rejection(title, company, location, 'ROLE_FILTER',
                                     'The role is outside your software engineering search.', hinted_id); continue
                if role_key(company, title) in submitted_roles:
                    record_rejection(title, company, location, 'DUPLICATE_ROLE',
                                     'This company and title have already been submitted.', hinted_id); continue
                if registry is not None and not _within_candidate_location(location, registry.value('relocation_within_india') == 'Yes'):
                    record_rejection(title, company, location, 'LOCATION_FILTER',
                                     'The location is outside the confirmed search area.', hinted_id); continue
                if not require_js_or_ts and find_bad_word(title, ['senior', 'staff', 'principal', 'lead', 'manager', 'director', 'architect']):
                    record_rejection(title, company, location, 'SENIORITY_FILTER',
                                     'The title indicates a senior role.', hinted_id); continue
                if find_bad_word(company, company_bad_words):
                    record_rejection(title, company, location, 'COMPANY_FILTER',
                                     'Company matches a configured exclusion.', hinted_id); continue
                was_selected = any(line.startswith('Selected, ') for line in card.text.splitlines())
                previous_job_id = _selected_job_id_from_url(driver.current_url)
                _pause_checkpoint(pause_controller, validate=lambda: bool(driver.current_url))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click()", card)
                buffer(0)
                # A sidebar job link exists before its details load. Waiting on that
                # link can make us inspect the previous job and miss its Apply button.
                try:
                    job_id = WebDriverWait(driver, 12).until(
                        lambda d: _resolved_selected_job_id(d, hinted_id, previous_job_id, was_selected))
                except TimeoutException as error:
                    raise CannotCompleteApplication(f'Could not verify the selected job details for {title}.') from error
                previous_trace = {'index': index, 'job_id': job_id}
                _trace_job_iteration(log, f'SELECTED index={index} hinted_job={hinted_id or "?"} actual_job={job_id}')
                if job_id in applied_ids:
                    record_rejection(title, company, location, 'ALREADY_SUBMITTED',
                                     'A prior application is confirmed as submitted.', job_id)
                    continue
                if job_id in attempted_ids:
                    block_source = attempted_reasons.get(job_id, 'PREVIOUS_ATTEMPT')
                    if block_source not in ('ALREADY_SUBMITTED', 'SUBMISSION_UNCERTAIN', 'IN_PROGRESS', 'REVIEW'):
                        block_source = 'PREVIOUS_ATTEMPT'
                    block_message = {
                        'ALREADY_SUBMITTED': 'A prior application is confirmed as submitted.',
                        'SUBMISSION_UNCERTAIN': 'A prior submit may have reached LinkedIn; verify Applied jobs before retrying.',
                        'IN_PROGRESS': 'A previous application attempt is still marked in progress.',
                        'REVIEW': 'A previous attempt is held for manual review.',
                    }.get(block_source, 'A prior attempt is blocked by the retry policy.')
                    record_rejection(title, company, location, block_source, block_message, job_id)
                    continue
                try:
                    heading = WebDriverWait(driver, 10).until(lambda d: next(
                        (h for h in d.find_elements(By.XPATH, "//h2[normalize-space(.)='About the job']")
                         if h.is_displayed()), False))
                    description = heading.find_element(By.XPATH, '../..').get_attribute('textContent') or ''
                except TimeoutException:
                    record_rejection(title, company, location, 'DESCRIPTION_FILTER',
                                     'Job description is unavailable.', job_id); continue
                if len(description.strip()) < 80:
                    record_rejection(title, company, location, 'DESCRIPTION_FILTER',
                                     'Job description is incomplete.', job_id, description); continue
                if require_js_or_ts and not re.search(r'\b(?:java[ -]?script|type[ -]?script)\b', title + ' ' + description, re.I):
                    record_rejection(title, company, location, 'LANGUAGE_FILTER',
                                     'The posting does not mention JavaScript or TypeScript.', job_id, description); continue
                company_headings = driver.find_elements(By.XPATH, "//h2[normalize-space(.)='About the company']")
                about = company_headings[0].find_element(By.XPATH, '../..').get_attribute('textContent') if company_headings else ''
                if find_bad_word(about or '', company_bad_words) and not find_bad_word(about or '', company_good_words):
                    record_rejection(title, company, location, 'COMPANY_DESCRIPTION_FILTER',
                                     'Company description matches a configured exclusion.', job_id, description); continue
                bad = find_bad_word(description, bad_words)
                if bad:
                    record_rejection(title, company, location, 'JOB_DESCRIPTION_FILTER',
                                     f'Job description matches configured exclusion {bad}.', job_id, description); continue
                years = _required_years(description)
                if not require_js_or_ts and years and max_years >= 0 and max(years) > max_years:
                    record_rejection(title, company, location, 'EXPERIENCE_FILTER',
                                     f'Posting requires {max(years)} years; configured maximum is {max_years}.',
                                     job_id, description); continue
                suitability_context = candidate_context
                if classifier_enabled:
                    _pause_checkpoint(pause_controller, validate=lambda: bool(driver.current_url))
                    if diagnostics: diagnostics.begin_stage('model')
                    from modules.ai.connections import classify_job_suitability
                    if registry is not None:
                        verified_keys = ('work_authorization', 'visa_sponsorship', 'citizenship',
                                         'relocation', 'relocation_within_india', 'security_clearance',
                                         'education_degree', 'certifications')
                        verified_facts = [f'{key}: {registry.value(key)}' for key in verified_keys
                                          if registry.value(key) is not None]
                        if verified_facts:
                            suitability_context += '\n\nVerified application facts:\n' + '\n'.join(verified_facts)
                    suitability = classify_job_suitability(
                        classifier_client, title, company, location, description, suitability_context)
                    if diagnostics:
                        diagnostics.complete_stage('model')
                        if suitability.get('failure_kind'):
                            diagnostics.event('model_fallback', reason_code='MODEL_FAILED',
                                              failure_kind=suitability['failure_kind'])
                    model_decision = suitability.get('model_decision')
                    override_reason = ''
                    if suitability.get('hard_blockers') and suitability['decision'] == 'APPLY':
                        suitability['decision'] = 'REVIEW'
                        override_reason = 'Reported hard blocker requires human review.'
                    final_decision = suitability['decision']
                    record_suitability_decision(
                        job_id, title, company, location, classifier_model, suitability,
                        decision_source=('QWEN_CLASSIFIER' if model_decision else 'CLASSIFIER_FALLBACK'),
                        model_decision=model_decision, final_decision=final_decision,
                        override_reason=override_reason, model_called=classifier_client is not None,
                        job_description=description)
                    decision = final_decision
                    reason = suitability.get('reason') or 'No reason was returned.'
                    score = suitability.get('match_score', 0)
                    matched = suitability.get('matched_skills') or []
                    missing = suitability.get('missing_skills') or []
                    log(f'Suitability {decision} ({score}/100): {title} at {company}')
                    log(f'Reason: {reason}')
                    if matched: log('Matched: ' + ', '.join(str(item) for item in matched))
                    if missing: log('Missing/unverified: ' + ', '.join(str(item) for item in missing))
                    if override_reason:
                        log(f'Decision override: {model_decision} → {final_decision}: {override_reason}')
                    if decision != 'APPLY':
                        log(f'Skipping {title}: classifier returned {decision}')
                        _trace_job_iteration(log, f'FILTER job={job_id} source=SUITABILITY_CLASSIFIER decision={decision} reason={reason}')
                        skipped += 1
                        if diagnostics: diagnostics.increment('jobs_skipped')
                        continue
                _pause_checkpoint(pause_controller, validate=lambda: bool(driver.current_url))
                log(f'Opening Easy Apply for {title} at {company}.')
                if diagnostics:
                    diagnostics.begin_stage('easy_apply')
                    diagnostics.set_job_url(driver.current_url)
                attempted_ids.add(job_id)
                outcome, message = apply_current_job(
                    driver, profile, questions, email, resume_path, dry_run, log, company,
                    job_id, registry, title, classifier_client, suitability_context,
                    description, about or '', classifier_model, pause_before_submit,
                    confirm_before_submit, pause_controller)
                log(f'{title}: {message}')
                if outcome == 'submitted':
                    on_submitted(job_id, title, company, location, description, max(years) if years else 'Unknown')
                    applied_ids.add(job_id); submitted += 1
                    key = role_key(company, title)
                    if key: submitted_roles.add(key)
                    if diagnostics:
                        diagnostics.increment('jobs_applied')
                        diagnostics.complete_stage('application')
                elif outcome == 'review':
                    reviewed += 1
                    if diagnostics:
                        diagnostics.increment('jobs_skipped')
                        diagnostics.complete_stage('application')
                    break
                else:
                    skipped += 1
                    if diagnostics:
                        diagnostics.increment('jobs_failed')
                        diagnostics.complete_stage('job_processing')
            except EasyApplyLimitReached:
                raise
            except SubmissionUncertain as error:
                raise SubmissionUncertain(f'{title} at {company} (job ID {job_id}): {error}') from error
            except (CannotCompleteApplication, NoSuchElementException, StaleElementReferenceException, TimeoutException, WebDriverException) as error:
                if isinstance(error, NoSuchWindowException) or (
                    isinstance(error, WebDriverException) and
                    any(term in str(error).casefold() for term in
                        ('invalid session id', 'no such window', 'session deleted'))
                ):
                    raise
                detail = str(error).splitlines()
                log(f'Skipping a result because LinkedIn changed the page: {type(error).__name__}: {detail[0] if detail else ""}')
                skipped += 1
                if diagnostics:
                    diagnostics.increment('jobs_failed')
                    diagnostics.complete_stage('job_processing')
                try:
                    _dismiss(driver)
                except CannotCompleteApplication as error:
                    # Do not deliberately discard an application the user asked to save.
                    raise CannotCompleteApplication('Could not confirm the incomplete application was saved; stopping.') from error
        if submitted >= limit or reviewed or page == max_pages: break
        _pause_checkpoint(pause_controller, validate=lambda: bool(driver.current_url))
        if not _next_results_page(driver, page): break
        log(f'Continuing to search results page {page + 1}.')
    if diagnostics:
        diagnostics.complete_stage('search')
    return submitted, skipped, reviewed
