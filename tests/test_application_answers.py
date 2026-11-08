import json

import pytest

from modules.application_answers import AnswerValidationError, validate_proposal


def proposal(answer, field_type='text', evidence=None, **extra):
    return json.dumps({'field_type': field_type, 'answer': answer,
                       'evidence': evidence or [], 'confidence': .95,
                       'needs_human_review': False, **extra})


def test_rating_expression_normalizes_only_with_unambiguous_rating_metadata():
    result = validate_proposal(proposal('7 out of 10', evidence=['ReactJS expertise was explicitly rated 7 out of 10']),
        {'question': 'On a scale of 1–10, how would you rate your expertise in ReactJS?',
         'field_type': 'text', 'numeric': True, 'rating': True, 'min': 1, 'max': 10},
        'ReactJS expertise was explicitly rated 7 out of 10')
    assert result.answer == '7'


def test_qwen_react_rating_pipeline_accepts_7_out_of_10_with_matching_evidence(monkeypatch):
    from modules import linkedin_modern as modern
    from modules.ai import connections
    raw = proposal('7 out of 10', evidence=['ReactJS expertise: 7 out of 10'])
    monkeypatch.setattr(connections, 'answer_question', lambda *args, **kwargs: raw)

    answer = modern._generate_application_answer(
        object(), 'On a scale of 1 to 10, how would you rate your expertise in ReactJS?', 'text', None,
        'ReactJS expertise: 7 out of 10', '', '', field_metadata={
            'field_type': 'text', 'numeric': True, 'rating': True, 'min': 1, 'max': 10,
        })

    assert answer == '7'


def test_project_count_response_349_chars_is_rejected_before_field_entry(monkeypatch):
    from modules import linkedin_modern as modern
    from modules.ai import connections
    prefix = 'Four projects from scratch: '
    long_answer = prefix + ('x' * (349 - len(prefix)))
    assert len(long_answer) == 349
    raw = proposal(long_answer, evidence=['Four projects from scratch'])
    calls = []
    monkeypatch.setattr(connections, 'answer_question', lambda *args, **kwargs: calls.append(kwargs) or raw)

    answer = modern._generate_application_answer(
        object(), 'How many projects have you built from scratch?', 'text', None,
        'Four projects from scratch', '', '', max_length=20,
        field_metadata={'field_type': 'text', 'maxlength': 20})

    assert answer is None
    assert len(calls) == 2  # One bounded shorter-answer retry, still rejected.
    assert all(call.get('structured_response') for call in calls)


def test_ci_cd_zero_year_answer_does_not_borrow_total_experience(monkeypatch):
    from modules import linkedin_modern as modern
    from modules.ai import connections
    context = 'Total professional experience: 6 years. CI/CD appears in the skills list.'
    raw = proposal('0', evidence=['CI/CD appears in the skills list'])
    monkeypatch.setattr(connections, 'answer_question', lambda *args, **kwargs: raw)

    answer = modern._generate_application_answer(
        object(), 'How many years of CI/CD experience do you have?', 'text', None,
        context, '', '', field_metadata={'field_type': 'text', 'numeric': True})

    assert answer is None
    assert not hasattr(modern, '_skill_duration_fallback')


def test_unsupported_project_claim_is_rejected():
    context = 'Candidate built a small React dashboard.'
    with pytest.raises(AnswerValidationError, match='number|named entity'):
        validate_proposal(
            proposal('I built five projects using AWS and Kubernetes.', evidence=['Candidate built a small React dashboard.']),
            {'field_type': 'text', 'question': 'Describe your projects'}, context)


@pytest.mark.parametrize('answer', ['about 7', '7 years', '7/10', '11'])
def test_invalid_numeric_rating_is_rejected(answer):
    with pytest.raises(AnswerValidationError):
        validate_proposal(proposal(answer),
            {'field_type': 'text', 'numeric': True, 'rating': True, 'min': 1, 'max': 10})


def test_rating_is_rejected_without_skill_specific_evidence():
    with pytest.raises(AnswerValidationError, match='Self-rating'):
        validate_proposal(proposal('7'),
            {'question': 'On a scale of 1–10, how would you rate your expertise in ReactJS?',
             'field_type': 'text', 'numeric': True, 'rating': True, 'min': 1, 'max': 10},
            'React is a documented skill; DSA rating is 7 out of 10')


def test_count_prose_over_maxlength_is_rejected_without_truncation():
    long_answer = 'I built four projects from scratch. ' + ('details ' * 40)
    with pytest.raises(AnswerValidationError, match='maxlength'):
        validate_proposal(proposal(long_answer, evidence=['four projects']),
            {'field_type': 'text', 'maxlength': 20, 'question': 'How many projects?'},
            'I built four projects from scratch.')


def test_integer_count_requires_matching_candidate_evidence():
    result = validate_proposal(proposal('4', 'count', ['I built four projects']),
        {'field_type': 'count', 'question': 'How many projects?'},
        'I built four projects')
    assert result.answer == '4'


def test_count_without_evidence_fails_closed():
    with pytest.raises(AnswerValidationError, match='evidence'):
        validate_proposal(proposal('4', 'count'),
            {'field_type': 'count', 'question': 'How many projects?'}, 'project history')


def test_required_yes_no_radio_matches_one_available_option():
    result = validate_proposal(proposal('Yes', 'radio', ['Built two personal projects']),
        {'field_type': 'radio', 'required': True, 'options': ['Yes', 'No']},
        'Built two personal projects')
    assert result.answer == 'Yes'


def test_choice_without_candidate_evidence_is_rejected():
    with pytest.raises(AnswerValidationError, match='evidence'):
        validate_proposal(proposal('Yes', 'radio'),
            {'field_type': 'radio', 'required': True, 'options': ['Yes', 'No']})


def test_dropdown_selection_is_exact_and_uniquely_identified():
    raw = proposal('New Delhi', 'select', ['Current location: New Delhi'])
    result = validate_proposal(raw,
        {'field_type': 'select', 'options': ['New Delhi', 'Mumbai']},
        'Current location: New Delhi')
    assert result.answer == 'New Delhi'
    with pytest.raises(AnswerValidationError, match='uniquely'):
        validate_proposal(raw,
            {'field_type': 'select', 'options': ['New Delhi', 'New Delhi']},
            'Current location: New Delhi')


def test_checkbox_selection_requires_explicit_evidence_for_each_choice():
    raw = proposal('React\nPython', 'checkbox', ['React and Python are demonstrated skills'])
    result = validate_proposal(raw,
        {'field_type': 'checkbox', 'options': ['React', 'Python', 'Go']},
        'React and Python are demonstrated skills')
    assert result.answer.splitlines() == ['React', 'Python']
    with pytest.raises(AnswerValidationError, match='evidence'):
        validate_proposal(proposal('Go', 'checkbox', ['Python is a skill']),
            {'field_type': 'checkbox', 'options': ['Go']}, 'Python is a skill')


def test_experience_years_require_an_explicit_duration_excerpt():
    result = validate_proposal(proposal('1', 'text', ['one year of hands-on AI-assisted development']),
        {'field_type': 'text', 'numeric': True, 'experience_years': True},
        'one year of hands-on AI-assisted development')
    assert result.answer == '1'
    with pytest.raises(AnswerValidationError, match='duration'):
        validate_proposal(proposal('1', 'text', ['AI tools are used']),
            {'field_type': 'text', 'numeric': True, 'experience_years': True},
            'AI tools are used')


def test_total_years_evidence_cannot_support_a_specific_skill_duration():
    excerpt = 'Total professional experience: 6 years; CI/CD appears in the skills list.'
    with pytest.raises(AnswerValidationError, match='specific skill'):
        validate_proposal(proposal('6', 'text', [excerpt]),
            {'question': 'How many years of CI/CD experience do you have?',
             'field_type': 'text', 'numeric': True, 'experience_years': True}, excerpt)


def test_html_pattern_is_enforced_deterministically():
    with pytest.raises(AnswerValidationError, match='pattern'):
        validate_proposal(proposal('7', evidence=['ReactJS expertise rating: 7 out of 10']),
            {'field_type': 'text', 'numeric': True, 'rating': True, 'min': 1, 'max': 10,
             'question': 'Rate expertise in ReactJS', 'pattern': r'[0-5]'},
            'ReactJS expertise rating: 7 out of 10')


def test_free_text_requires_candidate_evidence_and_rejects_unsupported_claim_literals():
    with pytest.raises(AnswerValidationError, match='evidence'):
        validate_proposal(proposal('Built a platform.', 'text'),
                          {'field_type': 'text'}, 'Candidate profile')
    with pytest.raises(AnswerValidationError, match='number'):
        validate_proposal(proposal('I built 10 platforms.', 'text', ['Built multiple applications']),
                          {'field_type': 'text'}, 'Built multiple applications')
    with pytest.raises(AnswerValidationError, match='named entity'):
        validate_proposal(proposal('Built an AWS service.', 'text', ['Built backend components']),
                          {'field_type': 'text'}, 'Built backend components')


@pytest.mark.parametrize('question', [
    'Do you consent to a background check?',
    'Are you legally entitled to work here?',
    'Do you require visa sponsorship?',
])
def test_unverified_attestations_require_human_review(question):
    with pytest.raises(AnswerValidationError, match='attestation'):
        validate_proposal(proposal('No', 'radio', ['No']),
            {'question': question, 'field_type': 'radio', 'options': ['Yes', 'No']}, 'No')
