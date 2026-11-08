"""Private, incremental record of fields observed on job applications."""
import json
import csv
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

AUDIT_PATH = Path(__file__).resolve().parent.parent / 'application_field_audit.json'


def _env_limit(name, default):
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


MAX_ERROR_ATTEMPTS = _env_limit('APPLICATION_AUDIT_MAX_ERROR_ATTEMPTS', 3)
MAX_ELIGIBLE_SKIP_ATTEMPTS = _env_limit('APPLICATION_AUDIT_MAX_ELIGIBLE_SKIP_ATTEMPTS', 3)
STALE_IN_PROGRESS_SECONDS = _env_limit('APPLICATION_AUDIT_STALE_IN_PROGRESS_SECONDS', 6 * 60 * 60)

_MISSING_ANSWER_PREFIXES = (
    'No verified answer for required question:', 'Unverified application question:',
    'No verified answer for required choice:', 'No saved answer for required question:',
)


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _missing_answer_question(reason):
    reason = str(reason or '')
    prefix = next((item for item in _MISSING_ANSWER_PREFIXES if reason.startswith(item)), None)
    if not prefix:
        return None
    question = reason[len(prefix):].removesuffix(' Draft saved on LinkedIn.').strip()
    return question.rstrip('.')


def previous_attempt_reasons(path=AUDIT_PATH, registry=None, *, max_error_attempts=None,
                             max_eligible_skip_attempts=None, stale_in_progress_seconds=None,
                             now=None):
    """Return the latest blocking status per job, evaluating status after latest selection.

    ``submitting`` and ``uncertain`` always require manual verification. A stale
    ``in_progress`` record is safe to retry because the submit path persists
    ``submitting`` before clicking LinkedIn's Submit button.
    """
    max_error_attempts = MAX_ERROR_ATTEMPTS if max_error_attempts is None else max(0, int(max_error_attempts))
    max_eligible_skip_attempts = (MAX_ELIGIBLE_SKIP_ATTEMPTS if max_eligible_skip_attempts is None
                                  else max(0, int(max_eligible_skip_attempts)))
    stale_in_progress_seconds = (STALE_IN_PROGRESS_SECONDS if stale_in_progress_seconds is None
                                 else max(0, int(stale_in_progress_seconds)))
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    if data.get('version') != 1 or not isinstance(data.get('applications'), list):
        raise ValueError('Invalid application audit file; cannot safely check previous attempts.')

    rows_by_job = {}
    for position, row in enumerate(data['applications']):
        if not isinstance(row, dict) or not row.get('job_id'):
            continue
        job_id = str(row['job_id'])
        rows_by_job.setdefault(job_id, []).append((position, row))

    blocked = {}
    for job_id, entries in rows_by_job.items():
        # Select the newest attempt first, including review records. JSON order
        # breaks ties and supports older records without timestamps.
        _, latest = max(entries, key=lambda item: (_parse_time(item[1].get('started_at')) or
                                                   datetime.min.replace(tzinfo=timezone.utc), item[0]))
        status = str(latest.get('status') or '').casefold()
        reason = str(latest.get('result') or '')
        history = [row for _, row in entries]
        historical_statuses = {str(row.get('status') or '').casefold() for row in history}

        # These states are irreversible without explicit verification: a later
        # failed/review record must never erase evidence of a sent or possible send.
        if 'submitted' in historical_statuses:
            blocked[job_id] = 'ALREADY_SUBMITTED'
        elif historical_statuses.intersection(('submitting', 'uncertain')):
            blocked[job_id] = 'SUBMISSION_UNCERTAIN'
        elif status == 'in_progress':
            started = _parse_time(latest.get('started_at'))
            if started is None or (now - started).total_seconds() < stale_in_progress_seconds:
                blocked[job_id] = 'IN_PROGRESS'
            # A stale in_progress row never reached the persisted pre-click
            # submitting transition, so it can be retried.
        elif status == 'error':
            errors = sum(str(row.get('status', '')).casefold() == 'error' for row in history)
            if errors >= max_error_attempts:
                blocked[job_id] = 'PREVIOUS_ATTEMPT'
        elif status == 'skip':
            question = _missing_answer_question(reason)
            if question is None:
                blocked[job_id] = 'PREVIOUS_ATTEMPT'
                continue
            has_answer = False
            if registry is not None:
                try:
                    has_answer = registry.answer(question, company=latest.get('company')) not in (None, '')
                except TypeError:
                    try:
                        has_answer = registry.answer(question, job_id=job_id) not in (None, '')
                    except TypeError:
                        has_answer = registry.answer(question) not in (None, '')
            eligible_skips = sum(
                str(row.get('status', '')).casefold() == 'skip' and
                _missing_answer_question(row.get('result')) is not None
                for row in history
            )
            if not has_answer or eligible_skips >= max_eligible_skip_attempts:
                blocked[job_id] = 'PREVIOUS_ATTEMPT'
        elif status == 'not_submitted':
            # LinkedIn's explicit limit response confirmed that no application
            # was sent; a later run may retry after the limit clears.
            continue
        elif status == 'review':
            blocked[job_id] = 'REVIEW'
        else:
            # Keep unknown states conservative until their semantics are known.
            blocked[job_id] = 'PREVIOUS_ATTEMPT'
    return blocked


def previously_attempted_ids(path=AUDIT_PATH, registry=None, **policy):
    """Compatibility helper returning only IDs blocked by current audit policy."""
    return set(previous_attempt_reasons(path, registry, **policy))


def role_key(company, title):
    def clean(value):
        return re.sub(r'[^a-z0-9]+', ' ', str(value).casefold()).strip()
    company, title = clean(company), clean(title)
    return (company, title) if company and company != 'unknown' and title else None


def previously_submitted_roles(history_path, audit_path=AUDIT_PATH):
    roles = set()
    try:
        with open(history_path, encoding='utf-8-sig', newline='') as stream:
            for row in csv.DictReader(stream):
                key = role_key(row.get('Company', ''), row.get('Title', ''))
                if key: roles.add(key)
    except FileNotFoundError:
        pass
    try:
        data = json.loads(Path(audit_path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return roles
    if data.get('version') != 1 or not isinstance(data.get('applications'), list):
        raise ValueError('Invalid application audit file; cannot safely check previous submissions.')
    for row in data['applications']:
        if row.get('status') == 'submitted':
            key = role_key(row.get('company', ''), row.get('job_title', ''))
            if key: roles.add(key)
    return roles


def _now():
    return datetime.now(timezone.utc).isoformat()


def record_suitability_decision(job_id, title, company, location, model, result, path=AUDIT_PATH,
                                *, decision_source='QWEN_CLASSIFIER', model_decision=None,
                                final_decision=None, override_reason='', model_called=True,
                                job_description='', run_id=None):
    """Persist a classifier result or deterministic filter decision."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        data = {'version': 1, 'applications': [], 'suitability_decisions': []}
    if data.get('version') != 1 or not isinstance(data.get('applications'), list):
        raise ValueError('Invalid application audit file; refusing to overwrite it.')
    decisions = data.setdefault('suitability_decisions', [])
    if not isinstance(decisions, list):
        raise ValueError('Invalid suitability decision log; refusing to overwrite it.')
    final_decision = final_decision or result.get('final_decision') or result.get('decision', 'REVIEW')
    model_decision = model_decision if model_decision is not None else result.get('model_decision')
    decisions.append({
        'decision_id': uuid.uuid4().hex,
        'job_id': str(job_id),
        'run_id': run_id,
        'job_title': str(title),
        'company': str(company),
        'location': str(location),
        'model': str(model),
        'decision': final_decision,
        'decision_source': decision_source,
        'model_decision': model_decision,
        'final_decision': final_decision,
        'override_reason': str(override_reason or result.get('override_reason') or ''),
        'model_called': bool(model_called),
        'classifier_failure': result.get('failure_kind'),
        'core_skills_match': result.get('core_skills_match', False),
        'match_score': result.get('match_score'),
        'confidence': result.get('confidence'),
        'matched_skills': result.get('matched_skills', []),
        'missing_skills': result.get('missing_skills', []),
        'experience_gap_years': result.get('experience_gap_years'),
        'hard_blockers': result.get('hard_blockers', []),
        'reason': result.get('reason', ''),
        'job_description': str(job_description or ''),
        'recorded_at': _now(),
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.application-audit-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


class ApplicationAudit:
    def __init__(self, job_id, title, company, path=AUDIT_PATH, run_id=None):
        self.path = Path(path)
        self.entry = {'attempt_id': uuid.uuid4().hex, 'job_id': str(job_id),
                      'job_title': title, 'company': company,
                      'job_url': f'https://www.linkedin.com/jobs/view/{job_id}/',
                      'started_at': _now(), 'status': 'in_progress', 'fields': [],
                      'run_id': run_id}
        self._save()

    def _save(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            data = {'version': 1, 'applications': []}
        if data.get('version') != 1 or not isinstance(data.get('applications'), list):
            raise ValueError('Invalid application audit file; refusing to overwrite it.')
        rows = data['applications']
        for i, row in enumerate(rows):
            if row.get('attempt_id') == self.entry['attempt_id']:
                rows[i] = self.entry
                break
        else:
            rows.append(self.entry)
        fd, tmp = tempfile.mkstemp(prefix='.application-audit-', dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)

    def field(self, label, value, source, page=None):
        # Application answers may contain personal information, but credentials
        # and tokens should never be recorded, even if a form exposes them.
        if re.search(r'password|passcode|secret|api.?key|access.?token|auth(?:entication)? token', str(label), re.I):
            return
        item = {'label': str(label), 'value': str(value), 'source': source,
                'page': page, 'recorded_at': _now()}
        self.entry['fields'].append(item)
        self._save()

    def finish(self, status, message):
        self.entry.update(status=status, result=message, finished_at=_now())
        self._save()
