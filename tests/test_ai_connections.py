'''
Unit tests for modules/ai/connections.py — the provider-agnostic AI layer built
on LangChain + LangGraph. These use stub models (no network, no API key) to
exercise real logic: provider resolution, message-text extraction, the LangGraph
answer pipeline, and the structured-output fallback.

License: MIT  (https://opensource.org/license/mit)
'''

import os

import pytest

from modules.ai import connections as C


# --------------------------------- stub models ------------------------------
class _Msg:
    def __init__(self, content):
        self.content = content


class _StubModel:
    '''A chat model stand-in: .invoke returns a fixed message. No structured output.'''
    def __init__(self, output):
        self._output = output

    def invoke(self, prompt):
        return _Msg(self._output)


class _Structured:
    def __init__(self, payload):
        self._payload = payload

    def invoke(self, prompt):
        class _Result:
            def __init__(self, d):
                self._d = d

            def model_dump(self):
                return self._d
        return _Result(self._payload)


class _StructuredModel(_StubModel):
    '''A model that supports with_structured_output (like a real chat model).'''
    def __init__(self, payload):
        super().__init__("")
        self._payload = payload

    def with_structured_output(self, schema):
        return _Structured(self._payload)


# ------------------------------- provider mapping ---------------------------
@pytest.mark.parametrize("name, expected", [
    ("gemini", "google_genai"),
    ("google", "google_genai"),
    ("google_genai", "google_genai"),
    ("openai", "openai"),
    ("deepseek", "openai"),   # DeepSeek runs through the OpenAI-compatible path
    ("ollama", "openai"),
    (None, "openai"),
    ("OpenAI", "openai"),     # case-insensitive
])
def test_resolve_provider(name, expected):
    assert C._resolve_provider(name) == expected


# ------------------------------- message text -------------------------------
def test_msg_text_from_string_content():
    assert C._msg_text(_Msg("plain answer")) == "plain answer"


def test_msg_text_from_content_blocks():
    msg = _Msg([{"type": "text", "text": "foo"}, {"type": "text", "text": "bar"}])
    assert C._msg_text(msg) == "foobar"


def test_msg_text_prefers_text_attribute():
    class HasText:
        text = "from-text"
        content = []
    assert C._msg_text(HasText()) == "from-text"


# ---------------------------- answer_question (graph) -----------------------
def test_answer_question_text_returns_cleaned_answer():
    client = C.AIClient(_StubModel("  5  "))
    answer = C.answer_question(client, "Years of experience?", question_type="text",
                              user_information_all="5 years")
    assert answer == "5"


def test_answer_question_select_snaps_to_allowed_option():
    client = C.AIClient(_StubModel("Yes, absolutely"))
    answer = C.answer_question(client, "Authorized to work?", options=["Yes", "No"],
                             question_type="single_select")
    assert answer == "Yes"


def test_answer_question_select_passthrough_when_no_option_matches():
    client = C.AIClient(_StubModel("Maybe later"))
    answer = C.answer_question(client, "Pick one", options=["Alpha", "Beta"],
                             question_type="single_select")
    assert answer == "Maybe later"


def test_answer_question_none_client_is_safe():
    assert C.answer_question(None, "anything") == ""


# ------------------------------- extract_skills -----------------------------
def test_extract_skills_none_client_returns_error():
    result = C.extract_skills(None, "some job description")
    assert isinstance(result, dict) and "error" in result


def test_extract_skills_structured_output():
    payload = {"tech_stack": ["Go"], "technical_skills": [], "other_skills": [],
               "required_skills": [], "nice_to_have": []}
    client = C.AIClient(_StructuredModel(payload))
    assert C.extract_skills(client, "Go backend role") == payload


def test_extract_skills_falls_back_to_json_parsing():
    # _StubModel has no with_structured_output, so extract_skills must fall back
    # to a plain call + JSON parse.
    raw = ('{"tech_stack": ["Python"], "technical_skills": [], "other_skills": [],'
           ' "required_skills": [], "nice_to_have": []}')
    client = C.AIClient(_StubModel(raw))
    assert C.extract_skills(client, "Python role")["tech_stack"] == ["Python"]


def test_suitability_classifier_preserves_review_even_with_core_skills_match(monkeypatch):
    monkeypatch.setattr(C, 'record_prompt', lambda *args, **kwargs: None)
    payload = {
        'decision': 'REVIEW', 'core_skills_match': True, 'match_score': 75,
        'confidence': 0.8, 'matched_skills': ['React'], 'missing_skills': ['AWS'],
        'experience_gap_years': 0, 'hard_blockers': [],
        'reason': 'Core fit; AWS needs confirmation.',
    }
    client = C.AIClient(_StructuredModel(payload), provider='test', model_name='stub')
    result = C.classify_job_suitability(
        client, 'Engineer', 'Example', 'New Delhi', 'A sufficiently long job description.',
        'Candidate has React experience.')
    assert result['decision'] == 'REVIEW'
    assert result['model_decision'] == 'REVIEW'


def test_suitability_classifier_sends_job_specific_evidence_to_backend(monkeypatch):
    candidate = '''## Technical Skills\nAI engineering: LLM integration, AI agents, tool/function calling, RAG.\n## Major Professional Projects\n### TalentStream AI — Recruitment SaaS\n- Built production RAG workflows and configurable AI-agent capabilities.\n'''
    payload = {
        'decision': 'REVIEW', 'core_skills_match': True, 'match_score': 70,
        'confidence': 0.7, 'matched_skills': ['RAG'], 'missing_skills': [],
        'experience_gap_years': None, 'hard_blockers': [], 'reason': 'Documented project fit.',
    }
    captured = {}
    monkeypatch.setattr(C, 'record_prompt', lambda *args, **kwargs: None)
    def queued(fn, prompt):
        captured['prompt'] = prompt
        return fn(prompt)
    monkeypatch.setattr(C, 'queued_call', queued)
    result = C.classify_job_suitability(
        C.AIClient(_StructuredModel(payload), provider='ollama', model_name='local-8k'),
        'AI Backend Engineer', 'Example', 'Delhi', 'Build RAG and tool calling services.', candidate)
    assert result['decision'] == 'REVIEW'
    assert 'TalentStream AI' in captured['prompt']
    assert 'RAG' in captured['prompt'] and 'tool/function calling' in captured['prompt']


# ------------------------------- client lifecycle ---------------------------
def test_create_ai_client_returns_none_when_ai_disabled(monkeypatch):
    import config.secrets as cfg
    monkeypatch.setattr(cfg, "use_AI", False)
    assert C.create_ai_client() is None


def test_close_ai_client_is_a_noop():
    # Must never raise, even with a client or None.
    assert C.close_ai_client(None) is None
    assert C.close_ai_client(C.AIClient(_StubModel("x"))) is None


# ------------------------------- live smoke test ----------------------------
@pytest.mark.live
@pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"),
                    reason="set OPENAI_API_KEY to run the real OpenAI smoke test")
def test_live_openai_answer(monkeypatch):
    '''Real end-to-end check of the LangChain -> OpenAI path. Skipped by default.'''
    import config.secrets as cfg
    monkeypatch.setattr(cfg, "use_AI", True)
    monkeypatch.setattr(cfg, "ai_provider", "openai")
    monkeypatch.setattr(cfg, "llm_model", os.getenv("OPENAI_TEST_MODEL", "gpt-4o-mini"))
    monkeypatch.setattr(cfg, "llm_api_key", os.environ["OPENAI_API_KEY"])
    monkeypatch.setattr(cfg, "llm_api_url", "https://api.openai.com/v1/")

    client = C.create_ai_client()
    assert client is not None
    answer = C.answer_question(client, "Reply with exactly the word: pong",
                             question_type="text")
    assert isinstance(answer, str) and answer.strip() != ""
