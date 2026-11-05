'''
Author:     Sai Vignesh Golla
LinkedIn:   https://www.linkedin.com/in/saivigneshgolla/

Copyright (c) 2024-2026 Sai Vignesh Golla

License:    MIT License
            https://opensource.org/license/mit

GitHub:     https://github.com/GodsScion/Auto_job_applier_linkedIn

------------------------------------------------------------------------------
Provider-agnostic AI layer built on LangChain + LangGraph.

A single code path serves OpenAI, any OpenAI-compatible endpoint (Ollama,
LM Studio, DeepSeek, vLLM, and similar) and Google Gemini. Pick the provider,
model, key and URL in `config/secrets.py`.

Public interface (used by runAiBot.py):
    create_ai_client()  -> AIClient | None
    extract_skills(client, job_description) -> dict
    answer_question(client, question, ...)  -> str
    close_ai_client(client) -> None
------------------------------------------------------------------------------
'''

from __future__ import annotations

import os
import json
import re
from difflib import SequenceMatcher
from typing import Optional

from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from langchain.chat_models import init_chat_model
from langgraph.graph import StateGraph, START, END

import config.secrets as cfg
from config.settings import showAiErrorAlerts
from modules.helpers import print_lg, critical_error_log, logger, convert_to_json
from modules.ai.prompts import extract_skills_prompt, ai_answer_prompt, job_suitability_prompt
from modules.ai.prompt_log import record_prompt
from modules.ai.request_queue import queued_call
from modules.ai.context_builder import build_job_context, fit_context_budget

from modules.gui import confirm


# Whether to keep popping up AI error dialogs (disabled once the user asks to pause them).
_alerts_enabled = bool(showAiErrorAlerts)


def _ai_error_alert(message: str, error: Exception, title: str = "AI Error") -> None:
    '''Log an AI error and (optionally) show a dismissible dialog, mirroring the rest of the tool.'''
    global _alerts_enabled
    if _alerts_enabled and confirm is not None:
        try:
            choice = confirm(f"{message}\n\n{error}\n", title, ["Pause AI alerts", "Okay, continue"])
            if choice == "Pause AI alerts":
                _alerts_enabled = False
        except Exception:
            pass
    critical_error_log(message, error)


def _resolve_provider(name: Optional[str]) -> str:
    '''
    Map the user-facing provider name to a LangChain model provider.
    Everything OpenAI-compatible (OpenAI, Ollama, LM Studio, DeepSeek, vLLM, ...)
    runs through the "openai" provider by pointing the URL at the right server.
    '''
    n = (name or "openai").strip().lower()
    if n in ("gemini", "google", "google_genai", "google-genai"):
        return "google_genai"
    return "openai"


def _msg_text(message) -> str:
    '''Extract plain text from a LangChain message (handles str content and content-block lists).'''
    text = getattr(message, "text", None)
    if callable(text):
        try:
            text = text()
        except Exception:
            text = None
    if isinstance(text, str) and text:
        return text
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text") or block.get("content") or "")
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content)


class AIClient:
    '''Small holder for the chat model and its compiled answer graph.'''
    def __init__(self, model, provider='unknown', model_name='unknown'):
        self.model = model
        self.provider = provider
        self.model_name = model_name
        self.answer_graph = _build_answer_graph(model, self)


def create_ai_client(model_name=None, provider=None, api_url=None, api_key=None, force=False) -> Optional[AIClient]:
    '''
    Build the chat model from `config/secrets.py`.
    Returns an `AIClient`, or `None` if AI is turned off or setup fails.
    '''
    if not cfg.use_AI and not force:
        print_lg("AI is turned off (use_AI = False in config/secrets.py). Skipping AI setup.")
        return None
    try:
        provider_name = provider or cfg.ai_provider
        provider = _resolve_provider(provider_name)
        model_name = model_name or cfg.llm_model
        api_key = (api_key if api_key is not None else getattr(cfg, "llm_api_key", "") or "").strip()
        temperature = getattr(cfg, "llm_temperature", None)

        kwargs = {}
        # Some newer models only accept their default temperature; leave it unset unless the user opts in.
        if temperature is not None:
            kwargs["temperature"] = temperature

        if provider == "google_genai":
            if api_key and api_key.lower() != "not-needed":
                os.environ.setdefault("GOOGLE_API_KEY", api_key)
            model = init_chat_model(model_name, model_provider="google_genai", **kwargs)
        else:
            base_url = (api_url if api_url is not None else getattr(cfg, "llm_api_url", "") or "").strip()
            # OpenAI-compatible servers accept any key; use a placeholder when none is given.
            kwargs["api_key"] = api_key or "not-needed"
            if base_url:
                kwargs["base_url"] = base_url
            model = init_chat_model(model_name, model_provider="openai", **kwargs)

        print_lg("---- AI CLIENT READY ----")
        print_lg(f"Provider: {provider_name}   |   Model: {model_name}")
        print_lg("Change these anytime in ./config/secrets.py")
        print_lg("-------------------------")
        return AIClient(model, provider_name, model_name)
    except Exception as e:
        _ai_error_alert(
            "Could not start the AI client. Check your provider, model name, API key and URL in config/secrets.py.",
            e,
        )
        return None


def close_ai_client(client: Optional[AIClient]) -> None:
    '''LangChain chat models hold no long-lived connection to close; kept for interface symmetry.'''
    return None


# --------------------------------------------------------------------------- #
# Skill extraction (structured output)
# --------------------------------------------------------------------------- #
class ExtractedSkills(BaseModel):
    '''Skills extracted from a job description and grouped into five buckets.'''
    tech_stack: list[str] = Field(default_factory=list, description="Programming languages, frameworks, libraries, databases and tools")
    technical_skills: list[str] = Field(default_factory=list, description="Technical expertise beyond specific tools (system design, data engineering, ...)")
    other_skills: list[str] = Field(default_factory=list, description="Non-technical / soft skills (communication, leadership, teamwork, ...)")
    required_skills: list[str] = Field(default_factory=list, description="Skills explicitly listed as required or expected")
    nice_to_have: list[str] = Field(default_factory=list, description="Skills listed as preferred or beneficial but not mandatory")


class JobSuitability(BaseModel):
    """Structured recommendation for whether a job should proceed to Easy Apply."""
    decision: str = Field(description="Exactly one of APPLY, REVIEW, or SKIP")
    core_skills_match: bool = Field(default=False, description="Whether the candidate's confirmed core skills match the role's essential work, independent of years of experience")
    match_score: int = Field(ge=0, le=100, description="Ranking heuristic, not hiring probability")
    confidence: float = Field(ge=0, le=1)
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    experience_gap_years: Optional[int] = None
    hard_blockers: list[str] = Field(default_factory=list)
    reason: str = Field(max_length=500, description="Concise rationale, no more than 500 characters")


def _rolling_window(text, limit, head_ratio=0.62):
    """Keep a bounded head/tail window so each classification leaves room for output."""
    text = str(text or '')
    if len(text) <= limit:
        return text
    head = int((limit - 64) * head_ratio)
    tail = limit - head - 64
    return (text[:head].rstrip() +
            '\n\n[Rolling context window: middle text omitted to leave response space.]\n\n' +
            text[-tail:].lstrip())


def classify_job_suitability(client, title, company, location, job_description, candidate_context):
    """Classify a job, failing closed to REVIEW when inference or parsing fails."""
    fallback = {
        "decision": "REVIEW", "match_score": 0, "confidence": 0.0,
        "core_skills_match": False,
        "matched_skills": [], "missing_skills": [], "experience_gap_years": None,
        "hard_blockers": [], "reason": "Classifier unavailable or returned an invalid result; human review required.",
    }
    def fail_closed(kind):
        return {**fallback, 'model_decision': None, 'failure_kind': kind}
    if not client:
        return fail_closed('MODEL_UNAVAILABLE')
    if not all((title, company, location, job_description, candidate_context)):
        return fail_closed('CLASSIFIER_CONTEXT_MISSING')

    # Context is selected by documented evidence and job-specific skill overlap.
    # Fit the assembled prompt under Ollama's 8,192-token context, reserving
    # capacity for prompt overhead and structured output.
    relevant_context = build_job_context(candidate_context, job_description, title)
    relevant_context, bounded_description = fit_context_budget(relevant_context, job_description)
    prompt = job_suitability_prompt.format(
        candidate_context=relevant_context, title=title, company=company,
        location=location, job_description=bounded_description,
    )
    prompt += ('\n\nOutput constraints: keep the reason to at most 2 short sentences '
               'and 45 words. Return at most 6 concise items in each skills list. '
               'Do not repeat the job description or candidate context.')
    result = None
    try:
        structured = client.model.with_structured_output(JobSuitability)
        value = queued_call(structured.invoke, prompt)
        result = value.model_dump() if hasattr(value, "model_dump") else value
        record_prompt('job suitability', prompt, json.dumps(result, ensure_ascii=False),
                      client.model_name, client.provider)
    except Exception as structured_error:
        record_prompt('job suitability', prompt, model=client.model_name,
                      provider=client.provider, error=structured_error)
        logger.info("Structured suitability output unavailable; trying JSON response: %s", structured_error)
        try:
            raw = _msg_text(queued_call(client.model.invoke, prompt))
            result = convert_to_json(raw)
            record_prompt('job suitability (text fallback)', prompt, raw,
                          client.model_name, client.provider)
        except Exception as error:
            record_prompt('job suitability (text fallback)', prompt,
                          model=client.model_name, provider=client.provider, error=error)
            logger.warning("Suitability classification failed: %s", error)
            kind = 'MODEL_TIMEOUT' if 'timeout' in type(error).__name__.casefold() or 'timed out' in str(error).casefold() else 'MODEL_UNAVAILABLE'
            return fail_closed(kind)

    try:
        if not isinstance(result, dict) or "error" in result:
            return fail_closed('INVALID_RESPONSE')
        result["decision"] = str(result.get("decision", "")).strip().upper()
        if result["decision"] not in {"APPLY", "REVIEW", "SKIP"}:
            return fail_closed('INVALID_RESPONSE')
        validated = JobSuitability.model_validate(result).model_dump()
        validated['model_decision'] = validated['decision']
        return validated
    except Exception as error:
        logger.warning("Invalid suitability response; sending job to review: %s", error)
        return fail_closed('INVALID_RESPONSE')


def extract_skills(client: Optional[AIClient], job_description: str, stream: bool = False) -> dict:
    '''
    Extract and classify skills from a job description.
    Returns a dict with the five skill buckets, or an ``{"error": ...}`` dict on failure.
    '''
    if not client or not job_description:
        return {"error": "AI client unavailable or empty job description."}
    prompt = extract_skills_prompt.format(job_description)
    try:
        structured = client.model.with_structured_output(ExtractedSkills)
        result = queued_call(structured.invoke, prompt)
        data = result.model_dump()
        record_prompt('skill extraction', prompt, json.dumps(data, ensure_ascii=False),
                      client.model_name, client.provider)
        return data
    except Exception as e:
        record_prompt('skill extraction', prompt, model=client.model_name,
                      provider=client.provider, error=e)
        # Some local or older models don't support structured output — fall back to plain JSON parsing.
        logger.warning("Structured skill extraction unavailable, falling back to plain parsing. %s", e)
        try:
            raw = _msg_text(queued_call(client.model.invoke, prompt))
            result = convert_to_json(raw)
            record_prompt('skill extraction (text fallback)', prompt, raw,
                          client.model_name, client.provider)
            return result
        except Exception as e2:
            record_prompt('skill extraction (text fallback)', prompt,
                          model=client.model_name, provider=client.provider, error=e2)
            _ai_error_alert("Could not extract skills from the job description.", e2)
            return {"error": str(e2)}


# --------------------------------------------------------------------------- #
# Question answering (LangGraph pipeline)
# --------------------------------------------------------------------------- #
class _AnswerState(TypedDict, total=False):
    question: str
    options: Optional[list]
    question_type: str
    job_description: Optional[str]
    about_company: Optional[str]
    user_information: Optional[str]
    prompt: str
    raw: str
    answer: str


def _build_answer_graph(model, client):
    '''
    Compile a small LangGraph pipeline for answering a form question:

        build_prompt -> generate -> (route by question type) -> format_text | select_option

    Free-text questions are returned as-is; select questions are snapped to one of
    the allowed options. The graph gives us a clean seam to extend later (validation,
    retries, resume/cover-letter nodes).
    '''
    def build_prompt(state: _AnswerState) -> dict:
        prompt = ai_answer_prompt.format(state.get("user_information") or "N/A", state.get("question") or "")
        jd = state.get("job_description")
        if jd and jd != "Unknown":
            prompt += f"\n\nJob description:\n{jd}"
        about = state.get("about_company")
        if about and about != "Unknown":
            prompt += f"\n\nAbout the company:\n{about}"
        options = state.get("options")
        if options:
            if state.get("question_type") == "multiple_select":
                count_match = re.search(r"\b(?:top|select|choose)\s+(\d+)\b", str(state.get("question") or ""), re.I)
                count_instruction = f"Select exactly {count_match.group(1)} options. " if count_match else "Select all options supported by the applicant context. "
                prompt += ("\n\n" + count_instruction +
                           "Return one exact option per line, with no other text. Available options:\n" +
                           "\n".join(f"- {o}" for o in options))
            else:
                prompt += "\n\nAnswer with exactly one of these options:\n" + "\n".join(f"- {o}" for o in options)
        return {"prompt": prompt}

    def generate(state: _AnswerState) -> dict:
        try:
            message = queued_call(model.invoke, state["prompt"])
            raw = _msg_text(message).strip()
            record_prompt('application answer', state['prompt'], raw,
                          client.model_name, client.provider)
            return {"raw": raw}
        except Exception as error:
            record_prompt('application answer', state['prompt'],
                          model=client.model_name, provider=client.provider, error=error)
            raise

    def format_text(state: _AnswerState) -> dict:
        return {"answer": (state.get("raw") or "").strip()}

    def select_option(state: _AnswerState) -> dict:
        raw = (state.get("raw") or "").strip()
        options = state.get("options") or []
        if state.get("question_type") == "multiple_select":
            selected = []
            for opt in options:
                pattern = r"(?<![a-z0-9])" + re.escape(str(opt).strip()) + r"(?![a-z0-9])"
                if re.search(pattern, raw, re.I):
                    selected.append(opt)
            for line in raw.splitlines():
                line = line.strip().lstrip("-•* ").strip()
                exact = next((opt for opt in options if line.casefold() == str(opt).casefold()), None)
                if exact is None:
                    exact = next((opt for opt in options
                                  if re.match(r"^" + re.escape(str(opt).strip()) + r"(?:\b|[,;:.!?])", line, re.I)), None)
                if exact is None and line:
                    scored = sorted(((SequenceMatcher(None, line.casefold(), str(opt).casefold()).ratio(), opt)
                                     for opt in options), reverse=True)
                    if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]) and scored[0][0] >= .45:
                        exact = scored[0][1]
                if exact is not None and exact not in selected:
                    selected.append(exact)
            count_match = re.search(r"\b(?:top|select|choose)\s+(\d+)\b", str(state.get("question") or ""), re.I)
            if count_match:
                target = min(int(count_match.group(1)), len(options))
                selected = selected[:target]
            return {"answer": "\n".join(selected)}
        for opt in options:                       # exact
            if raw == opt:
                return {"answer": opt}
        low = raw.lower()
        for opt in options:                       # case-insensitive
            if low == opt.lower():
                return {"answer": opt}
        # Accept a uniquely prefixed choice (e.g. "Yes, absolutely" -> "Yes"),
        # but never guess by arbitrary substring overlap.
        matches = [opt for opt in options
                   if re.match(r"^" + re.escape(str(opt).strip()) + r"(?:\b|[,;:.!?])", raw, re.I)]
        if len(matches) == 1:
            return {"answer": matches[0]}
        normalize = lambda value: re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()
        raw_normalized = normalize(raw)
        scored = sorted(
            ((SequenceMatcher(None, raw_normalized, normalize(opt)).ratio(), opt)
             for opt in options), reverse=True)
        if scored:
            best_score, best_option = scored[0]
            if len(scored) == 1 or best_score > scored[1][0]:
                return {"answer": best_option}
        return {"answer": options[0] if options else ""}

    def route(state: _AnswerState) -> str:
        return "select" if state.get("question_type") in ("single_select", "multiple_select") else "text"

    graph = StateGraph(_AnswerState)
    graph.add_node("build_prompt", build_prompt)
    graph.add_node("generate", generate)
    graph.add_node("format_text", format_text)
    graph.add_node("select_option", select_option)
    graph.add_edge(START, "build_prompt")
    graph.add_edge("build_prompt", "generate")
    graph.add_conditional_edges("generate", route, {"text": "format_text", "select": "select_option"})
    graph.add_edge("format_text", END)
    graph.add_edge("select_option", END)
    return graph.compile()


def answer_question(
    client: Optional[AIClient],
    question: str,
    options: Optional[list] = None,
    question_type: str = "text",
    job_description: Optional[str] = None,
    about_company: Optional[str] = None,
    user_information_all: Optional[str] = None,
    stream: bool = False,
) -> str:
    '''
    Generate an answer to a single application-form question.
    Returns the answer string, or "" if AI is unavailable or the call fails.
    '''
    if not client or not question:
        return ""
    try:
        request = {
            "question": question,
            "options": options,
            "question_type": question_type,
            "job_description": job_description,
            "about_company": about_company,
            "user_information": user_information_all,
        }
        final = client.answer_graph.invoke(request)
        answer = final.get("answer", "") or ""
        if not answer.strip() or answer.strip().casefold() == "cannot_answer":
            # A model may still refuse or return an empty choice despite the
            # best-estimate prompt. Retry once with an explicit instruction.
            retry = dict(request)
            retry["question"] = (str(question) +
                "\n\nProvide your best reasonable estimate from the supplied applicant context. "
                "Do not answer CANNOT_ANSWER.")
            final = client.answer_graph.invoke(retry)
            answer = final.get("answer", "") or ""
            if not answer.strip() or answer.strip().casefold() == "cannot_answer":
                return ""
        print_lg(f'AI answered "{question}" -> "{answer}"')
        return answer
    except Exception as e:
        _ai_error_alert("Could not generate an AI answer for a question.", e)
        return ""
