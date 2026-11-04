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


def previously_attempted_ids(path=AUDIT_PATH, registry=None):
    """Block repeats, except a skipped form whose missing answer is now verified."""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return set()
    if data.get('version') != 1 or not isinstance(data.get('applications'), list):
        raise ValueError('Invalid application audit file; cannot safely check previous attempts.')
    latest = {}
    for row in data['applications']:
        if row.get('job_id') and row.get('status') != 'review':
            latest[str(row['job_id'])] = row
    blocked = set()
    prefixes = ('No verified answer for required question:', 'Unverified application question:',
                'No verified answer for required choice:', 'No saved answer for required question:')
    for job_id, row in latest.items():
        reason = str(row.get('result') or '')
        if (job_id == '4485704953' and row.get('status') == 'skip' and registry is not None and
                registry.value('onsite_india') == 'Yes' and registry.value('commute_pluswealth_gurugram') == 'Yes'):
            continue
        prefix = next((p for p in prefixes if reason.startswith(p)), None)
        if row.get('status') == 'skip' and prefix and registry is not None:
            question = reason[len(prefix):].removesuffix(' Draft saved on LinkedIn.').strip().rstrip('.')
            if registry.answer(question) is not None: continue
        blocked.add(job_id)
    return blocked


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
        if row.get('status') in ('submitted', 'submitting', 'uncertain'):
            key = role_key(row.get('company', ''), row.get('job_title', ''))
            if key: roles.add(key)
    return roles


def _now():
    return datetime.now(timezone.utc).isoformat()


def record_suitability_decision(job_id, title, company, location, model, result, path=AUDIT_PATH,
                                *, decision_source='QWEN_CLASSIFIER', model_decision=None,
                                final_decision=None, override_reason='', model_called=True,
                                job_description=''):
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
    def __init__(self, job_id, title, company, path=AUDIT_PATH):
        self.path = Path(path)
        self.entry = {'attempt_id': uuid.uuid4().hex, 'job_id': str(job_id),
                      'job_title': title, 'company': company,
                      'job_url': f'https://www.linkedin.com/jobs/view/{job_id}/',
                      'started_at': _now(), 'status': 'in_progress', 'fields': []}
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
