"""Support LinkedIn's current AI-powered job search and Easy Apply screens.

The older LinkedIn page and its form markup are still handled by runAiBot.py.
This adapter uses accessible names rather than LinkedIn's generated CSS classes.
"""
from __future__ import annotations

import os
import re
import time
import hashlib
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from selenium.common.exceptions import (ElementClickInterceptedException, NoSuchElementException, NoSuchWindowException,
                                        StaleElementReferenceException, TimeoutException,
                                        WebDriverException)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select, WebDriverWait
from modules.application_audit import ApplicationAudit, role_key, record_suitability_decision
from modules import application_diagnostics, run_diagnostics
from modules import diagnostic_execution
from modules.application_answers import (AnswerValidationError, answer_prompt_metadata,
                                         parse_proposal, validate_proposal)
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


class ApplicationCleanupIncomplete(CannotCompleteApplication):
    """Cleanup could not be completed safely; leave the current browser state intact."""


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


def _set_attempt_diagnostic_context(audit, phase, operation, page=None, *,
                                   locator_strategy=None, selector=None,
                                   wait_timeout_seconds=None, wait_predicate=None,
                                   field_label=None, observation=None):
    if audit is None:
        return
    audit.diagnostic_phase = phase
    audit.diagnostic_last_operation = operation
    if page is not None:
        audit.diagnostic_page_index = page
    audit.diagnostic_locator_strategy = locator_strategy
    audit.diagnostic_selector = selector
    audit.diagnostic_wait_timeout_seconds = wait_timeout_seconds
    audit.diagnostic_wait_predicate = wait_predicate
    audit.diagnostic_field_label = field_label
    if observation is not None:
        audit.diagnostic_observation = observation


def _diagnostic_wait(driver, audit, timeout, predicate, *, phase, operation,
                     predicate_name, locator_strategy=None, selector=None, page=None,
                     ignored_exceptions=()):
    """Run an existing wait unchanged while retaining its precise predicate metadata."""
    _set_attempt_diagnostic_context(
        audit, phase, operation, page, locator_strategy=locator_strategy,
        selector=selector, wait_timeout_seconds=timeout, wait_predicate=predicate_name)
    try:
        wait = (WebDriverWait(driver, timeout, ignored_exceptions=ignored_exceptions)
                if ignored_exceptions else WebDriverWait(driver, timeout))
        return wait.until(predicate)
    except Exception as error:
        _record_attempt_exception(audit, driver, error)
        raise


def _redact_attempt_values(value, audit):
    entry = getattr(audit, 'entry', {})
    text = str(value or '')
    job_id = str(entry.get('job_id') or '')
    placeholder = '__APPLICATION_JOB_ID__'
    if job_id:
        text = text.replace(job_id, placeholder)
    text = run_diagnostics._safe_text(text)
    if job_id:
        text = text.replace(placeholder, job_id)
    values = [str(item.get('value') or '') for item in entry.get('fields', [])]
    values.extend(str(value or '') for value in getattr(audit, 'diagnostic_sensitive_values', []))
    for sensitive in sorted((item for item in values if len(item) >= 3), key=len, reverse=True):
        text = re.sub(re.escape(sensitive), '[REDACTED_FORM_VALUE]', text, flags=re.I)
    return text


def _record_attempt_exception_impl(audit, driver, error, *, phase=None, operation=None, page=None):
    if audit is None:
        return
    seen = getattr(audit, '_diagnostic_exception_ids', None)
    if seen is None:
        seen = audit._diagnostic_exception_ids = set()
    if id(error) in seen:
        return
    seen.add(id(error))

    try:
        current_url = getattr(driver, 'current_url', None) if driver is not None else None
    except Exception:
        current_url = None
    try:
        modal_exists = bool(_modal(driver)) if driver is not None else None
    except Exception:
        modal_exists = None
    entry = getattr(audit, 'entry', {})
    run_id = entry.get('run_id')
    traceback_text = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
    event = {
        'run_id': run_id,
        'attempt_id': entry.get('attempt_id'),
        'job_id': entry.get('job_id'),
        'job_title': entry.get('job_title', ''),
        'company': entry.get('company', ''),
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'exception_class': type(error).__name__,
        'exception_message': _redact_attempt_values(str(error), audit),
        'traceback': _redact_attempt_values(traceback_text, audit),
        'current_url': _redact_attempt_values(current_url, audit) if current_url else None,
        'phase': phase or getattr(audit, 'diagnostic_phase', 'OPENING_JOB'),
        'form_page_index': page if page is not None else getattr(audit, 'diagnostic_page_index', None),
        'easy_apply_modal_exists': modal_exists,
        'last_operation': operation or getattr(audit, 'diagnostic_last_operation', 'unknown'),
        'locator_strategy': getattr(audit, 'diagnostic_locator_strategy', None),
        'selector': getattr(audit, 'diagnostic_selector', None),
        'wait_timeout_seconds': getattr(audit, 'diagnostic_wait_timeout_seconds', None),
        'wait_predicate': getattr(audit, 'diagnostic_wait_predicate', None),
        'field_label': getattr(audit, 'diagnostic_field_label', None),
        'observation': getattr(audit, 'diagnostic_observation', None),
        'run_id_source': getattr(audit, 'run_id_source', 'explicit' if run_id else 'direct'),
    }
    root = getattr(audit, 'diagnostic_root', run_diagnostics.RUNS_DIR)
    if Path(root) == Path(run_diagnostics.RUNS_DIR):
        application_diagnostics.append_exception_event(event)
    else:
        application_diagnostics.append_exception_event(event, root)


def _record_attempt_exception(audit, driver, error, *, phase=None, operation=None, page=None):
    """Best-effort diagnostics; recorder failures must not replace ``error``."""
    try:
        _record_attempt_exception_impl(audit, driver, error, phase=phase,
                                       operation=operation, page=page)
    except Exception as diagnostic_error:
        entry = getattr(audit, 'entry', {}) if audit is not None else {}
        try:
            sys.stderr.write(
                'Could not persist application diagnostic event '
                f"for attempt {entry.get('attempt_id')}: {type(diagnostic_error).__name__}: {diagnostic_error}\n"
            )
            sys.stderr.flush()
        except Exception:
            pass


def _dialog_observation(driver):
    """Describe dialog controls without recording dialog body or form values."""
    try:
        dialogs = [d for d in driver.find_elements(By.CSS_SELECTOR, 'dialog[open], [role="dialog"]')
                   if d.is_displayed()]
        return [{'testid': d.get_attribute('data-testid'),
                 'header_labels': [h.text.strip()[:120] for h in d.find_elements(By.CSS_SELECTOR, 'h1,h2,h3')],
                 'buttons': [{'text': (b.text or '').strip()[:80],
                              'aria_label': (b.get_attribute('aria-label') or '')[:100]}
                             for b in d.find_elements(By.TAG_NAME, 'button') if b.is_displayed()]}
                for d in dialogs]
    except Exception as error:
        return {'unavailable': type(error).__name__}


def _cleanup_click(driver, element, audit, operation, selector, target='Dismiss/Close control',
                   locator_strategy='xpath'):
    before = _dialog_observation(driver)
    try:
        element.click()
    except Exception as error:
        after = _dialog_observation(driver)
        _set_attempt_diagnostic_context(
            audit, 'CLEANING_UP', operation, locator_strategy=locator_strategy, selector=selector,
            observation={'reason': getattr(audit, 'diagnostic_cleanup_reason', 'application cleanup'),
                         'save_prompt_detected': getattr(audit, 'diagnostic_save_prompt_detected', None),
                         'target': target, 'intercepted': type(error).__name__ == 'ElementClickInterceptedException',
                         'cleanup_complete': False,
                         'overlay_present': len(before) > 1 or len(after) > 1,
                         'overlay_present_before': len(before) > 1,
                         'overlay_present_after': len(after) > 1, 'dialogs_before': before,
                         'dialogs_after': after,
                         'save_prompt_detected_after': any(
                             'save this application' in ' '.join(item.get('header_labels', [])).casefold()
                             for item in after if isinstance(item, dict))})
        _record_attempt_exception(audit, driver, error)
        raise


_SAVE_CONTROL_NAMES = {'save', 'save application', 'save and exit', 'save for later', 'save draft'}


def _cleanup_dialog_state(driver):
    """Classify visible dialogs without logging their body text or form values."""
    dialogs = [d for d in driver.find_elements(By.CSS_SELECTOR, 'dialog[open], [role="dialog"]')
               if d.is_displayed()]
    application_modal = _modal(driver)
    save_prompts, application_dialogs, unknown = [], [], []
    for dialog in dialogs:
        if dialog is application_modal:
            continue
        try:
            headings = [h.text.strip() for h in dialog.find_elements(By.CSS_SELECTOR, 'h1,h2,h3')]
            text = ' '.join(headings) + ' ' + (getattr(dialog, 'text', '') or '')
            if 'save this application' in text.casefold():
                save_prompts.append(dialog)
            elif text.strip().casefold().startswith('apply to '):
                # Some drivers return a new wrapper for the same modal.
                application_dialogs.append(dialog)
                continue
            else:
                unknown.append(dialog)
        except Exception:
            unknown.append(dialog)
    if len(application_dialogs) > 1 or (application_dialogs and application_modal is None):
        unknown.extend(application_dialogs[1:])
        if application_dialogs and application_modal is None and not unknown:
            unknown.extend(application_dialogs)
    return {'application_modal': application_modal, 'application_dialogs': application_dialogs,
            'save_prompts': save_prompts,
            'unknown_dialogs': unknown, 'visible_dialogs': dialogs}


def _cleanup_snapshot(state):
    def describe(dialog):
        try:
            headings = [h.text.strip()[:120] for h in dialog.find_elements(By.CSS_SELECTOR, 'h1,h2,h3')]
            buttons = [{'text': (b.text or '').strip()[:80],
                        'aria_label': (b.get_attribute('aria-label') or '')[:100]}
                       for b in dialog.find_elements(By.TAG_NAME, 'button') if b.is_displayed()]
            return {'header_labels': headings, 'buttons': buttons,
                    'testid': dialog.get_attribute('data-testid')}
        except Exception as error:
            return {'unavailable': type(error).__name__}
    return {'application_modal_present': state['application_modal'] is not None,
            'application_dialog_count': len(state.get('application_dialogs', [])),
            'save_prompt_count': len(state['save_prompts']),
            'unknown_dialog_count': len(state['unknown_dialogs']),
            'dialogs': [describe(dialog) for dialog in state['visible_dialogs']]}


def _cleanup_incomplete(driver, audit, reason, state, *, cause=None, operation='inspect cleanup dialogs'):
    observation = _cleanup_snapshot(state)
    _set_attempt_diagnostic_context(audit, 'CLEANING_UP', operation,
        locator_strategy='css selector', selector='dialog[open], [role="dialog"]',
        observation={'reason': reason, 'cleanup_complete': False, **observation})
    error = ApplicationCleanupIncomplete(reason)
    _record_attempt_exception(audit, driver, error)
    if cause is not None:
        raise error from cause
    raise error


def _save_draft_from_prompt(driver, state, audit, cleanup_reason, *, intercepted_cause=None):
    prompts = state['save_prompts']
    if len(prompts) != 1 or state['unknown_dialogs']:
        _cleanup_incomplete(driver, audit, 'A save dialog was ambiguous or covered by an unknown dialog; leaving the browser open.',
                            state, cause=intercepted_cause)
    prompt = prompts[0]
    buttons = [button for button in prompt.find_elements(By.TAG_NAME, 'button')
               if button.is_displayed() and button.is_enabled()
               and ((button.text or '').strip().casefold() in _SAVE_CONTROL_NAMES
                    or (button.get_attribute('aria-label') or '').strip().casefold() in _SAVE_CONTROL_NAMES)]
    if len(buttons) != 1:
        _cleanup_incomplete(driver, audit, 'The save dialog has no unique, interactable Save control; leaving the browser open.',
                            state, cause=intercepted_cause, operation='locate unique save control')
    _set_attempt_diagnostic_context(audit, 'CLEANING_UP', 'click unique Save control',
        locator_strategy='button text or aria-label', selector='Save / Save application / Save and exit / Save for later / Save draft',
        observation={'reason': cleanup_reason, 'cleanup_action': 'save draft',
                     'cleanup_complete': False, **_cleanup_snapshot(state)})
    if audit is not None:
        audit.diagnostic_cleanup_reason = cleanup_reason
        audit.diagnostic_save_prompt_detected = True
    try:
        _cleanup_click(driver, buttons[0], audit, 'click unique Save control',
                       'Save / Save application / Save and exit / Save for later / Save draft',
                       target='Save draft control', locator_strategy='button text or aria-label')
    except ElementClickInterceptedException as error:
        # Reinspect after interception, but never retry the same Save control.
        after = _cleanup_dialog_state(driver)
        _cleanup_incomplete(driver, audit, 'Clicking Save was intercepted; leaving the current browser state for recovery.',
                            after, cause=error, operation='reinspect after intercepted Save click')

    def prompt_closed(d):
        current = _cleanup_dialog_state(d)
        if current['unknown_dialogs']:
            return {'incomplete': current}
        return True if not current['save_prompts'] else False
    try:
        result = _diagnostic_wait(driver, audit, 5, prompt_closed,
            phase='CLEANING_UP', operation='verify save dialog closed',
            predicate_name='no visible dialog semantically identified as Save this application',
            locator_strategy='css selector', selector='dialog[open], [role="dialog"]')
    except TimeoutException as error:
        after = _cleanup_dialog_state(driver)
        _cleanup_incomplete(driver, audit, 'The Save dialog did not close; leaving the browser open.',
                            after, cause=error, operation='verify save dialog closed')
    if isinstance(result, dict) and 'incomplete' in result:
        _cleanup_incomplete(driver, audit, 'An unknown dialog appeared after saving; leaving the browser open.',
                            result['incomplete'], operation='verify save dialog closure')
    return True


def _confirm_submission_and_restore_search(driver, job_id, company, search_url, audit, log):
    """Verify the exact job first; search restoration is best-effort recovery."""
    _set_attempt_diagnostic_context(audit, 'VERIFYING_SUBMISSION',
                                    'open exact job and verify submitted status')
    try:
        driver.get(f'https://www.linkedin.com/jobs/view/{job_id}/')
        _diagnostic_wait(driver, audit, 15, lambda d: f'/jobs/view/{job_id}' in d.current_url
            and _job_status_confirms(d.find_element(By.TAG_NAME, 'body').text, company),
            phase='VERIFYING_SUBMISSION', operation='verify exact-job application status',
            predicate_name='exact job URL and body confirms submitted status for expected company',
            locator_strategy='tag name', selector='body')
    except (TimeoutException, WebDriverException) as status_error:
        _record_attempt_exception(audit, driver, status_error,
                                  phase='VERIFYING_SUBMISSION',
                                  operation='verify exact-job application status')
        raise SubmissionUncertain(
            'Submit was clicked, but the application status could not be confirmed. Check Applied jobs before retrying.'
        ) from status_error

    # Persist the authoritative result before attempting any unrelated navigation.
    if audit:
        audit.finish('submitted', 'LinkedIn confirmed the application was sent.')
    _set_attempt_diagnostic_context(audit, 'RESTORING_SEARCH', 'restore search results')
    try:
        driver.get(search_url)
        _diagnostic_wait(driver, audit, 15, lambda d: d.find_elements(By.XPATH, CARDS_XPATH),
            phase='RESTORING_SEARCH', operation='wait for search result cards',
            predicate_name='one or more search result cards are present',
            locator_strategy='xpath', selector=CARDS_XPATH)
    except (TimeoutException, WebDriverException) as navigation_error:
        _record_attempt_exception(audit, driver, navigation_error,
                                  phase='RESTORING_SEARCH', operation='wait for search result cards')
        log(f'Application submission was confirmed, but search-page restoration failed: {navigation_error}')
    return 'submitted', 'LinkedIn confirmed the application was sent.'


def _diagnostic_target_card(cards, job_id):
    return diagnostic_execution.unique_target_card(cards, job_id, _card_job_id)


def _diagnostic_should_retry_stale(field_attempt, diagnostic_budget):
    return diagnostic_budget is None and field_attempt == 0


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
                                 job_description, about_company, max_length=None, trace_context=None,
                                 field_metadata=None, evidence_context=None):
    if not _ai_answer_allowed(label, kind, options):
        return None
    if isinstance(field_metadata, dict):
        field_metadata.pop('_answer_rejected', None)
    answer = None
    trace_metadata = dict(trace_context or {})
    metadata = dict(field_metadata or {})
    normalized_kind = ('count' if re.search(r'\bhow many\b|\bnumber of projects\b', label, re.I)
                       else kind)
    if metadata.get('field_type') in ('radio', 'checkbox'):
        normalized_kind = metadata['field_type']
    metadata.update({'question': label, 'field_type': normalized_kind,
                     'required': bool(metadata.get('required', False)),
                     'options': options or metadata.get('options') or [],
                     'maxlength': max_length if max_length is not None else metadata.get('maxlength')})
    q = re.sub(r'[^a-z0-9]+', ' ', label.casefold())
    metadata['numeric'] = bool(metadata.get('numeric') or normalized_kind in ('integer', 'count', 'number')
                               or _question_expects_numeric_answer(label))
    metadata['rating'] = bool(metadata.get('rating') or ('scale' in q and metadata['numeric']))
    metadata['experience_years'] = bool(metadata.get('experience_years') or
                                        _is_skill_duration_question(label))
    if client:
        from modules.ai.connections import answer_question
        question_type = 'single_select' if options else kind
        raw = answer_question(
            client, label, options=options, question_type=question_type,
            job_description=job_description, about_company=about_company,
            user_information_all=candidate_context, trace_context=trace_metadata,
            field_metadata=answer_prompt_metadata(metadata), structured_response=True)
        try:
            answer = validate_proposal(raw, metadata,
                                       candidate_context if evidence_context is None else evidence_context).answer
        except AnswerValidationError as error:
            try:
                proposal_length = len(str(parse_proposal(raw)['answer']))
            except Exception:
                proposal_length = 0
            if metadata.get('maxlength') is not None and proposal_length > int(metadata['maxlength']):
                retry_trace = dict(trace_metadata)
                raw = answer_question(
                    client, label + f' Keep the answer within {metadata["maxlength"]} characters.',
                    options=options, question_type=question_type,
                    job_description=job_description, about_company=about_company,
                    user_information_all=candidate_context, trace_context=retry_trace,
                    field_metadata=answer_prompt_metadata(metadata), structured_response=True)
                trace_metadata.update(retry_trace)
                try:
                    answer = validate_proposal(raw, metadata,
                                               candidate_context if evidence_context is None else evidence_context).answer
                    error = None
                except AnswerValidationError as retry_error:
                    error = retry_error
            if error is not None:
                answer = None
                if isinstance(field_metadata, dict):
                    field_metadata['_answer_rejected'] = True
                trace_id = trace_metadata.get('trace_id')
                if trace_id:
                    try:
                        from modules.ai import trace_store
                        trace_store.append_event(trace_id, 'validation', 'answer_rejected',
                                                 {'reason': str(error), 'field_type': normalized_kind})
                    except Exception:
                        pass
    trace_id = trace_metadata.get('trace_id')
    if trace_id:
        try:
            from modules.ai import trace_store
            trace_store.complete_trace(trace_id, parsed_output={'final_application_answer': answer})
            trace_store.append_event(trace_id, 'workflow', 'final_application_answer', {'answer': answer})
        except Exception:
            pass
    return answer


def _generate_application_selections(client, label, options, candidate_context,
                                     job_description, about_company, trace_context=None):
    """Return only explicitly supported checkbox choices; no defaults or padding."""
    metadata = {'field_type': 'checkbox', 'required': True, 'options': options}
    answer = _generate_application_answer(
        client, label, 'checkbox', options, candidate_context, job_description,
        about_company, trace_context=trace_context,
        field_metadata=metadata)
    if metadata.get('_answer_rejected'):
        raise AnswerValidationError('AI checkbox proposal was rejected; no selection is safe.')
    return [line.strip() for line in str(answer or '').splitlines() if line.strip()]


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


def _fill_city_typeahead(driver, modal, field, profile, audit=None, page=None):
    city = str(profile.get('current_city') or '').strip()
    if not city:
        raise CannotCompleteApplication('The required city is not saved in your profile.')
    descriptor = _field_descriptor(modal, field)
    original_value = (field.get_attribute('value') or '').strip()
    tracked_value = None
    if original_value.casefold() != city.casefold():
        tracked_value = _track_automation_value(audit, descriptor, original_value, city,
                                               'verified candidate data', page)
    for attempt in range(2):
        try:
            modal, field = _reacquire_scalar_field(driver, descriptor, page)
            current_value = (field.get_attribute('value') or '').strip()
            if current_value.casefold() != city.casefold():
                field.click()
                modal, field = _reacquire_scalar_field(driver, descriptor, page)
                field.send_keys(Keys.CONTROL, 'a')
                modal, field = _reacquire_scalar_field(driver, descriptor, page)
                field.send_keys(Keys.BACKSPACE)
                try:
                    _diagnostic_wait(driver, audit, 3,
                        lambda _: not (_reacquire_scalar_field(driver, descriptor, page)[1].get_attribute('value') or '').strip(),
                        phase='FILLING_FIELDS', operation='wait for city field to clear',
                        predicate_name='city typeahead input value is empty',
                        locator_strategy='css selector', selector='input[data-testid="typeahead-input"]',
                        page=page)
                except TimeoutException as error:
                    raise CannotCompleteApplication(
                        'Could not clear the existing LinkedIn location before entering a new one.') from error
                modal, field = _reacquire_scalar_field(driver, descriptor, page)
                field.send_keys(city)
            modal, field = _reacquire_scalar_field(driver, descriptor, page)
            if (field.get_attribute('value') or '').strip().casefold() == city.casefold():
                break
            if attempt == 1:
                raise CannotCompleteApplication('Could not verify the city entered in LinkedIn location.')
        except StaleElementReferenceException:
            modal, field = _reacquire_scalar_field(driver, descriptor, page)
            if (field.get_attribute('value') or '').strip().casefold() == city.casefold():
                break
            if attempt == 1:
                raise
    else:
        raise CannotCompleteApplication('Could not enter the LinkedIn location.')
    def city_choice(_):
        m = _modal(driver)
        if not m: return False
        choices = [e for e in m.find_elements(By.XPATH, './/*[@role="option"]') if e.is_displayed()]
        country = str(profile.get('country') or '').lower()
        return next((e for e in choices if city.lower() in e.text.lower()
                     and (not country or country in e.text.lower())), False)
    try:
        choice = _diagnostic_wait(driver, audit, 8, city_choice,
            phase='FILLING_FIELDS', operation='wait for matching city suggestion',
            predicate_name='visible role=option text matches city and configured country',
            locator_strategy='xpath', selector='.//*[@role="option"]', page=page)
        _click(driver, choice)
        modal, field = _reacquire_scalar_field(driver, descriptor, page)
        actual = (field.get_attribute('value') or '').strip()
        if actual.casefold() != city.casefold():
            raise CannotCompleteApplication('Could not verify selected LinkedIn location value.')
        _verify_browser_acceptance(driver, field, descriptor['label'])
        if tracked_value is not None:
            tracked_value['browser_validated'] = True
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
    # LinkedIn sometimes validates on blur. Fire blur on the element (unlike a
    # tab key, this does not move focus to a potentially destructive control).
    if driver is not None:
        try:
            driver.execute_script('arguments[0].blur();', field)
        except WebDriverException:
            pass
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


def _verify_browser_acceptance(driver, field, label):
    """Require a fresh DOM value plus native and visible validation acceptance."""
    try:
        state = driver.execute_script('''
            const el = arguments[0];
            const parent = el.closest('[data-test-form-element], .fb-dash-form-element') || el.parentElement;
            const errors = parent ? Array.from(parent.querySelectorAll('[role="alert"], [aria-live], .artdeco-inline-feedback'))
              .filter(n => !!(n.offsetWidth || n.offsetHeight || n.getClientRects().length))
              .map(n => (n.innerText || n.textContent || '').trim()).filter(Boolean) : [];
            return {valid: el.willValidate ? el.validity.valid : true,
              message: el.validationMessage || '', invalid: el.getAttribute('aria-invalid') === 'true', errors};
        ''', field)
    except Exception as error:
        raise CannotCompleteApplication(f'Could not inspect browser validation for: {label}.') from error
    if not isinstance(state, dict) or not state.get('valid', False) or state.get('invalid') or state.get('errors'):
        reason = (state.get('message') if isinstance(state, dict) else '') or 'visible field validation error'
        raise CannotCompleteApplication(f'Browser rejected the answer for {label}: {reason}')


def _track_automation_value(audit, descriptor, original, intended, source, page=None):
    if audit is None:
        return None
    entry = {'descriptor': dict(descriptor), 'original': str(original or ''),
             'intended': str(intended or ''), 'source': source,
             'page': page,
             'factual_validated': True, 'browser_validated': False,
             'cleanup_verified': False}
    tracked = getattr(audit, 'automation_answer_values', None)
    if tracked is None:
        tracked = audit.automation_answer_values = []
    tracked.append(entry)
    return entry


def _track_choice_value(audit, label, option_labels, option_types, original, intended, source, page=None):
    return _track_automation_value(
        audit, {'kind': 'choice', 'label': label, 'option_labels': list(option_labels),
                'option_types': list(option_types)}, '\n'.join(original), '\n'.join(intended), source, page)


def _mark_rejected_ai_answer(audit):
    if audit is not None:
        audit.ai_answer_rejected = True


def _cleanup_unverified_automation_values(driver, audit):
    """Restore only bot-mutated values that lack both factual and DOM validation."""
    pending = [item for item in getattr(audit, 'automation_answer_values', [])
               if not (item.get('factual_validated') and item.get('browser_validated'))]
    if not pending:
        return
    for item in pending:
        descriptor = item['descriptor']
        intended = item['original']
        if descriptor.get('kind') == 'choice':
            original_selected = set(intended.splitlines())
            _restore_choice_group(driver, descriptor['label'], descriptor['option_labels'],
                                  descriptor['option_types'], original_selected)
            item['cleanup_verified'] = True
            continue
        modal, field = _reacquire_scalar_field(driver, descriptor)
        if field.tag_name == 'select':
            options = Select(field).options
            if intended:
                matches = [o for o in options if o.text.strip() == intended]
                if len(matches) != 1:
                    raise ApplicationCleanupIncomplete('Could not restore the original dropdown value safely.')
                Select(field).select_by_visible_text(intended)
            else:
                driver.execute_script("arguments[0].selectedIndex = -1; arguments[0].dispatchEvent(new Event('change',{bubbles:true}));", field)
            actual = Select(field).first_selected_option.text.strip()
            if intended and actual != intended:
                raise ApplicationCleanupIncomplete('Could not verify restored dropdown value.')
            if not intended and actual and Select(field).first_selected_option.get_attribute('value'):
                raise ApplicationCleanupIncomplete('Could not verify cleared dropdown value.')
        else:
            _replace_text(field, intended, descriptor['label'], driver)
            _modal_now, field = _reacquire_scalar_field(driver, descriptor)
            if (field.get_attribute('value') or '').strip() != intended:
                raise ApplicationCleanupIncomplete('Could not verify restored application field value.')
        item['cleanup_verified'] = True


def _validate_page_before_next(driver, modal, audit, page):
    """Stop before navigation unless the current page and all bot values are accepted."""
    try:
        state = driver.execute_script('''
          const root = arguments[0];
          const invalid = Array.from(root.querySelectorAll('input,select,textarea'))
            .filter(el => (el.willValidate && !el.validity.valid) || el.getAttribute('aria-invalid') === 'true')
            .map(el => ({label: el.getAttribute('aria-label') || el.name || el.id || 'application field',
                         message: el.validationMessage || 'invalid'}));
          const errors = Array.from(root.querySelectorAll('[role="alert"], [aria-live="assertive"]'))
            .filter(el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length))
            .map(el => (el.innerText || el.textContent || '').trim()).filter(Boolean);
          return {invalid, errors};
        ''', modal)
    except Exception as error:
        try:
            invalid = modal.find_elements(By.CSS_SELECTOR, ':invalid, [aria-invalid="true"]')
            alerts = [item for item in modal.find_elements(By.CSS_SELECTOR, '[role="alert"], [aria-live="assertive"]')
                      if item.is_displayed() and (item.text or '').strip()]
        except Exception:
            raise CannotCompleteApplication('Could not verify current page browser validation.') from error
        state = {'invalid': invalid, 'errors': alerts}
    if not isinstance(state, dict) or state.get('invalid') or state.get('errors'):
        raise CannotCompleteApplication('Current page has browser validation errors; refusing to advance.')
    for item in getattr(audit, 'automation_answer_values', []):
        if item.get('page') != page:
            continue
        if not item.get('factual_validated') or not item.get('browser_validated'):
            raise CannotCompleteApplication('A bot-entered answer on this page is not fully verified.')
        if item['descriptor'].get('kind') == 'choice':
            _modal_now, group = _reacquire_choice_group(driver, item['descriptor']['label'], page)
            actual = '\n'.join(_radio_option_text(group, option, item['descriptor']['label'])
                               for option in group.find_elements(By.XPATH,
                                   './/input[@type="radio" or @type="checkbox"]') if option.is_selected())
        else:
            _modal_now, field = _reacquire_scalar_field(driver, item['descriptor'], page)
            actual = _field_value(field).strip()
        if actual.casefold() != item['intended'].strip().casefold():
            raise CannotCompleteApplication('A bot-entered value changed after validation; refusing to advance.')


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
    _set_attempt_diagnostic_context(audit, 'VALIDATING_ANSWERS', 'collect name fields for validation', page,
        locator_strategy='xpath', selector='.//input|.//textarea')
    fields = modal.find_elements(By.XPATH, './/input|.//textarea')
    separate_middle = any('middle name' in _field_label(modal, field).casefold() for field in fields)
    for field in fields:
        if not field.is_displayed(): continue
        label = _field_label(modal, field)
        _set_attempt_diagnostic_context(audit, 'VALIDATING_ANSWERS', 'verify candidate name field', page,
            locator_strategy='xpath', selector='.//input|.//textarea', field_label=label)
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


def _field_descriptor(modal, field):
    """Keep identifying attributes only; never retain a WebElement past a mutation."""
    descriptor = {
        'label': _field_label(modal, field),
        'tag': str(getattr(field, 'tag_name', '') or '').casefold(),
        'type': str(field.get_attribute('type') or '').casefold(),
        'id': str(field.get_attribute('id') or ''),
        'name': str(field.get_attribute('name') or ''),
        'placeholder': str(field.get_attribute('placeholder') or ''),
        'required': bool(field.get_attribute('required')),
        'maxlength': field.get_attribute('maxlength'),
        'min': field.get_attribute('min'),
        'max': field.get_attribute('max'),
        'pattern': field.get_attribute('pattern'),
    }
    return descriptor


def _answer_metadata(descriptor, required, options=None):
    label = descriptor.get('label') or ''
    q = label.casefold()
    def bound(key):
        value = descriptor.get(key)
        try:
            return float(value) if value not in (None, '') else None
        except (TypeError, ValueError):
            return None
    return {
        'field_type': descriptor.get('type') or descriptor.get('tag') or 'text',
        'required': bool(required),
        'maxlength': descriptor.get('maxlength'),
        'min': bound('min'), 'max': bound('max'),
        'pattern': descriptor.get('pattern') or None,
        'options': list(options or []),
        'numeric': bool(descriptor.get('type') == 'number' or _question_expects_numeric_answer(label)),
        'experience_years': bool('years of experience' in q and 'total years of experience' not in q),
    }


def _current_form_modal(driver, page=None):
    modal = _modal(driver)
    if modal is None:
        raise CannotCompleteApplication('The application dialog is no longer available.')
    if page is not None and _progress(modal) != page:
        raise CannotCompleteApplication('The application form changed pages before the field could be reacquired.')
    return modal


def _reacquire_scalar_field(driver, descriptor, page=None):
    if not descriptor['label'] and not any(descriptor[key] for key in ('id', 'name', 'placeholder')):
        raise CannotCompleteApplication(
            'Cannot safely reacquire an unlabelled application field without a stable identifier.')
    modal = _current_form_modal(driver, page)
    candidates = []
    wanted_label = _application_answer_cache_key(descriptor['label'])[0]
    for field in modal.find_elements(By.XPATH, './/input|.//select|.//textarea'):
        if str(getattr(field, 'tag_name', '') or '').casefold() != descriptor['tag']:
            continue
        if descriptor['type'] and str(field.get_attribute('type') or '').casefold() != descriptor['type']:
            continue
        if _application_answer_cache_key(_field_label(modal, field))[0] == wanted_label:
            candidates.append(field)
    if len(candidates) > 1:
        # Use stable attributes only to disambiguate duplicate labels. A changed
        # generated ID is not required when the accessible label is unique.
        for attribute in ('name', 'placeholder', 'id'):
            expected = descriptor[attribute]
            if not expected:
                continue
            narrowed = [field for field in candidates if str(field.get_attribute(attribute) or '') == expected]
            if narrowed:
                candidates = narrowed
            if len(candidates) == 1:
                break
    if len(candidates) != 1:
        raise CannotCompleteApplication(
            f'Could not uniquely reacquire the application field: {descriptor["label"] or "unlabelled field"}.')
    current = candidates[0]
    if _application_answer_cache_key(_field_label(modal, current))[0] != wanted_label:
        raise CannotCompleteApplication('The reacquired field no longer matches its original label.')
    return modal, current


def _field_value(field):
    if field.tag_name == 'select':
        return Select(field).first_selected_option.text.strip()
    return (field.get_attribute('value') or '').strip()


def _fill_scalar_stably(driver, descriptor, answer, kind, page):
    expected = str(answer).strip()
    for attempt in range(2):
        try:
            _modal_now, field = _reacquire_scalar_field(driver, descriptor, page)
            if kind == 'date':
                _replace_date(field, answer, descriptor['label'])
            else:
                _replace_text(field, answer, descriptor['label'], driver)
            _modal_now, field = _reacquire_scalar_field(driver, descriptor, page)
            actual = _field_value(field).strip()
            if actual.casefold() == expected.casefold():
                _verify_browser_acceptance(driver, field, descriptor['label'])
                return field, actual
            raise CannotCompleteApplication(
                f'Could not verify the entered answer for: {descriptor["label"]}.')
        except StaleElementReferenceException:
            # A mutation may have completed before React replaced the node.
            # Reacquire and inspect state before deciding whether one bounded
            # fresh interaction is still needed.
            if attempt == 1:
                raise
            _modal_now, fresh = _reacquire_scalar_field(driver, descriptor, page)
            if fresh is field:
                # A locator that resolves to the same Selenium handle did not
                # reacquire a replacement node; repeating against it is not a
                # safe stale-element recovery.
                raise
            actual = _field_value(fresh).strip()
            if actual.casefold() == expected.casefold():
                _verify_browser_acceptance(driver, fresh, descriptor['label'])
                return fresh, actual
    raise CannotCompleteApplication(f'Could not fill the application field: {descriptor["label"]}.')


def _select_option_stably(driver, descriptor, option_text, page):
    expected = str(option_text).strip()
    for attempt in range(2):
        try:
            _modal_now, field = _reacquire_scalar_field(driver, descriptor, page)
            matches = [option for option in Select(field).options
                       if option.text.strip() == expected]
            if len(matches) != 1:
                raise CannotCompleteApplication(
                    f'Could not uniquely reacquire the selected option for: {descriptor["label"]}.')
            if Select(field).first_selected_option.text.strip() == expected:
                _verify_browser_acceptance(driver, field, descriptor['label'])
                return field, expected
            Select(field).select_by_visible_text(expected)
            _modal_now, field = _reacquire_scalar_field(driver, descriptor, page)
            actual = Select(field).first_selected_option.text.strip()
            if actual == expected:
                _verify_browser_acceptance(driver, field, descriptor['label'])
                return field, actual
            raise CannotCompleteApplication(f'Could not verify the selected answer for: {descriptor["label"]}.')
        except StaleElementReferenceException:
            if attempt == 1:
                raise
            _modal_now, fresh = _reacquire_scalar_field(driver, descriptor, page)
            if fresh is field:
                raise
            if _field_value(fresh).strip() == expected:
                return fresh, expected
    raise CannotCompleteApplication(f'Could not select the application option for: {descriptor["label"]}.')


def _choice_group_label(group):
    options = group.find_elements(By.XPATH, './/input[@type="radio" or @type="checkbox"]')
    legends = group.find_elements(By.TAG_NAME, 'legend')
    lines = group.text.splitlines()
    return (legends[0].text if legends else
            (group.get_attribute('aria-label') or
             (options[0].get_attribute('aria-label') if options else '') or
             (lines[0] if lines else 'Unlabelled choice'))).strip()


def _reacquire_choice_group(driver, label, page=None):
    modal = _current_form_modal(driver, page)
    wanted = _application_answer_cache_key(label)[0]
    groups = [group for group in modal.find_elements(By.XPATH, './/fieldset|.//*[@role="radiogroup"]')
              if _application_answer_cache_key(_choice_group_label(group))[0] == wanted]
    if len(groups) != 1:
        raise CannotCompleteApplication(f'Could not uniquely reacquire the choice group: {label}.')
    return modal, groups[0]


def _set_choice_option_stably(driver, group_label, option_label, option_type, selected, page=None):
    wanted_option = str(option_label).strip().casefold()
    for attempt in range(2):
        try:
            _modal_now, group = _reacquire_choice_group(driver, group_label, page)
            options = [option for option in group.find_elements(
                By.XPATH, './/input[@type="radio" or @type="checkbox"]')
                if str(option.get_attribute('type') or '').casefold() == option_type]
            matches = [option for option in options
                       if _radio_option_text(group, option, group_label).strip().casefold() == wanted_option]
            if len(matches) != 1:
                raise CannotCompleteApplication(
                    f'Could not uniquely reacquire choice "{option_label}" in: {group_label}.')
            option = matches[0]
            if option.is_selected() == selected:
                return
            field_id = option.get_attribute('id')
            labels = [item for item in group.find_elements(By.TAG_NAME, 'label')
                      if field_id and item.get_attribute('for') == field_id]
            if len(labels) > 1:
                raise CannotCompleteApplication(
                    f'Could not uniquely identify the label for choice "{option_label}".')
            target = labels[0] if labels else option
            try:
                _click(driver, target)
            except StaleElementReferenceException:
                if attempt == 1:
                    raise
                # Retry only if the current modal exposes a replacement
                # option node. A locator resolving to the same stale handle
                # cannot justify repeating the click.
                _modal_now, fresh_group = _reacquire_choice_group(driver, group_label, page)
                fresh_options = [item for item in fresh_group.find_elements(
                    By.XPATH, './/input[@type="radio" or @type="checkbox"]')
                    if str(item.get_attribute('type') or '').casefold() == option_type]
                fresh_matches = [item for item in fresh_options
                                 if _radio_option_text(fresh_group, item, group_label).strip().casefold() == wanted_option]
                if len(fresh_matches) != 1 or fresh_matches[0] is option:
                    raise
                if fresh_matches[0].is_selected() == selected:
                    return
                continue
            _modal_now, fresh_group = _reacquire_choice_group(driver, group_label, page)
            fresh_options = [item for item in fresh_group.find_elements(
                By.XPATH, './/input[@type="radio" or @type="checkbox"]')
                if str(item.get_attribute('type') or '').casefold() == option_type]
            fresh_matches = [item for item in fresh_options
                             if _radio_option_text(fresh_group, item, group_label).strip().casefold() == wanted_option]
            if len(fresh_matches) != 1 or fresh_matches[0].is_selected() != selected:
                raise CannotCompleteApplication(
                    f'Could not verify choice "{option_label}" in: {group_label}.')
            return
        except StaleElementReferenceException:
            if attempt == 1:
                raise
    raise CannotCompleteApplication(f'Could not update choice "{option_label}" in: {group_label}.')


def _restore_choice_group(driver, group_label, option_labels, option_types, selected_labels, page=None):
    for option_label, option_type in zip(option_labels, option_types):
        should_select = option_label in selected_labels
        if option_type != 'radio' or should_select:
            _set_choice_option_stably(driver, group_label, option_label, option_type, should_select, page)
            continue
        _modal_now, group = _reacquire_choice_group(driver, group_label, page)
        options = [option for option in group.find_elements(By.XPATH,
                    './/input[@type="radio" or @type="checkbox"]')
                   if str(option.get_attribute('type') or '').casefold() == 'radio'
                   and _radio_option_text(group, option, group_label).strip().casefold() == option_label.casefold()]
        if len(options) != 1:
            raise ApplicationCleanupIncomplete('Could not uniquely identify a radio choice for cleanup.')
        if options[0].is_selected():
            driver.execute_script('''
              const el = arguments[0];
              const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked').set;
              setter.call(el, false);
              el.dispatchEvent(new Event('input', {bubbles:true}));
              el.dispatchEvent(new Event('change', {bubbles:true}));
            ''', options[0])
            _modal_now, group = _reacquire_choice_group(driver, group_label, page)
            selected_now = [_radio_option_text(group, item, group_label)
                            for item in group.find_elements(By.XPATH,
                                './/input[@type="radio" or @type="checkbox"]') if item.is_selected()]
            if selected_now:
                raise ApplicationCleanupIncomplete('Could not clear a bot-selected radio choice safely.')
    _modal_now, group = _reacquire_choice_group(driver, group_label, page)
    selected = [_radio_option_text(group, option, group_label)
                for option in group.find_elements(By.XPATH,
                    './/input[@type="radio" or @type="checkbox"]') if option.is_selected()]
    if set(selected) != set(selected_labels):
        raise ApplicationCleanupIncomplete('Could not verify the restored choice selection.')


def _fill_fields(driver, modal, profile, questions, email, log, registry=None, audit=None, page=None, job_id=None,
                 ai_client=None, candidate_context='', job_description='', about_company='', ai_model='', company=None,
                 answer_cache=None, title=''):
    answer_cache = answer_cache if answer_cache is not None else {}
    trace_context = {'job_id': job_id, 'job_title': title, 'company': company,
                     'run_id': getattr(audit, 'entry', {}).get('run_id')}
    # A LinkedIn city typeahead does not always carry HTML's required attribute.
    _set_attempt_diagnostic_context(audit, 'FILLING_FIELDS', 'collect city typeahead fields', page,
        locator_strategy='css selector', selector='input[data-testid="typeahead-input"]')
    typeahead_fields = modal.find_elements(By.CSS_SELECTOR, 'input[data-testid="typeahead-input"]')
    typeahead_descriptors = [_field_descriptor(modal, field) for field in typeahead_fields
                             if 'city' in (field.get_attribute('placeholder') or '').lower()]
    typeahead_fields = []
    for descriptor in typeahead_descriptors:
        modal, field = _reacquire_scalar_field(driver, descriptor, page)
        _fill_city_typeahead(driver, modal, field, profile, audit, page)

    _set_attempt_diagnostic_context(audit, 'FILLING_FIELDS', 'collect current form fields', page,
        locator_strategy='xpath', selector='.//input|.//select|.//textarea')
    fields = modal.find_elements(By.XPATH, './/input|.//select|.//textarea')
    descriptors = [_field_descriptor(modal, field) for field in fields]
    has_middle_name_field = any('middle name' in item['label'].casefold() for item in descriptors)
    fields = []  # Drop cached WebElements before any field mutation can rerender the DOM.
    for descriptor in descriptors:
        modal, field = _reacquire_scalar_field(driver, descriptor, page)
        kind = descriptor['type'] or descriptor['tag']
        if kind in ('hidden', 'file', 'radio', 'checkbox'):
            continue
        label = descriptor['label']
        _set_attempt_diagnostic_context(audit, 'FILLING_FIELDS', 'inspect and fill application field', page,
            locator_strategy='xpath', selector='.//input|.//select|.//textarea', field_label=label)
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
        if audit is not None and existing:
            sensitive = getattr(audit, 'diagnostic_sensitive_values', [])
            sensitive.append(existing)
            audit.diagnostic_sensitive_values = sensitive
        key = registry.classify(label) if registry is not None else None
        if registry is not None and registry.unverified(label) and (required or has_value):
            can_infer_answer = (required and
                                _ai_answer_allowed(label, kind))
            if not can_infer_answer:
                registry.record_missing(label)
                raise CannotCompleteApplication(f'Unverified application question: {label or "unlabelled field"}.')
        answer = _answer_for(label, profile, questions, email, registry)
        cache_key = _application_answer_cache_key(label, cache_options)
        # Do not reuse a raw string proposal across fields: each field can have
        # different maxlength, bounds, type, or offered choices and must pass its
        # own validator with the current DOM metadata.
        answer_source = 'verified candidate data' if answer is not None else 'saved profile/configuration'
        if audit is not None and answer is not None:
            sensitive = getattr(audit, 'diagnostic_sensitive_values', [])
            sensitive.append(answer)
            audit.diagnostic_sensitive_values = sensitive
        nonnumeric_hint = None
        if answer is not None and (kind == 'number' or _question_expects_numeric_answer(label)):
            raw_numeric = str(answer).strip()
            numeric = re.fullmatch(
                r'[₹$€£]?\s*(-?\d[\d,]*(?:\.\d+)?)\s*(?:years?|yrs?|months?|days?|lpa|inr|per\s+annum)?',
                raw_numeric, re.I)
            if numeric:
                answer = numeric.group(1).replace(',', '')
            else:
                nonnumeric_hint = raw_numeric
                answer = None
        field_context = str(candidate_context or '')
        if nonnumeric_hint:
            field_context += f'\n\nEarlier saved response to this numeric question (not itself a number): {nonnumeric_hint}'
        if kind in ('text', 'textarea') and _is_skill_summary_question(label) and ai_client is not None:
            _modal_now, field = _reacquire_scalar_field(driver, descriptor, page)
            answer_metadata = _answer_metadata(descriptor, required)
            tailored_skills = _generate_application_answer(
                ai_client,
                label + '. Review the full applicant context, including professional work and personal projects. Create a concise but comprehensive skills list across the candidate\'s demonstrated frontend, backend, databases, AI, cloud, testing, and integration work. Expand shorthand such as JS to JavaScript. Do not reduce the answer to one keyword, and do not claim unsupported proficiency levels or years.',
                kind, None, field_context, job_description, about_company,
                field.get_attribute('maxlength'), trace_context,
                answer_metadata, evidence_context=candidate_context)
            if answer_metadata.get('_answer_rejected'):
                _mark_rejected_ai_answer(audit)
                raise CannotCompleteApplication('The AI skill summary was rejected; no draft answer is safe.')
            if tailored_skills:
                answer = tailored_skills
                answer_source = f'AI tailored ({ai_model})'
        elif (answer and ai_client is not None and _is_narrative_question(label, kind)
              and answer.strip().casefold() not in {'yes', 'no', 'decline'}
              and not re.fullmatch(r'\d+(?:\.\d+)?', answer.strip()) and len(answer.strip()) < 180):
            narrative_context = (field_context + '\n\nSaved draft for this question: ' + answer)
            _modal_now, field = _reacquire_scalar_field(driver, descriptor, page)
            answer_metadata = _answer_metadata(descriptor, required)
            enriched = _generate_application_answer(
                ai_client,
                label + '. Turn the saved draft into a polished, specific answer using the applicant context. Preserve its factual meaning and add only relevant details supported by professional work or projects.',
                kind, None, narrative_context, job_description, about_company,
                field.get_attribute('maxlength'), trace_context,
                answer_metadata, evidence_context=candidate_context)
            if answer_metadata.get('_answer_rejected'):
                _mark_rejected_ai_answer(audit)
                raise CannotCompleteApplication('The AI narrative proposal was rejected; no draft answer is safe.')
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
            modal, field = _reacquire_scalar_field(driver, descriptor, page)
            if field.tag_name == 'select':
                choices = [option.text.strip() for option in Select(field).options
                           if option.is_enabled() and option.text.strip().casefold() not in
                           ('select an option', 'select', 'choose an option')]
            answer_metadata = _answer_metadata(descriptor, required, choices)
            answer = _generate_application_answer(
                ai_client, label, kind, choices, field_context, job_description,
                about_company, field.get_attribute('maxlength'), trace_context,
                answer_metadata, evidence_context=candidate_context)
            if answer_metadata.get('_answer_rejected'):
                _mark_rejected_ai_answer(audit)
                raise CannotCompleteApplication('The AI answer was rejected; no existing draft value is safe to retain.')
            if answer is not None:
                answer_source = f'AI generated ({ai_model})'
        # AI calls and earlier field changes may have rerendered the form.
        # Refresh the current value before deciding whether a mutation is needed.
        modal, field = _reacquire_scalar_field(driver, descriptor, page)
        if field.tag_name == 'select':
            selected_text = Select(field).first_selected_option.text.strip()
            existing = selected_text if selected_text.casefold() not in (
                'select an option', 'select', 'choose an option') else ''
        else:
            existing = (field.get_attribute('value') or '').strip()
        has_value = bool(existing)
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
        tracked_value = _track_automation_value(audit, descriptor, existing, answer, answer_source, page)
        if field.tag_name == 'select':
            _set_attempt_diagnostic_context(audit, 'FILLING_FIELDS', 'select verified option for application field', page,
                locator_strategy='xpath', selector='.//select', field_label=label)
            modal, field = _reacquire_scalar_field(driver, descriptor, page)
            options = Select(field).options
            if answer_source.startswith('AI'):
                exact_matches = [o for o in options if o.is_enabled() and o.text.strip() == str(answer).strip()]
                match = exact_matches[0] if len(exact_matches) == 1 else None
            else:
                match = _matching_choice([o for o in options if o.is_enabled()], answer,
                                         'experience' in label.lower())
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
            field, actual = _select_option_stably(driver, descriptor, match.text, page)
            _verify_browser_acceptance(driver, field, label)
        else:
            modal, field = _reacquire_scalar_field(driver, descriptor, page)
            max_length = field.get_attribute('maxlength')
            if max_length and str(max_length).isdigit() and len(str(answer)) > int(max_length):
                if registry is not None: registry.record_missing(label)
                raise CannotCompleteApplication(f'The verified answer is too long for: {label}.')
            if kind == 'date':
                _set_attempt_diagnostic_context(audit, 'FILLING_FIELDS', 'enter date in application field', page,
                    locator_strategy='xpath', selector='.//input|.//textarea', field_label=label)
                field, actual = _fill_scalar_stably(driver, descriptor, answer, 'date', page)
            else:
                _set_attempt_diagnostic_context(audit, 'FILLING_FIELDS', 'replace text in application field', page,
                    locator_strategy='xpath', selector='.//input|.//textarea', field_label=label)
                field, actual = _fill_scalar_stably(driver, descriptor, answer, kind, page)
        if tracked_value is not None:
            tracked_value['browser_validated'] = True
        if audit and kind != 'password': audit.field(label, actual, answer_source, page)
        buffer(0)
        log(f'Filled {"an AI drafted" if answer_source.startswith("AI") else "a saved"} answer for: {label}')

    # Store choice descriptors, not groups/options, then reacquire each group
    # immediately before reading or mutating it.
    _set_attempt_diagnostic_context(audit, 'FILLING_FIELDS', 'collect radio and checkbox groups', page,
        locator_strategy='xpath', selector='.//fieldset|.//*[@role="radiogroup"]')
    modal = _current_form_modal(driver, page) if driver is not None else modal
    group_descriptors = []
    for group in modal.find_elements(By.XPATH, './/fieldset|.//*[@role="radiogroup"]'):
        options = group.find_elements(By.XPATH, './/input[@type="radio" or @type="checkbox"]')
        if not options:
            continue
        label = _choice_group_label(group)
        lines = group.text.splitlines()
        try:
            parent_lines = group.find_element(By.XPATH, '..').text.splitlines()
        except (NoSuchElementException, StaleElementReferenceException):
            parent_lines = []
        option_labels = [_radio_option_text(group, option, label) for option in options]
        option_types = [str(option.get_attribute('type') or '').casefold() for option in options]
        selected = [option.is_selected() for option in options]
        required = ('*' in label or any(line.strip().endswith('*') for line in lines) or
                    str(group.get_attribute('aria-required') or '').casefold() == 'true' or
                    bool(group.get_attribute('required')) or
                    any(option.get_attribute('required') for option in options) or
                    any(line.strip().rstrip('*').strip().casefold() == label.casefold() and
                        line.strip().endswith('*') for line in parent_lines))
        group_descriptors.append({'label': label, 'lines': lines, 'option_labels': option_labels,
                                  'option_types': option_types, 'selected': selected,
                                  'required': required})

    for descriptor in group_descriptors:
        label = descriptor['label']
        required = descriptor['required']
        option_labels = descriptor['option_labels']
        option_types = descriptor['option_types']
        selected = descriptor['selected']
        is_checkbox_group = 'checkbox' in option_types
        if 'resume' in label.lower():
            continue  # verified separately by _ensure_resume
        _set_attempt_diagnostic_context(audit, 'FILLING_FIELDS', 'inspect and fill choice group', page,
            locator_strategy='xpath', selector='.//fieldset|.//*[@role="radiogroup"]', field_label=label)
        if registry is not None and registry.unverified(label) and (required or any(selected)):
            can_infer_choice = (required and not is_checkbox_group and
                                _ai_answer_allowed(label, 'single_select', option_labels))
            if not can_infer_choice:
                registry.record_missing(label)
                raise CannotCompleteApplication('Unverified application question: ' + label)
        if is_checkbox_group:
            if required:
                instruction_lines = [line.strip() for line in descriptor['lines']
                                     if line.strip().endswith('*') and line.strip() != label]
                selection_question = ' '.join([label, *instruction_lines]).strip()
                verified_choice = registry.answer(selection_question, job_id=job_id, company=company) if registry is not None else None
                if not verified_choice:
                    if registry is not None: registry.record_missing(label)
                    raise CannotCompleteApplication('A required checkbox group needs a verified answer: ' + label)
                chosen = [item.strip() for item in str(verified_choice).split(',') if item.strip()]
                if not chosen or any(not any(item.casefold() == option.casefold() for option in option_labels) for item in chosen):
                    if registry is not None: registry.record_missing(label)
                    raise CannotCompleteApplication('No unambiguous verified options for: ' + label[:180])
                chosen_set = {item.casefold() for item in chosen}
                original_labels = [option_label for option_label, was_selected in
                                   zip(option_labels, selected) if was_selected]
                tracked_choice = _track_choice_value(
                    audit, label, option_labels, option_types, original_labels,
                    [option_label for option_label in option_labels
                     if option_label.casefold() in chosen_set], 'verified candidate data', page)
                for option_label, option_type in zip(option_labels, option_types):
                    should_select = option_label.casefold() in chosen_set
                    _set_attempt_diagnostic_context(audit, 'FILLING_FIELDS', 'select verified checkbox option', page,
                        locator_strategy='xpath', selector='.//input[@type="checkbox"]', field_label=label)
                    _set_choice_option_stably(driver, label, option_label, option_type, should_select, page)
                _modal_now, fresh_group = _reacquire_choice_group(driver, label, page)
                fresh_options = fresh_group.find_elements(By.XPATH, './/input[@type="radio" or @type="checkbox"]')
                selected_labels = [_radio_option_text(fresh_group, option, label)
                                   for option in fresh_options if option.is_selected()]
                if not selected_labels:
                    raise CannotCompleteApplication('Could not select any options for: ' + label[:180])
                if tracked_choice is not None:
                    tracked_choice['browser_validated'] = True
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
        # Model choices are not reused from a string cache; each group is
        # validated against its own available options and candidate evidence.
        answer_source = 'verified candidate data'
        if required and (answer is None or not str(answer).strip()):
            answer_metadata = {'field_type': 'radio', 'required': required, 'options': option_labels}
            answer = _generate_application_answer(
                ai_client, label, 'single_select', option_labels, candidate_context,
                job_description, about_company, trace_context=trace_context,
                field_metadata=answer_metadata)
            if answer_metadata.get('_answer_rejected'):
                _mark_rejected_ai_answer(audit)
                raise CannotCompleteApplication('The AI choice was rejected; refusing to retain a draft value.')
            if answer is not None:
                answer_source = f'AI generated ({ai_model})'
        if required and answer is None and registry is not None:
            registry.record_missing(label)
            raise CannotCompleteApplication('No verified answer for required choice: ' + label[:180])
        if required and answer is not None:
            expected = str(answer).strip().casefold()
            matched = [text for text in option_labels if text.casefold() == expected]
            if not matched and expected in ('yes', 'no'):
                matched = [text for text in option_labels
                           if re.match(r'^' + expected + r'(?:\b|[,;])', text, re.I)]
            if len(matched) != 1:
                if registry is not None: registry.record_missing(label)
                raise CannotCompleteApplication('No unambiguous option for required choice: ' + label[:180])
            original_labels = [option_label for option_label, was_selected in
                               zip(option_labels, selected) if was_selected]
            tracked_choice = _track_choice_value(
                audit, label, option_labels, option_types, original_labels,
                [matched[0]], answer_source, page)
            _set_attempt_diagnostic_context(audit, 'FILLING_FIELDS', 'select verified radio option', page,
                locator_strategy='xpath', selector='.//input[@type="radio"]', field_label=label)
            _set_choice_option_stably(driver, label, matched[0], 'radio', True, page)
            if tracked_choice is not None:
                tracked_choice['browser_validated'] = True
            if audit: audit.field(label, matched[0], answer_source, page)
        elif required and not any(selected) and answer is None:
            raise CannotCompleteApplication('A required choice needs an answer: ' + label[:180])
    modal = _current_form_modal(driver, page) if driver is not None else modal
    grouped_checkbox_ids = set()
    for group in modal.find_elements(By.XPATH, './/fieldset|.//*[@role="radiogroup"]'):
        if any(str(option.get_attribute('type') or '').casefold() == 'checkbox'
               for option in group.find_elements(By.XPATH, './/input[@type="radio" or @type="checkbox"]')):
            grouped_checkbox_ids.update(str(option.get_attribute('id') or '')
                                        for option in group.find_elements(
                                            By.XPATH, './/input[@type="radio" or @type="checkbox"]')
                                        if option.get_attribute('id'))
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
    modal = _current_form_modal(driver, page) if driver is not None else modal
    required_radios = modal.find_elements(By.XPATH, './/input[@required and @type="radio"]')
    all_radios = modal.find_elements(By.XPATH, './/input[@type="radio"]') if required_radios else []
    for field in required_radios:
        name = field.get_attribute('name')
        group = [other for other in all_radios if other.get_attribute('name') == name] if name else [field]
        if not any(option.is_selected() for option in group):
            if registry is not None: registry.record_missing(_field_label(modal, field) or 'Required radio choice')
            raise CannotCompleteApplication('A required radio choice needs your answer.')


def _repair_invalid_fields(driver, modal, ai_client, candidate_context,
                          job_description, about_company, ai_model, log, trace_context=None):
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
                job_description, about_company, field.get_attribute('maxlength'), trace_context,
                _answer_metadata(_field_descriptor(modal, field), True, options))
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


def _ensure_resume(driver, modal, resume_path, audit=None, page=None):
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
        _diagnostic_wait(driver, audit, 20, selected,
            phase='INSPECTING_FORM', operation='wait for configured resume selection',
            predicate_name='configured resume filename is visible and its radio is selected',
            locator_strategy='css selector', selector='input[type="radio"]', page=page)
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


def _dismiss(driver, audit=None, cleanup_reason='application cleanup'):
    _set_attempt_diagnostic_context(audit, 'CLEANING_UP', 'inspect dialogs before application cleanup',
        locator_strategy='css selector', selector='dialog[open], [role="dialog"]',
        observation={'reason': cleanup_reason, 'cleanup_complete': False})
    if audit is not None:
        audit.diagnostic_cleanup_reason = cleanup_reason
    try:
        state = _cleanup_dialog_state(driver)
    except Exception as error:
        _set_attempt_diagnostic_context(audit, 'CLEANING_UP', 'inspect dialogs before application cleanup',
            locator_strategy='css selector', selector='dialog[open], [role="dialog"]',
            observation={'reason': cleanup_reason, 'cleanup_complete': False,
                         'dialog_inspection_error': type(error).__name__})
        incomplete = ApplicationCleanupIncomplete('Could not inspect dialogs safely; leaving the browser open.')
        _record_attempt_exception(audit, driver, incomplete)
        raise incomplete from error
    if state['unknown_dialogs'] or len(state['save_prompts']) > 1:
        _cleanup_incomplete(driver, audit,
            'An unknown or ambiguous dialog blocks safe application cleanup; leaving the browser open.',
            state)
    if state['save_prompts']:
        if getattr(audit, 'ai_answer_rejected', False):
            _cleanup_incomplete(driver, audit,
                'An AI answer was rejected; refusing to preserve this application as a draft.',
                state, operation='refuse draft save after rejected AI answer')
        return _save_draft_from_prompt(driver, state, audit, cleanup_reason)
    modal = state['application_modal']
    if modal is None:
        return False

    # Reacquire after classifying the UI. Never reuse the earlier modal handle.
    modal = _modal(driver)
    controls = [button for button in (modal.find_elements(
        By.XPATH, './/button[@aria-label="Dismiss" or @aria-label="Close"]') if modal else [])
        if button.is_displayed() and button.is_enabled()]
    if len(controls) != 1:
        state = _cleanup_dialog_state(driver)
        _cleanup_incomplete(driver, audit,
            'The application modal has no unique visible and enabled Dismiss control; leaving the browser open.',
            state, operation='locate unique application Dismiss control')

    _set_attempt_diagnostic_context(audit, 'CLEANING_UP', 'click unique application Dismiss control',
        locator_strategy='xpath', selector='.//button[@aria-label="Dismiss" or @aria-label="Close"]',
        observation={'reason': cleanup_reason, 'cleanup_action': 'dismiss application modal',
                     'cleanup_complete': False, **_cleanup_snapshot(state)})
    if audit is not None:
        audit.diagnostic_cleanup_reason = cleanup_reason
        audit.diagnostic_save_prompt_detected = False
    try:
        _cleanup_click(driver, controls[0], audit, 'click unique application Dismiss control',
                       './/button[@aria-label="Dismiss" or @aria-label="Close"]')
    except ElementClickInterceptedException as error:
        # Inspect the new state and recover only if a unique, known Save dialog
        # appeared. The covered Dismiss control is never retried.
        state = _cleanup_dialog_state(driver)
        if len(state['save_prompts']) == 1 and not state['unknown_dialogs']:
            if getattr(audit, 'ai_answer_rejected', False):
                _cleanup_incomplete(driver, audit,
                    'An AI answer was rejected; refusing to preserve this application as a draft.',
                    state, cause=error, operation='refuse intercepted draft save after rejected AI answer')
            return _save_draft_from_prompt(driver, state, audit, cleanup_reason,
                                           intercepted_cause=error)
        _cleanup_incomplete(driver, audit,
            'The Dismiss click was intercepted and no unique known Save dialog can safely resolve it.',
            state, cause=error, operation='reinspect after intercepted Dismiss click')

    def modal_closed_or_dialog(d):
        current = _cleanup_dialog_state(d)
        if current['unknown_dialogs'] or len(current['save_prompts']) > 1:
            return {'incomplete': current}
        if current['save_prompts']:
            return {'save_prompt': current}
        return True if current['application_modal'] is None else False
    try:
        result = _diagnostic_wait(driver, audit, 5, modal_closed_or_dialog,
            phase='CLEANING_UP', operation='verify application modal closure',
            predicate_name='application modal closed or a semantic Save this application dialog appeared',
            locator_strategy='css selector', selector='dialog[open], [role="dialog"]')
    except TimeoutException as error:
        state = _cleanup_dialog_state(driver)
        _cleanup_incomplete(driver, audit,
            'The application modal did not close after Dismiss; leaving the browser open.',
            state, cause=error, operation='verify application modal closure')
    if isinstance(result, dict) and 'incomplete' in result:
        _cleanup_incomplete(driver, audit,
            'An unknown or ambiguous dialog appeared during cleanup; leaving the browser open.',
            result['incomplete'])
    if isinstance(result, dict) and 'save_prompt' in result:
        return _save_draft_from_prompt(driver, result['save_prompt'], audit, cleanup_reason)
    return False


def _dismiss_preserving_application_error(driver, audit, application_error):
    """Record cleanup failure separately and keep the originating application error primary."""
    _record_attempt_exception(audit, driver, application_error)
    try:
        _cleanup_unverified_automation_values(driver, audit)
        return _dismiss(driver, audit)
    except Exception as cleanup_error:
        _record_attempt_exception(audit, driver, cleanup_error,
                                  phase='CLEANING_UP', operation='dismiss incomplete application')
        raise application_error.with_traceback(application_error.__traceback__) from cleanup_error


def _apply_current_job(driver, profile, questions, email, resume_path, dry_run, log, company, job_id,
                       registry=None, audit=None, ai_client=None, candidate_context='',
                       job_description='', about_company='', ai_model='', pause_before_submit=False,
                       confirm_before_submit=None, job_title='', pause_controller=None,
                       diagnostic_budget=None):
    # The details pane loads asynchronously even after the job URL changes.
    # A visible Easy Apply button may have text but no stable aria-label.
    _set_attempt_diagnostic_context(audit, 'OPENING_JOB', 'resolve selected job and Easy Apply control')
    _raise_if_limited(driver, 'before_easy_apply')
    try:
        buttons = _diagnostic_wait(driver, audit, 12,
            lambda d: _visible_apply_buttons(d)
            if _selected_job_id_from_url(d.current_url) == str(job_id) else False,
            phase='OPENING_JOB', operation='wait for selected job Easy Apply control',
            predicate_name='selected job ID matches and exactly visible enabled Easy Apply controls are queried',
            locator_strategy='xpath', selector=APPLY_XPATH,
            ignored_exceptions=(StaleElementReferenceException,))
    except TimeoutException:
        return 'skip', 'Easy Apply control not found for the selected job after waiting; the panel may still be loading or LinkedIn changed its markup.'
    if len(buttons) != 1:
        return 'skip', 'Multiple application controls matched; refusing to click an ambiguous button.'
    if diagnostic_budget is not None:
        diagnostic_budget.consume(job_id)
    _set_attempt_diagnostic_context(audit, 'OPENING_APPLICATION', 'click Easy Apply control',
        locator_strategy='xpath', selector=APPLY_XPATH)
    _click(driver, buttons[0])
    _raise_if_limited(driver, 'after_easy_apply_click')
    try:
        _raise_if_limited(driver, 'opening_modal')
        _set_attempt_diagnostic_context(audit, 'WAITING_FOR_MODAL', 'wait for application modal and page indicator')
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
        _diagnostic_wait(driver, audit, 15, application_ready,
            phase='WAITING_FOR_MODAL', operation='wait for application modal and page indicator',
            predicate_name='application_ready: visible Apply dialog with page indicator or actionable safety reminder',
            locator_strategy='css selector', selector='dialog[open], [role="dialog"]',
            ignored_exceptions=(StaleElementReferenceException,))
        application_answer_cache = {}
        for _ in range(15):
            _set_attempt_diagnostic_context(audit, 'INSPECTING_FORM', 'inspect current form page')
            _raise_if_limited(driver, 'application_form')
            _pause_checkpoint(pause_controller, validate=lambda: bool(_modal(driver)))
            modal = _modal(driver)
            if not modal: raise CannotCompleteApplication('The application dialog closed unexpectedly.')
            page = _progress(modal)
            _set_attempt_diagnostic_context(audit, 'INSPECTING_FORM', 'read form page and required controls', page)
            body = modal.text
            _verify_work_experience(modal, registry)
            if re.search(r'\bResume\*', body) and 'Upload resume' in body:
                _ensure_resume(driver, modal, resume_path, audit, page)
                if audit: audit.field('Resume', Path(resume_path).name, 'selected', page)
                modal = _modal(driver)
            for field_attempt in range(2):
                try:
                    _set_attempt_diagnostic_context(audit, 'FILLING_FIELDS', 'fill and verify visible form fields', page)
                    modal = _modal(driver)
                    if not modal:
                        raise CannotCompleteApplication('The application dialog closed unexpectedly.')
                    answer_cache = application_answer_cache
                    _fill_fields(driver, modal, profile, questions, email, log, registry, audit, page, job_id,
                                 ai_client, candidate_context, job_description, about_company, ai_model,
                                 company=company, answer_cache=answer_cache, title=job_title)
                    modal = _modal(driver)
                    _verify_name_fields(modal, registry, audit, page)
                    break
                except StaleElementReferenceException as error:
                    # The last field operation/label/locator was set at the point
                    # of interaction; retain it instead of replacing it with a
                    # generic retry-boundary description.
                    _record_attempt_exception(audit, driver, error)
                    if not _diagnostic_should_retry_stale(field_attempt, diagnostic_budget):
                        raise
                    log('LinkedIn refreshed the application fields mid-fill; reacquiring the form and retrying once.')
                    _diagnostic_wait(driver, audit, 8,
                        lambda d: _modal(d) and _progress(_modal(d)) == page,
                        phase='FILLING_FIELDS', operation='wait for current form page after DOM refresh',
                        predicate_name='application modal exists and progress page equals current page',
                        locator_strategy='css selector', selector='dialog[open]', page=page,
                        ignored_exceptions=(StaleElementReferenceException,))
            submit = [b for b in modal.find_elements(By.TAG_NAME, 'button')
                      if b.text.strip().casefold() == 'submit application']
            if submit:
                _set_attempt_diagnostic_context(audit, 'VALIDATING_ANSWERS', 'validate final review and configured resume', page)
                _validate_page_before_next(driver, modal, audit, page)
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
                diagnostic_execution.assert_submission_allowed(diagnostic=diagnostic_budget is not None)
                _set_attempt_diagnostic_context(audit, 'SUBMITTING', 'click Submit Application', page,
                    locator_strategy='button text', selector='Submit Application')
                if audit: audit.finish('submitting', 'Submission is being attempted; confirmation is pending.')
                diagnostic_execution.perform_submission_click(
                    _click, driver, submit[0], diagnostic=diagnostic_budget is not None)
                _set_attempt_diagnostic_context(audit, 'VERIFYING_SUBMISSION', 'wait for LinkedIn submission confirmation', page)
                _raise_if_limited(driver, 'after_submit', 'unconfirmed')
                try:
                    def confirmed(d):
                        for dialog in d.find_elements(By.CSS_SELECTOR, 'dialog[open]'):
                            if not dialog.is_displayed(): continue
                            if _confirmation_matches(dialog.text, company): return True
                        return False
                    _diagnostic_wait(driver, audit, 15, confirmed,
                        phase='VERIFYING_SUBMISSION', operation='wait for confirmation dialog',
                        predicate_name='visible open dialog text confirms application sent for expected company',
                        locator_strategy='css selector', selector='dialog[open]', page=page)
                except TimeoutException as error:
                    _record_attempt_exception(audit, driver, error,
                                              phase='VERIFYING_SUBMISSION',
                                              operation='wait for confirmation dialog', page=page)
                    _raise_if_limited(driver, 'after_submit', 'unconfirmed')
                    # Some LinkedIn screens close the confirmation without naming the
                    # company. Check this exact job's application status before retrying.
                    search_url = driver.current_url
                    outcome, message = _confirm_submission_and_restore_search(
                        driver, job_id, company, search_url, audit, log)
                    return outcome, message
                try:
                    _set_attempt_diagnostic_context(audit, 'VERIFYING_SUBMISSION', 'dismiss confirmation dialog', page)
                    for button in driver.find_elements(By.XPATH, '//button[normalize-space(.)="Not now" or normalize-space(.)="Done"]'):
                        if button.is_displayed():
                            try: button.click()
                            except WebDriverException: pass
                            break
                except WebDriverException as navigation_error:
                    _record_attempt_exception(audit, driver, navigation_error,
                                              phase='VERIFYING_SUBMISSION',
                                              operation='dismiss confirmation dialog', page=page)
                    # The confirmation dialog already established submission.
                    log(f'Application submission was confirmed, but confirmation cleanup failed: {navigation_error}')
                return 'submitted', 'LinkedIn confirmed the application was sent.'
            buttons = [b for b in modal.find_elements(By.TAG_NAME, 'button')
                       if b.text.strip() in ('Next', 'Review') and b.is_displayed()]
            if len(buttons) != 1:
                raise CannotCompleteApplication('Could not find the next step in the application.')
            _set_attempt_diagnostic_context(audit, 'VALIDATING_ANSWERS', 'validate page before advancing', page)
            _validate_page_before_next(driver, modal, audit, page)
            _set_attempt_diagnostic_context(audit, 'ADVANCING_FORM', 'click Next or Review', page,
                locator_strategy='button text', selector='Next or Review')
            _click(driver, buttons[0])
            try:
                _set_attempt_diagnostic_context(audit, 'VALIDATING_ANSWERS', 'wait for form page transition', page)
                _diagnostic_wait(driver, audit, 10, lambda d: _modal(d) and
                    _progress(_modal(d)) is not None and _progress(_modal(d)) != page,
                    phase='ADVANCING_FORM', operation='wait for form page transition',
                    predicate_name='visible application modal progress page is non-null and differs from prior page',
                    locator_strategy='css selector', selector='dialog[open]', page=page)
            except TimeoutException as error:
                _record_attempt_exception(audit, driver, error,
                                          phase='VALIDATING_ANSWERS',
                                          operation='wait for next form page', page=page)
                m = _modal(driver)
                text = m.text if m else ''
                messages = [line.strip() for line in text.splitlines() if 'required' in line.lower() or 'please enter' in line.lower()]
                raise CannotCompleteApplication('The form did not advance' + (': ' + '; '.join(messages[:3]) if messages else '.')) from error
        raise CannotCompleteApplication('The application exceeded the maximum number of steps.')
    except EasyApplyLimitReached:
        raise
    except ApplicationCleanupIncomplete:
        raise
    except CannotCompleteApplication as error:
        saved = _dismiss_preserving_application_error(driver, audit, error)
        return 'skip', str(error) + (' Draft saved on LinkedIn.' if saved else '')


def apply_current_job(driver, profile, questions, email, resume_path, dry_run, log, company, job_id,
                      registry=None, title='', ai_client=None, candidate_context='',
                      job_description='', about_company='', ai_model='', pause_before_submit=False,
                      confirm_before_submit=None, pause_controller=None, diagnostic_budget=None,
                      run_context=None):
    if run_context is None:
        run_context = application_diagnostics.ApplicationRunContext(None)
    elif run_context.run_id is not None:
        run_context = application_diagnostics.run_context_for_id(
            run_context.run_id, run_context.root, source=run_context.source, require_active=True)
    audit = ApplicationAudit(job_id, title, company, run_id=run_context.run_id)
    audit.run_id_source = run_context.source
    audit.diagnostic_root = run_context.root
    audit.diagnostic_page_index = None
    audit._diagnostic_exception_ids = set()
    _set_attempt_diagnostic_context(audit, 'OPENING_JOB', 'begin Easy Apply attempt')
    try:
        outcome, message = _apply_current_job(
            driver, profile, questions, email, resume_path, dry_run, log, company, job_id,
            registry, audit, ai_client, candidate_context, job_description, about_company,
            ai_model, pause_before_submit, confirm_before_submit, title, pause_controller,
            diagnostic_budget)
    except EasyApplyLimitReached as error:
        _record_attempt_exception(audit, driver, error)
        audit.finish(error.submission_state, 'LinkedIn reported an Easy Apply application limit.')
        raise
    except Exception as error:
        _record_attempt_exception(audit, driver, error)
        try:
            _raise_if_limited(driver, 'application_failure',
                              'unconfirmed' if audit.entry.get('status') == 'submitting' else 'not_submitted')
        except EasyApplyLimitReached as limit:
            audit.finish(limit.submission_state, 'LinkedIn reported an Easy Apply application limit.')
            raise
        if audit.entry.get('status') == 'submitting' and not isinstance(error, SubmissionUncertain):
            audit.finish('uncertain', 'Submit was attempted but could not be confirmed.')
            raise SubmissionUncertain('Submit was attempted but could not be confirmed; check LinkedIn before retrying.') from error
        if audit.entry.get('status') == 'submitted':
            raise
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


def _trace_final_classifier_decision(job_id, title, company, classifier_model, suitability,
                                    model_decision, final_decision, override_reason='', run_id=None):
    """Persist final classifier decisions and deterministic overrides to trace history."""
    from modules.ai import trace_store
    recent = trace_store.list_traces(filters={
        'job_id': job_id, 'run_id': run_id, 'category': 'job suitability'}, limit=500)['items']
    matching = [item for item in recent if item.get('job_title') == title
                and item.get('company') == company]
    for item in matching:
        trace_store.correlate_trace(item['trace_id'], decision=final_decision)
        trace_store.append_event(item['trace_id'], 'decision', 'final_decision', {
            'model_decision': model_decision, 'final_decision': final_decision,
            'override_reason': override_reason or None,
        })
    if not matching:
        trace_id = trace_store.create_trace(
            run_id=run_id, job_id=job_id, job_title=title, company=company,
            category='job suitability', model=classifier_model, status='completed',
            retry_info={'attempt': 0, 'mode': 'deterministic_fallback'})
        trace_store.append_event(trace_id, 'workflow', 'classifier_fallback', {
            'failure_kind': suitability.get('failure_kind') or 'MODEL_UNAVAILABLE',
            'reason': suitability.get('reason'),
        })
        trace_store.append_event(trace_id, 'decision', 'final_decision', {
            'model_decision': model_decision, 'final_decision': final_decision,
            'override_reason': override_reason or None,
        })
        trace_store.complete_trace(trace_id, parsed_output=suitability, decision=final_decision)
    return len(matching) or 1


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
               confirm_before_submit=None, pause_controller=None, diagnostics=None,
               diagnostic_job_id=None, diagnostic_budget=None, run_context=None):
    """Process current search results, stopping if a submission cannot be confirmed."""
    submitted = skipped = reviewed = 0
    active_run_id = run_context.run_id if run_context is not None else None
    diagnostic_job_id = str(diagnostic_job_id) if diagnostic_job_id is not None else None
    diagnostic_target_seen = False
    diagnostic_target_processed = False
    if diagnostic_job_id is not None:
        if not diagnostic_job_id.isdigit() or diagnostic_budget is None:
            raise diagnostic_execution.DiagnosticConfigurationError(
                'Diagnostic job isolation requires a numeric job ID and a single-use attempt budget.'
            )
        if diagnostic_budget.job_id != diagnostic_job_id:
            raise diagnostic_execution.DiagnosticConfigurationError(
                'Diagnostic target and attempt budget refer to different jobs.'
            )
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
            override_reason='', model_called=False, job_description=description,
            run_id=active_run_id)
        try:
            from modules.ai import trace_store
            trace_id = trace_store.create_trace(run_id=active_run_id, job_id=job_id or stable_id,
                job_title=title, company=company, category='deterministic job decision',
                provider='', model='', status='completed',
                retry_info={'attempt': 0, 'mode': 'deterministic'})
            trace_store.append_event(trace_id, 'workflow', 'deterministic_filter',
                                     {'source': source, 'reason': reason})
            trace_store.append_event(trace_id, 'decision', 'final_decision',
                                     {'model_decision': None, 'final_decision': 'SKIP', 'override_reason': reason})
            trace_store.complete_trace(trace_id, decision='SKIP')
        except Exception:
            pass
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
    try:
        WebDriverWait(driver, 15).until(lambda d: d.find_elements(By.XPATH, CARDS_XPATH))
    except TimeoutException as error:
        if diagnostic_job_id is not None:
            raise diagnostic_execution.DiagnosticJobNotFound(
                f'No LinkedIn job result cards loaded while locating requested job ID {diagnostic_job_id}.'
            ) from error
        raise
    seen = set()
    previous_trace = None
    for page in range(1, max_pages + 1):
        for index in range(100):
            if submitted >= limit: break
            if diagnostic_job_id is not None and diagnostic_target_processed:
                break
            _pause_checkpoint(pause_controller, validate=lambda: bool(driver.current_url))
            try:
                try:
                    cards = WebDriverWait(driver, 10).until(lambda d: d.find_elements(By.XPATH, CARDS_XPATH))
                except TimeoutException as error:
                    if diagnostic_job_id is not None:
                        break
                    raise error
                if diagnostic_job_id is not None:
                    target_card = _diagnostic_target_card(cards, diagnostic_job_id)
                    if target_card is not None:
                        diagnostic_target_seen = True
                    else:
                        target_card = None
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
                if diagnostic_job_id is not None and card is not target_card:
                    continue
                if diagnostic_job_id is not None:
                    diagnostic_target_processed = True
                _trace_job_iteration(
                    log, f'BEFORE page={page} visible={current_visible_ids}; '
                    f'NEXT index={index} job={_card_job_id(card) or "?"} reason=positional index'
                )
                title, company, location, lines = _card_details(card)
                hinted_id = _card_job_id(card)
                if diagnostic_job_id is not None and hinted_id != diagnostic_job_id:
                    raise diagnostic_execution.DiagnosticJobAmbiguous(
                        f'Selected result did not match requested job {diagnostic_job_id}; refusing to continue.'
                    )
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
                if diagnostic_job_id is not None and str(job_id) != diagnostic_job_id:
                    raise diagnostic_execution.DiagnosticJobAmbiguous(
                        f'LinkedIn selected job {job_id}, not requested job {diagnostic_job_id}; stopping.'
                    )
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
                if not classifier_enabled and not require_js_or_ts and years and max_years >= 0 and max(years) > max_years:
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
                        classifier_client, title, company, location, description, suitability_context,
                        job_id=job_id, run_id=active_run_id)
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
                    try:
                        _trace_final_classifier_decision(
                            job_id, title, company, classifier_model, suitability,
                            model_decision, final_decision, override_reason,
                            active_run_id)
                    except Exception:
                        pass
                    record_suitability_decision(
                        job_id, title, company, location, classifier_model, suitability,
                        decision_source=('QWEN_CLASSIFIER' if model_decision else 'CLASSIFIER_FALLBACK'),
                        model_decision=model_decision, final_decision=final_decision,
                        override_reason=override_reason, model_called=classifier_client is not None,
                        job_description=description, run_id=active_run_id)
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
                    driver, profile, questions, email, resume_path,
                    (True if diagnostic_job_id is not None else dry_run), log, company,
                    job_id, registry, title, classifier_client, suitability_context,
                    description, about or '', classifier_model, pause_before_submit,
                    confirm_before_submit, pause_controller,
                    diagnostic_budget=diagnostic_budget, run_context=run_context)
                try:
                    from modules.ai import trace_store
                    trace_store.correlate_job(job_id, run_id=active_run_id,
                                              application_outcome=outcome)
                    related = trace_store.list_traces(filters={
                        'job_id': job_id, 'run_id': active_run_id}, limit=500)['items']
                    for item in related:
                        trace_store.append_event(item['trace_id'], 'application_outcome', 'application_result',
                                                 {'outcome': outcome})
                except Exception:
                    pass
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
                if diagnostic_job_id is not None:
                    break
            except EasyApplyLimitReached:
                raise
            except SubmissionUncertain as error:
                raise SubmissionUncertain(f'{title} at {company} (job ID {job_id}): {error}') from error
            except (CannotCompleteApplication, NoSuchElementException, StaleElementReferenceException, TimeoutException, WebDriverException) as error:
                if diagnostic_job_id is not None:
                    raise
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
        if diagnostic_job_id is not None and diagnostic_target_seen:
            if not diagnostic_target_processed:
                raise diagnostic_execution.DiagnosticJobAmbiguous(
                    f'Requested job {diagnostic_job_id} appeared outside the processable result range.'
                )
            break
        if submitted >= limit or reviewed or page == max_pages: break
        _pause_checkpoint(pause_controller, validate=lambda: bool(driver.current_url))
        if not _next_results_page(driver, page): break
        log(f'Continuing to search results page {page + 1}.')
    if diagnostics:
        diagnostics.complete_stage('search')
    if diagnostic_job_id is not None and not diagnostic_target_seen:
        raise diagnostic_execution.DiagnosticJobNotFound(
            f'LinkedIn search did not return the requested job ID {diagnostic_job_id}.'
        )
    return submitted, skipped, reviewed
