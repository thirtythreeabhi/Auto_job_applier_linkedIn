"""Deterministic validation for model-proposed application answers.

The model may suggest a value, but only this module returns a value suitable for
entry into a form. Validation deliberately fails closed when field facts are
missing or ambiguous.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ValidatedAnswer:
    field_type: str
    answer: str
    evidence: tuple[str, ...]
    confidence: float
    needs_human_review: bool = False


class AnswerValidationError(ValueError):
    pass


_NUMBER_WORDS = {
    '0': 'zero', '1': 'one', '2': 'two', '3': 'three', '4': 'four',
    '5': 'five', '6': 'six', '7': 'seven', '8': 'eight', '9': 'nine',
}


def _number_supported(number: str, evidence: tuple[str, ...], unit: str | None = None) -> bool:
    word = _NUMBER_WORDS.get(number)
    for excerpt in evidence:
        if re.search(rf'(?<!\d){re.escape(number)}(?!\d)', excerpt):
            if unit is None or re.search(rf'\b{unit}\b', excerpt, re.I):
                return True
        if word and re.search(rf'\b{word}\b', excerpt, re.I):
            if unit is None or re.search(rf'\b{unit}\b', excerpt, re.I):
                return True
    return False


def _experience_subject(question: str) -> str:
    patterns = (
        r'\byears?\s+of\s+(.+?)\s+experience\b',
        r'\bexperience\s+(?:with|in|using|on)\s+(.+?)(?:\?|$)',
        r'\b(?:with|in|using|on)\s+(.+?)\s+(?:experience|for)\b',
    )
    for pattern in patterns:
        match = re.search(pattern, question, re.I)
        if match:
            subject = re.sub(r'\b(?:professional|work|hands-on|years?|months?)\b', ' ', match.group(1), flags=re.I)
            subject = re.sub(r'[^a-z0-9+#./-]+', ' ', subject, flags=re.I).strip()
            if subject:
                return subject
    return ''


def _subject_duration_supported(number: str, subject: str, evidence: tuple[str, ...]) -> bool:
    tokens = [token for token in re.split(r'\s+', subject.casefold())
              if token not in {'and', 'or', 'the', 'a', 'an', 'of', 'developer', 'developers', 'tools', 'tool'}]
    word = _NUMBER_WORDS.get(number)
    for excerpt in evidence:
        folded = excerpt.casefold()
        for token in tokens:
            alternatives = [part for part in re.split(r'[/+]', token) if part] or [token]
            start = min((folded.find(part) for part in alternatives if folded.find(part) >= 0),
                        default=-1)
            while start >= 0:
                for part in alternatives:
                    for match in re.finditer(re.escape(part), excerpt, re.I):
                        duration = rf'(?<!\d){re.escape(number)}(?!\d)\s*(?:years?|yrs?)\b'
                        if word:
                            duration += rf'|\b{word}\s+(?:years?|yrs?)\b'
                        relation = re.compile(
                            rf'(?:{duration})[^.;\n]{{0,60}}{re.escape(part)}|'
                            rf'{re.escape(part)}[^.;\n]{{0,60}}(?:{duration})', re.I)
                        if relation.search(excerpt):
                            return True
                start = folded.find(token, start + 1)
    return False


def parse_proposal(raw: str) -> dict[str, Any]:
    """Parse a JSON object, tolerating a single fenced JSON block only."""
    text = str(raw or '').strip()
    fenced = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', text, re.I | re.S)
    if fenced:
        text = fenced.group(1)
    try:
        proposal = json.loads(text)
    except (TypeError, ValueError) as error:
        raise AnswerValidationError('Model response was not valid JSON.') from error
    if not isinstance(proposal, dict):
        raise AnswerValidationError('Model response must be a JSON object.')
    if not {'field_type', 'answer', 'evidence', 'confidence', 'needs_human_review'} <= proposal.keys():
        raise AnswerValidationError('Model response is missing required answer fields.')
    if not isinstance(proposal['evidence'], list) or not all(isinstance(x, str) for x in proposal['evidence']):
        raise AnswerValidationError('Answer evidence must be a list of strings.')
    try:
        confidence = float(proposal['confidence'])
    except (TypeError, ValueError) as error:
        raise AnswerValidationError('Answer confidence must be numeric.') from error
    if not 0 <= confidence <= 1:
        raise AnswerValidationError('Answer confidence must be between 0 and 1.')
    if not isinstance(proposal['needs_human_review'], bool):
        raise AnswerValidationError('needs_human_review must be boolean.')
    if not isinstance(proposal['answer'], (str, int, float)):
        raise AnswerValidationError('Answer must be a scalar string or number.')
    proposal['answer'] = str(proposal['answer']).strip()
    proposal['confidence'] = confidence
    return proposal


def validate_proposal(raw: str, metadata: dict[str, Any], candidate_context: str = '') -> ValidatedAnswer:
    proposal = parse_proposal(raw)
    kind = str(metadata.get('field_type') or 'text').casefold()
    proposed_kind = str(proposal['field_type']).casefold()
    answer = proposal['answer']
    context = str(candidate_context or '')
    evidence = tuple(x.strip() for x in proposal['evidence'] if x.strip())
    if proposal['needs_human_review']:
        raise AnswerValidationError('Model requested human review.')
    if proposed_kind != kind:
        raise AnswerValidationError('Model answer type does not match the field.')
    if proposal['confidence'] < float(metadata.get('minimum_confidence', .7)):
        raise AnswerValidationError('Model confidence is below the configured threshold.')
    if any(item not in context for item in evidence):
        raise AnswerValidationError('Model cited evidence that is not present in candidate context.')

    options = [str(x).strip() for x in metadata.get('options') or []]
    if options and not evidence:
        raise AnswerValidationError('Choice answer has no candidate evidence.')
    question = str(metadata.get('question') or '').casefold()
    if re.search(r'\b(consent|agree|attest|eligible|eligibility|legally entitled|work authorization|visa|sponsorship|citizen|criminal record|background check)\b', question):
        raise AnswerValidationError('Personal or legal attestation requires an explicit verified answer.')
    if kind in {'radio', 'single_select', 'select'}:
        matches = [option for option in options if option.casefold() == answer.casefold()]
        if len(matches) != 1:
            raise AnswerValidationError('Choice does not uniquely match an available option.')
        answer = matches[0]
    elif kind == 'checkbox':
        proposed = [x.strip() for x in answer.splitlines() if x.strip()]
        if not proposed or any(sum(o.casefold() == x.casefold() for o in options) != 1 for x in proposed):
            raise AnswerValidationError('Checkbox choices do not uniquely match available options.')
        matched = [next(o for o in options if o.casefold() == x.casefold()) for x in proposed]
        if any(not any(item.casefold() in e.casefold() for e in evidence) for item in matched):
            raise AnswerValidationError('Checkbox choice lacks direct supporting candidate evidence.')
        answer = '\n'.join(matched)
    elif kind in {'integer', 'count'} or metadata.get('numeric'):
        number = re.fullmatch(r'\s*([+-]?\d+(?:\.\d+)?)\s*', answer)
        if not number and metadata.get('rating'):
            number = re.fullmatch(r'\s*(\d+)\s*(?:out\s+of\s+\d+)\s*', answer, re.I)
        if not number:
            raise AnswerValidationError('Numeric fields accept only a numeric value.')
        value = float(number.group(1))
        if kind in {'integer', 'count'} and not value.is_integer():
            raise AnswerValidationError('Count fields require an integer.')
        minimum, maximum = metadata.get('min'), metadata.get('max')
        if minimum is not None and value < float(minimum):
            raise AnswerValidationError('Numeric answer is below the field minimum.')
        if maximum is not None and value > float(maximum):
            raise AnswerValidationError('Numeric answer exceeds the field maximum.')
        if kind in {'integer', 'count'}:
            if not evidence or not _number_supported(str(int(value)), evidence, 'projects' if kind == 'count' else None):
                raise AnswerValidationError('Count answer lacks evidence supporting the exact count.')
        elif metadata.get('experience_years'):
            if not evidence or not _number_supported(answer, evidence, 'years?'):
                raise AnswerValidationError('Experience duration lacks explicit candidate evidence.')
            subject = _experience_subject(question)
            if subject and not _subject_duration_supported(str(int(value)), subject, evidence):
                raise AnswerValidationError('Experience duration is not supported for the specific skill in the question.')
        if metadata.get('rating'):
            target = re.search(r'\bexpertise\s+in\s+([^?*]+)', question, re.I)
            skill = re.sub(r'[^a-z0-9+#.]+', ' ', target.group(1)).strip() if target else ''
            has_rating_evidence = any(
                skill and skill.casefold() in excerpt.casefold() and
                re.search(rf'(?<!\d){re.escape(str(int(value)))}\s*(?:out\s+of\s+10|/\s*10)', excerpt, re.I)
                for excerpt in evidence)
            if not has_rating_evidence:
                raise AnswerValidationError('Self-rating has no explicit candidate evidence for this skill.')
        answer = str(int(value)) if value.is_integer() else str(value)
    else:
        if not answer:
            raise AnswerValidationError('Answer is empty.')
        if not evidence:
            raise AnswerValidationError('Free-text answer has no candidate evidence.')
        # Check concrete literals commonly used to make unsupported claims.
        context_folded = context.casefold()
        for literal in re.findall(r'\b\d+(?:\.\d+)?\b|\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten)\b', answer, re.I):
            if literal.casefold() not in context_folded and not (
                literal.isdigit() and _number_supported(literal, evidence)):
                raise AnswerValidationError('Free-text answer contains a number absent from candidate evidence.')
        for proper_name in re.findall(r'\b[A-Z][A-Za-z0-9+#.-]{2,}\b', answer):
            if proper_name.casefold() not in context_folded:
                raise AnswerValidationError('Free-text answer contains an unsupported named entity or technology.')
        max_length = metadata.get('maxlength')
        if max_length is not None and len(answer) > int(max_length):
            raise AnswerValidationError('Answer exceeds the field maxlength; it must be regenerated shorter.')

    pattern = metadata.get('pattern')
    if pattern:
        try:
            if not re.fullmatch(pattern, answer):
                raise AnswerValidationError('Answer does not match the field validation pattern.')
        except re.error as error:
            raise AnswerValidationError('Field validation pattern could not be evaluated safely.') from error
    return ValidatedAnswer(kind, answer, evidence, proposal['confidence'])


def answer_prompt_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return only the actual form constraints to include in an LLM prompt."""
    keys = ('question', 'field_type', 'required', 'maxlength', 'min', 'max',
            'pattern', 'options', 'numeric', 'rating', 'experience_years')
    return {key: metadata.get(key) for key in keys if key in metadata}
