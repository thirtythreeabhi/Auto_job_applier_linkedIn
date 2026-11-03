import json
import stat

from modules.candidate_registry import CandidateRegistry, QUESTION_ALIASES
from modules.linkedin_modern import _experience_option, _matching_choice


def make_registry(tmp_path):
    path = tmp_path / 'registry.json'
    data = {'version': 1, 'records': [
        {'key': key, 'answer': value, 'verification_status': 'verified', 'source': 'test'}
        for key, value in [('first_name','Alex'), ('country','India'), ('total_experience_years',1),
                           ('python_experience_years',1), ('current_ctc',420000),
                           ('education_institution','Example Institute')]]}
    path.write_text(json.dumps(data))
    return CandidateRegistry(path, tmp_path / 'unresolved.json')


def test_more_than_100_aliases_are_mapped_to_verified_answers(tmp_path):
    registry = make_registry(tmp_path)
    assert len(QUESTION_ALIASES) >= 100
    assert registry.answer('What is your current country of residence?*') == 'India'
    assert registry.answer('Undergraduate Institution Name*') == 'Example Institute'
    assert registry.answer('How many years of experience do you have in Python?') == '1'
    assert registry.answer('Current CTC in LPA') == '4.2'
    assert registry.answer('How many years of work experience do you have with MongoDB?') is None
    assert registry.answer('How many years of experience with Python or JAVA?') is None
    assert registry.answer('Are you an immediate joiner? If not, mention notice period and remaining days.') is None


def test_exported_mappings_are_private_and_follow_registry_values(tmp_path):
    registry = make_registry(tmp_path)
    path = tmp_path / 'mappings.json'
    registry.export_field_mappings(path)
    fields = json.loads(path.read_text())['mappings']
    assert next(x['value'] for x in fields if x['field'] == 'Current CTC in LPA') == '4.2'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not any(x['answer_key'] == 'visa_sponsorship' for x in fields)


def test_select_matching_requires_one_unambiguous_choice():
    class Option:
        def __init__(self, text): self.text = text
    yes = Option('Yes, I have')
    assert _matching_choice([Option('No'), yes], 'Yes') is yes
    assert _matching_choice([Option('Yes'), Option('YES')], 'yes') is None
    assert _matching_choice([Option('0-1 years'), Option('1-3 years')], '1', True) is None
    one = Option('1 year')
    assert _matching_choice([Option('0 years'), one], '1', True) is one


def test_required_radio_group_needs_one_selected_not_every_option():
    from modules.linkedin_modern import _fill_fields
    class Radio:
        def __init__(self, chosen): self.chosen = chosen
        def is_selected(self): return self.chosen
        def get_attribute(self, key): return 'experience' if key == 'name' else None
    first, second = Radio(True), Radio(False)
    class Modal:
        def find_elements(self, by, query):
            if query in ('.//input[@required and @type="radio"]', './/input[@type="radio"]'):
                return [first, second]
            return []
    _fill_fields(None, Modal(), {}, {}, '', lambda _: None)


def test_joining_date_expiry_and_serving_notice_are_not_guessed(tmp_path):
    from datetime import date, timedelta
    registry = make_registry(tmp_path)
    for key, value in [('joining_date', (date.today() + timedelta(days=12)).isoformat()), ('serving_notice', 'Yes')]:
        record = {'key':key, 'answer':value, 'verification_status':'verified', 'source':'test'}
        registry.records[key] = record
    assert registry.answer('How soon can you join us?') == (date.today() + timedelta(days=12)).isoformat()
    assert registry.answer('Are you currently serving your notice period?') == 'Yes'
    assert registry.answer('Are you an immediate joiner? If not, state your notice period and remaining days.') is None
    registry.records['joining_date']['answer'] = (date.today() - timedelta(days=1)).isoformat()
    assert registry.answer('How soon can you join us?') is None


def test_checkbox_wont_automatically_accept_legal_agreement(monkeypatch):
    import pytest
    from modules import linkedin_modern as modern
    class Field:
        def is_selected(self): return False
    field = Field()
    class Modal:
        def find_elements(self, by, query):
            return [field] if query == './/input[@required and @type="checkbox"]' else []
    class Registry:
        def answer(self, label): return 'Yes'
        def record_missing(self, label): pass
    monkeypatch.setattr(modern, '_field_label', lambda modal, f: 'I agree to terms')
    monkeypatch.setattr(modern, '_click', lambda *args: pytest.fail('Legal agreement must not be checked'))
    with pytest.raises(modern.CannotCompleteApplication):
        modern._fill_fields(None, Modal(), {}, {}, '', lambda _: None, Registry())


def test_skill_presence_never_implies_unconfirmed_years(tmp_path):
    from modules.field_mappings import PROJECT_SKILLS, ALIASES
    registry = make_registry(tmp_path)
    registry.records['skill_typescript_used'] = {'answer':'Yes', 'verification_status':'verified'}
    assert sum(len(ALIASES['skill_' + key + '_used']) for key in PROJECT_SKILLS) >= 100
    assert registry.answer('Have you used TypeScript in a project?') == 'Yes'
    assert registry.answer('How many years of professional TypeScript experience do you have?') is None
    assert registry.answer('Have you used Rust in a project?') is None
    assert registry.answer('Do you need visa sponsorship?') is None


def test_confirmed_new_skills_and_nationality_are_context_specific(tmp_path):
    registry = make_registry(tmp_path)
    for key, value in [('mongodb_experience_years',1), ('angularjs_experience_years',1), ('ssis_experience_years',1), ('nationality','Indian')]:
        registry.records[key] = {'answer':value, 'verification_status':'verified'}
    assert registry.answer('How many years of work experience do you have with MongoDB?') == '1'
    assert registry.answer('How many years of work experience do you have with AngularJS?') == '1'
    assert registry.answer('How many years of work experience do you have with SQL Server Integration Services (SSIS)?') == '1'
    assert registry.answer('Nationality*') == 'Indian'
    assert registry.answer('Are you a citizen of the United States?') is None
    assert registry.answer('How many years of work experience with Angular?') is None


def test_commute_answer_is_limited_to_the_confirmed_job(tmp_path):
    registry = make_registry(tmp_path)
    registry.records['commute_pluswealth_gurugram'] = {'answer':'Yes', 'verification_status':'verified'}
    question = "Are you comfortable commuting to this job's location?"
    assert registry.answer(question, job_id='4485704953') == 'Yes'
    assert registry.answer(question, job_id='123') is None
    assert registry.answer(question) is None


def test_java_full_stack_experience_is_not_general_full_stack(tmp_path):
    registry = make_registry(tmp_path)
    registry.records['fullstack_experience_years'] = {'answer': 1, 'verification_status': 'verified'}
    assert registry.answer('How many years of Relevant experience as Java Full Stack Development.?') is None
    assert registry.answer('How many years of professional experience in full stack application development using C# and ASP.NET Core?') is None
