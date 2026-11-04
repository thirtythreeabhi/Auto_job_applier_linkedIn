"""Private verified candidate answers and deduplicated unanswered questions."""
from __future__ import annotations

import json
import hashlib
import os
import re
import tempfile
import unicodedata
from datetime import date, datetime
from pathlib import Path
from modules.field_mappings import ALIASES

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / 'candidate_answers.json'
UNRESOLVED_PATH = ROOT / 'unresolved_questions.json'


def normalize_question(value):
    value = unicodedata.normalize('NFKC', str(value)).casefold()
    return re.sub(r'\s+', ' ', re.sub(r'[^\w]+', ' ', value)).strip()


QUESTION_ALIASES = {normalize_question(question): key for key, questions in ALIASES.items() for question in questions}


def _today():
    return datetime.now().astimezone().date().isoformat()


def _write_private(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


class CandidateRegistry:
    def __init__(self, path=REGISTRY_PATH, unresolved_path=UNRESOLVED_PATH):
        self.path = Path(path)
        self.unresolved_path = Path(unresolved_path)
        payload = json.loads(self.path.read_text(encoding='utf-8'))
        if payload.get('version') != 1 or not isinstance(payload.get('records'), list):
            raise ValueError('Candidate answer registry is invalid.')
        self.payload = payload
        self.records = {record['key']: record for record in payload['records']}

    def verified(self, key):
        record = self.records.get(key, {})
        return record.get('verification_status') == 'verified' and record.get('answer') not in (None, '')

    def value(self, key):
        return self.records[key]['answer'] if self.verified(key) else None

    def classify(self, question):
        name = normalize_question(question)
        compact = name.replace(' ', '')
        for record in self.records.values():
            if record.get('key', '').startswith('custom:') and record.get('normalized_question') == name:
                return record['key']
        if name in QUESTION_ALIASES:
            return QUESTION_ALIASES[name]
        # These facts are not inferred from the candidate's location or profile.
        if 'notice period' in name:
            if any(phrase in name for phrase in ('immediate joiner', 'remaining days', 'serving notice', 'currently serving')):
                return None
            return 'notice_period_days'
        if 'join' in name and any(x in name for x in ('how soon', 'availability', 'when can', 'earliest')):
            return 'joining_date'
        if any(x in name for x in ('relocat', 'willing to move')):
            return 'relocation_within_india' if 'india' in name else 'relocation'
        if any(x in name for x in ('authorized to work', 'authorised to work', 'work authorization', 'work authorisation', 'employment eligibility', 'right to work')):
            return 'work_authorization'
        if 'sponsorship' in name or 'visa' in name or 'work permit' in name: return 'visa_sponsorship'
        if name in ('nationality', 'what is your nationality', 'your nationality', 'nationality of applicant'):
            return 'nationality'
        if 'citizen' in name or 'nationality' in name: return 'citizenship'
        if 'security clearance' in name or 'clearance level' in name: return 'security_clearance'
        if 'shift' in name or 'night work' in name: return 'shift_willingness'
        if 'certification' in name or 'certified' in name: return 'certifications'
        if 'disabilit' in name: return 'disability'
        if 'veteran' in name or 'military service' in name: return 'veteran'
        if 'gender' in name or 'sexual orientation' in name: return 'gender'
        if 'race' in name or 'ethnicity' in name or 'ethnic' in name: return 'ethnicity'
        if any(x in name for x in ('take home', 'in hand', 'net salary', 'base salary')):
            return 'net_or_base_salary'
        if any(x in name for x in ('salary', 'ctc', 'compensation', 'lpa')):
            if any(x in name for x in ('expected', 'desired', 'expectation')): return 'desired_salary'
            if any(x in name for x in ('current', 'present', 'existing')): return 'current_ctc'
            return None
        if 'additional' in name and 'month' in name and 'experience' in name:
            return 'additional_experience_months'
        has_duration = any(x in name for x in ('year', 'month', 'tenure'))
        has_experience = any(x in name for x in ('experience', 'work', 'using', 'used', 'tenure'))
        if has_duration and has_experience:
            if re.search(r'\b(?:and|or)\b|[,/]', str(question).casefold()):
                if 'nextjs' in compact and 'react' in name: return 'nextjs_experience_years'
                return None
            # A combined stack question requires evidence for that combination,
            # not just the user's general full-stack duration.
            if (('fullstack' in compact or 'full stack' in name) and
                    (re.search(r'\b(?:java|python|react|node|angular|ruby|php|golang|dotnet|aspnet)\b', name)
                     or re.search(r'\bc\s*sharp\b|c\s*#|\.net', str(question).casefold()))):
                return None
            if re.search(r'\bnode(?:\s*js)?\b', name): return 'nodejs_experience_years'
            if 'nextjs' in compact: return 'nextjs_experience_years'
            if re.search(r'\bpython\b', name): return 'python_experience_years'
            if re.search(r'\bmongo\s*db\b', name): return 'mongodb_experience_years'
            if re.search(r'\bangular\s*js\b', name): return 'angularjs_experience_years'
            if re.search(r'\bssis\b|\bsql server integration services\b', name): return 'ssis_experience_years'
            if re.search(r'\bcursor\b|\bclaude\b|ai assisted coding|ai driven development|ai coding assistant', name): return 'ai_coding_assistant_experience_years'
            if re.search(r'\bgen\s*ai\b|\bgenerative ai\b|\bgenerative artificial intelligence\b|\bllm\b|\brag\b|\bretrieval augmented generation\b', name): return 'generative_ai_experience_years'
            if 'postgresql' in compact or re.search(r'\bpostgres\b', name): return 'postgresql_experience_years'
            if 'fullstack' in compact or 'full stack' in name: return 'fullstack_experience_years'
            if ('reactjs' in compact or re.search(r'\breact\b', name)) and 'native' not in name:
                return 'reactjs_experience_years'
            match = re.search(r'\byears? of (?:(?:professional|work|overall|total) ){0,2}experience\b', name)
            if name in ('years of experience', 'total work experience', 'professional experience'):
                return 'total_experience_years'
            if match and not re.search(r'\b(with|using|in|on)\b', name[match.end():]):
                return 'total_experience_years'
            if 'total' in name and 'months' in name and 'professional experience' in name:
                return 'total_experience_years'
            return None
        if has_duration:
            return None
        if 'react' in name and 'rest' in name and 'api' in name and any(x in name for x in ('develop', 'built', 'build', 'experience')):
            return 'react_rest_api_experience'
        if ('data structure' in name and 'algorithm' in name and any(x in name for x in ('rate', 'rating', 'scale'))):
            return 'dsa_self_rating'
        if 'first name' in name: return 'first_name'
        if 'middle name' in name: return 'middle_name'
        if 'last name' in name or 'surname' in name: return 'last_name'
        if 'full name' in name or name == 'name': return 'full_name'
        if 'email' in name: return 'application_email'
        if 'phone country code' in name or 'country calling code' in name: return 'phone_country_code'
        if 'phone' in name or 'mobile' in name: return 'phone_number'
        if 'linkedin' in name: return 'linkedin'
        if 'portfolio' in name or 'website' in name: return 'website'
        if 'postal' in name or 'zip' in name: return 'zipcode'
        if 'street' in name or name in ('address', 'residential address'): return 'street'
        if 'city' in name or name in ('current location', 'location'): return 'current_city'
        if name in ('state', 'state province', 'province', 'state of residence'): return 'state'
        if name in ('country', 'country of residence'): return 'country'
        if 'graduation year' in name or 'year of graduation' in name or 'year graduated' in name: return 'graduation_year'
        if 'percentage' in name and any(x in name for x in ('education', 'degree', 'graduation', 'academic', 'college')): return 'education_percentage'
        if 'qualification' in name or name in ('degree', 'highest degree', 'education degree'): return 'education_degree'
        if 'university' in name: return 'education_university'
        if any(x in name for x in ('college name', 'institution name', 'school name')): return 'education_institution'
        if 'current job title' in name or 'current role' in name or 'current position' in name: return 'current_role'
        if 'current' in name and ('employment type' in name or 'job type' in name): return 'current_employment_type'
        if 'current employer' in name or 'current company' in name or 'recent employer' in name: return 'current_employer'
        if name in ('professional summary', 'profile summary', 'summary'): return 'profile_summary'
        return None

    def answer(self, question, job_id=None):
        if (str(job_id) == '4485704953' and
                normalize_question(question) == 'are you comfortable commuting to this job s location'):
            answer = self.value('commute_pluswealth_gurugram')
            return str(answer) if answer is not None else None
        key = self.classify(question)
        answer = self.value(key) if key else None
        if answer is None: return None
        if key == 'joining_date':
            try:
                if date.fromisoformat(str(answer)) < date.today(): return None
            except ValueError:
                return None
        name = normalize_question(question)
        if key in ('current_ctc', 'desired_salary'):
            if any(x in name for x in ('usd', 'eur', 'gbp', 'dollar', 'euro', 'pound')): return None
            if 'lakh' in name or 'lpa' in name: return f'{float(answer) / 100000:g}'
            if 'month' in name: return f'{float(answer) / 12:g}'
        if key.endswith('_experience_years') and 'month' in name:
            return str(int(answer) * 12)
        if key == 'notice_period_days':
            if 'week' in name: return f'{int(answer) / 7:g}'
            if 'month' in name: return f'{int(answer) / 30:g}'
        return str(answer)

    def unverified(self, question):
        key = self.classify(question)
        return bool(key and not self.verified(key))

    def record_missing(self, question):
        label = ' '.join(str(question).split()).strip()
        if not label: return
        key = self.classify(label)
        # Country, shift and relocation questions can have different answers by context.
        contextual = {'work_authorization', 'visa_sponsorship', 'relocation', 'shift_willingness',
                      'citizenship', 'security_clearance', 'certifications', 'joining_date'}
        identity = 'key:' + key if key and key not in contextual and not self.verified(key) else normalize_question(label)
        try:
            data = json.loads(self.unresolved_path.read_text(encoding='utf-8'))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = {'version': 1, 'questions': []}
        items = data.setdefault('questions', [])
        item = next((x for x in items if x.get('normalized_question') == identity), None)
        if item:
            item['count'] = int(item.get('count', 0)) + 1
            item['last_seen'] = _today()
        else:
            items.append({'normalized_question': identity, 'question': label, 'key': key,
                          'status': 'unresolved', 'count': 1, 'first_seen': _today(), 'last_seen': _today()})
        _write_private(self.unresolved_path, data)

    def save_exact_answer(self, question, answer, unit='', source='Control panel: user confirmation'):
        """Persist a user-confirmed answer for this exact normalized question."""
        normalized = normalize_question(question)
        value = str(answer).strip()
        if not normalized or not value:
            raise ValueError('A question and answer are required.')
        key = 'custom:' + hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:24]
        record = self.records.get(key)
        if record is None:
            record = {'key': key, 'normalized_question': normalized}
            self.payload['records'].append(record)
            self.records[key] = record
        dtype = 'number' if re.fullmatch(r'-?\d+(?:\.\d+)?', value) else ('boolean' if value.casefold() in ('yes', 'no') else 'string')
        record.update(answer=value, unit=str(unit).strip() or None, data_type=dtype,
                      source=source, verification_status='verified', last_updated=_today())
        _write_private(self.path, self.payload)
        if self.unresolved_path.exists():
            data = json.loads(self.unresolved_path.read_text(encoding='utf-8'))
            for item in data.get('questions', []):
                if normalize_question(item.get('question', '')) == normalized:
                    item['status'] = 'resolved'; item['resolved_on'] = _today()
            _write_private(self.unresolved_path, data)

    def update(self, key, answer, source, verified=True, persist=True):
        record = self.records.get(key)
        if not record:
            aliases = ALIASES.get(key, [])
            if not aliases: return
            record = {'key': key, 'normalized_question': normalize_question(aliases[0])}
            self.payload['records'].append(record)
            self.records[key] = record
        record.update(answer=answer if verified else None, source=source,
                      verification_status='verified' if verified else 'unverified', last_updated=_today())
        if persist: _write_private(self.path, self.payload)
        if persist and verified and self.unresolved_path.exists():
            try:
                data = json.loads(self.unresolved_path.read_text(encoding='utf-8'))
                for item in data.get('questions', []):
                    if item.get('key') == key:
                        item['status'] = 'resolved'; item['resolved_on'] = _today()
                _write_private(self.unresolved_path, data)
            except (OSError, ValueError):
                pass

    def export_field_mappings(self, path=ROOT / 'candidate_field_mappings.json'):
        """Export a private, reviewable snapshot of aliases and verified values."""
        fields = []
        for key, questions in ALIASES.items():
            if not self.verified(key): continue
            for question in questions:
                answer = self.answer(question)
                if answer is not None:
                    fields.append({'field': question, 'value': answer, 'answer_key': key,
                                   'source': self.records[key].get('source')})
        _write_private(path, {'version': 1, 'generated_at': _today(), 'mappings': fields})
        return len(fields)


CONFIG_KEYS = {
    ('personals', 'first_name'): 'first_name', ('personals', 'middle_name'): 'middle_name',
    ('personals', 'last_name'): 'last_name', ('personals', 'email'): 'resume_email',
    ('personals', 'application_email'): 'application_email', ('personals', 'phone_number'): 'phone_number',
    ('personals', 'current_city'): 'current_city', ('personals', 'state'): 'state',
    ('personals', 'country'): 'country', ('personals', 'street'): 'street', ('personals', 'zipcode'): 'zipcode',
    ('personals', 'gender'): 'gender', ('personals', 'ethnicity'): 'ethnicity',
    ('personals', 'disability_status'): 'disability', ('personals', 'veteran_status'): 'veteran',
    ('questions', 'linkedIn'): 'linkedin', ('questions', 'website'): 'website',
    ('questions', 'years_of_experience'): 'total_experience_years',
    ('questions', 'additional_experience_months'): 'additional_experience_months',
    ('questions', 'nodejs_experience_years'): 'nodejs_experience_years',
    ('questions', 'postgresql_experience_years'): 'postgresql_experience_years',
    ('questions', 'fullstack_experience_years'): 'fullstack_experience_years',
    ('questions', 'reactjs_experience_years'): 'reactjs_experience_years',
    ('questions', 'nextjs_experience_years'): 'nextjs_experience_years',
    ('questions', 'ai_coding_assistant_experience_years'): 'ai_coding_assistant_experience_years',
    ('questions', 'react_rest_api_experience'): 'react_rest_api_experience',
    ('questions', 'dsa_self_rating'): 'dsa_self_rating', ('questions', 'current_role'): 'current_role',
    ('questions', 'current_employment_type'): 'current_employment_type',
    ('questions', 'recent_employer'): 'current_employer', ('questions', 'education_degree'): 'education_degree',
    ('questions', 'graduation_year'): 'graduation_year', ('questions', 'education_percentage'): 'education_percentage',
    ('questions', 'current_ctc'): 'current_ctc', ('questions', 'desired_salary'): 'desired_salary',
    ('questions', 'default_resume_path'): 'resume_path',
}


def sync_changed_config(changed, current, path=REGISTRY_PATH, unresolved_path=UNRESOLVED_PATH):
    """A changed control-panel field is an explicit user update; unchanged defaults are not."""
    if not Path(path).exists(): return
    registry = CandidateRegistry(path, unresolved_path)
    verified_keys = []
    for (section, field), key in CONFIG_KEYS.items():
        if field in changed.get(section, {}):
            value = current[section][field]
            verified = value not in (None, '')
            registry.update(key, value, 'Control panel: user update', verified, persist=False)
            if verified: verified_keys.append(key)
    if any(key in changed.get('personals', {}) for key in ('first_name', 'middle_name', 'last_name')):
        person = current.get('personals', {})
        full_name = ' '.join(str(person.get(key, '')).strip() for key in ('first_name', 'middle_name', 'last_name')).strip()
        registry.update('full_name', full_name, 'Control panel: user update', bool(full_name), persist=False)
    q_changed = changed.get('questions', {})
    if 'notice_period_verified' in q_changed:
        confirmed = bool(current.get('questions', {}).get('notice_period_verified'))
        value = current.get('questions', {}).get('notice_period')
        registry.update('notice_period_days', value, 'Control panel: user confirmation', confirmed and value is not None, persist=False)
        if confirmed and value is not None: verified_keys.append('notice_period_days')
    elif 'notice_period' in q_changed:
        registry.update('notice_period_days', current['questions']['notice_period'], 'Control panel: user update', True, persist=False)
        verified_keys.append('notice_period_days')
    _write_private(registry.path, registry.payload)
    if verified_keys and registry.unresolved_path.exists():
        try:
            data = json.loads(registry.unresolved_path.read_text(encoding='utf-8'))
            for item in data.get('questions', []):
                if item.get('key') in verified_keys:
                    item['status'] = 'resolved'; item['resolved_on'] = _today()
            _write_private(registry.unresolved_path, data)
        except (OSError, ValueError):
            pass
