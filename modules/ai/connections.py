'''
Author:     Sai Vignesh Golla
LinkedIn:   https://www.linkedin.com/in/saivigneshgolla/

Copyright (c) 2024-2026 Sai Vignesh Golla

License:    MIT License
            https://opensource.org/license/mit

GitHub:     https://github.com/GodsScion/Auto_job_applier_linkedIn

------------------------------------------------------------------------------
Provider-agnostic AI layer built on LangChain + LangGraph.

A single code path serves native Ollama, OpenAI-compatible endpoints (LM Studio,
DeepSeek, vLLM, and similar) and Google Gemini. Pick the provider,
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
import time
from urllib.error import URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen
from difflib import SequenceMatcher
from typing import Optional

from pydantic import BaseModel, Field, field_validator
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


def _check_ollama(model_name: str, api_url: str) -> str:
    """Fail early with a useful message when the configured local model cannot run."""
    parsed = urlsplit(api_url)
    base = urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")
    if not base:
        raise RuntimeError("Ollama URL is empty. Set local_ai_api_url in the AI settings.")
    try:
        with urlopen(base + "/api/tags", timeout=3) as response:
            models = json.loads(response.read().decode("utf-8")).get("models", [])
    except (OSError, ValueError, URLError) as error:
        raise RuntimeError(
            f"Ollama is not reachable at {base}. Start Ollama (`ollama serve`) and retry."
        ) from error
    names = {str(item.get("name", item.get("model", ""))) for item in models}
    if model_name not in names:
        available = ", ".join(sorted(names)) or "none"
        raise RuntimeError(
            f"Configured Ollama model '{model_name}' is not installed at {base}. "
            f"Available models: {available}. Install it with `ollama pull {model_name}`."
        )
    return base


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


def create_ai_client(model_name=None, provider=None, api_url=None, api_key=None, force=False,
                     required=False) -> Optional[AIClient]:
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

        if str(provider_name).strip().lower() == "ollama":
            base_url = (api_url if api_url is not None else getattr(cfg, "local_ai_api_url", "") or "").strip()
            base_url = _check_ollama(model_name, base_url)
            from langchain_ollama import ChatOllama
            model = ChatOllama(
                model=model_name,
                base_url=base_url,
                num_ctx=(getattr(cfg, "local_ai_ollama_num_ctx", 8192)),
                keep_alive=(getattr(cfg, "local_ai_ollama_keep_alive", "5m") or "5m"),
                **kwargs,
            )
        elif provider == "google_genai":
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
        if required:
            raise RuntimeError(
                f"Could not start required AI client ({provider_name if 'provider_name' in locals() else provider or 'unknown'}/"
                f"{model_name or 'unknown'}): {e}"
            ) from e
        if str(provider_name if 'provider_name' in locals() else provider or '').strip().lower() == "ollama":
            # Local model setup errors must reach the caller; silently disabling the
            # classifier makes an offline model look like a successful run.
            raise
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

    @field_validator('confidence', mode='before')
    @classmethod
    def normalize_confidence_percentage(cls, value):
        """Accept model confidence expressed as either a fraction or percent.

        This runs inside Pydantic's structured-output parser, before the
        ``ge=0`` / ``le=1`` constraints are checked, and also during the final
        validation of JSON fallback results.
        """
        if isinstance(value, str):
            value = value.strip()
            if value.endswith('%'):
                value = value[:-1].strip()
        confidence = float(value)
        if 1 < confidence <= 100:
            confidence /= 100
        return confidence


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


def classify_job_suitability(client, title, company, location, job_description, candidate_context,
                             *, job_id=None, run_id=None):
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
               'Do not repeat the job description or candidate context. Return every '
               'schema field; do not omit required fields or invent facts. Use decision '
               'APPLY, REVIEW, or SKIP; core_skills_match as a boolean; match_score as '
               'an integer from 0 to 100; confidence as a number from 0 to 1; '
               'matched_skills, missing_skills, and hard_blockers as string arrays; '
               'experience_gap_years as an integer or null; and reason as a string. '
               'For text output, return only one complete JSON object with these exact '
               'keys: decision, core_skills_match, match_score, confidence, '
               'matched_skills, missing_skills, experience_gap_years, hard_blockers, reason.')
    result = None
    raw_response = ''
    trace_ids = []
    metadata = {'run_id': run_id or os.environ.get('BOT_RUN_ID'), 'job_id': job_id,
                'job_title': title, 'company': company, 'candidate_context': relevant_context}
    try:
        include_raw = True
        try:
            structured = client.model.with_structured_output(JobSuitability, include_raw=True)
        except TypeError:
            include_raw = False
            structured = client.model.with_structured_output(JobSuitability)
        started = time.perf_counter()
        value = queued_call(structured.invoke, prompt)
        if include_raw and isinstance(value, dict) and 'parsed' in value:
            raw_response = _msg_text(value.get('raw')) if value.get('raw') is not None else ''
            if value.get('parsing_error'):
                raise value['parsing_error']
            parsed = value.get('parsed')
            result = parsed.model_dump() if hasattr(parsed, 'model_dump') else parsed
        else:
            result = value.model_dump() if hasattr(value, "model_dump") else value
        if not raw_response:
            raw_response = json.dumps(result, ensure_ascii=False, default=str)
        trace_ids.append(record_prompt('job suitability', prompt,
            raw_response, client.model_name, client.provider,
            parsed_output=result, latency_ms=(time.perf_counter() - started) * 1000,
            retry_info={'attempt': 1, 'mode': 'structured_output'}, **metadata))
    except Exception as structured_error:
        trace_ids.append(record_prompt('job suitability', prompt, response=raw_response,
            model=client.model_name,
            provider=client.provider, error=structured_error,
            latency_ms=(time.perf_counter() - started) * 1000 if 'started' in locals() else None,
            retry_info={'attempt': 1, 'mode': 'structured_output'}, **metadata))
        logger.info("Structured suitability output unavailable; trying JSON response: %s", structured_error)
        try:
            from modules.ai import trace_store
            trace_store.append_event(trace_ids[-1], 'retry', 'structured_output_fallback',
                                     {'next_attempt': 2, 'mode': 'text_json'})
        except Exception:
            pass
        try:
            started = time.perf_counter()
            raw = _msg_text(queued_call(client.model.invoke, prompt))
            result = convert_to_json(raw)
            trace_ids.append(record_prompt('job suitability (text fallback)', prompt, raw,
                client.model_name, client.provider, parsed_output=result,
                latency_ms=(time.perf_counter() - started) * 1000,
                retry_info={'attempt': 2, 'mode': 'text_json', 'fallback_from': 'structured_output'},
                **metadata))
        except Exception as error:
            trace_ids.append(record_prompt('job suitability (text fallback)', prompt,
                model=client.model_name, provider=client.provider, error=error,
                latency_ms=(time.perf_counter() - started) * 1000 if 'started' in locals() else None,
                retry_info={'attempt': 2, 'mode': 'text_json', 'fallback_from': 'structured_output'},
                **metadata))
            logger.warning("Suitability classification failed: %s", error)
            kind = 'MODEL_TIMEOUT' if 'timeout' in type(error).__name__.casefold() or 'timed out' in str(error).casefold() else 'MODEL_UNAVAILABLE'
            final = fail_closed(kind)
            _finalize_classifier_traces(trace_ids, final, job_id, metadata['run_id'])
            return final

    try:
        if not isinstance(result, dict) or "error" in result:
            final = fail_closed('INVALID_RESPONSE')
            _finalize_classifier_traces(trace_ids, final, job_id, metadata['run_id'])
            return final
        result["decision"] = str(result.get("decision", "")).strip().upper()
        if result["decision"] not in {"APPLY", "REVIEW", "SKIP"}:
            final = fail_closed('INVALID_RESPONSE')
            _finalize_classifier_traces(trace_ids, final, job_id, metadata['run_id'])
            return final
        validated = JobSuitability.model_validate(result).model_dump()
        validated['model_decision'] = validated['decision']
        _finalize_classifier_traces(trace_ids, validated, job_id, metadata['run_id'])
        return validated
    except Exception as error:
        logger.warning("Invalid suitability response; sending job to review: %s", error)
        final = fail_closed('INVALID_RESPONSE')
        _finalize_classifier_traces(trace_ids, final, job_id, metadata['run_id'])
        return final


def _finalize_classifier_traces(trace_ids, result, job_id=None, run_id=None):
    """Attach the classifier result to each request attempt without duplicate logs."""
    from modules.ai import trace_store
    for trace_id in trace_ids:
        try:
            trace_store.correlate_trace(trace_id, job_id=job_id, run_id=run_id,
                                        decision=result.get('decision'))
            trace_store.append_event(trace_id, 'decision', 'classifier_result', {
                'model_decision': result.get('model_decision'),
                'decision': result.get('decision'),
                'failure_kind': result.get('failure_kind'),
            })
        except Exception:
            pass


def extract_skills(client: Optional[AIClient], job_description: str, stream: bool = False) -> dict:
    '''
    Extract and classify skills from a job description.
    Returns a dict with the five skill buckets, or an ``{"error": ...}`` dict on failure.
    '''
    if not client or not job_description:
        return {"error": "AI client unavailable or empty job description."}
    prompt = extract_skills_prompt.format(job_description)
    started = time.perf_counter()
    structured_trace_id = None
    try:
        structured = client.model.with_structured_output(ExtractedSkills)
        result = queued_call(structured.invoke, prompt)
        data = result.model_dump()
        structured_trace_id = record_prompt('skill extraction', prompt, json.dumps(data, ensure_ascii=False),
                      client.model_name, client.provider, parsed_output=data,
                      latency_ms=(time.perf_counter() - started) * 1000,
                      retry_info={'attempt': 1, 'mode': 'structured_output'})
        return data
    except Exception as e:
        structured_trace_id = record_prompt('skill extraction', prompt, model=client.model_name,
                      provider=client.provider, error=e,
                      latency_ms=(time.perf_counter() - started) * 1000,
                      retry_info={'attempt': 1, 'mode': 'structured_output'})
        # Some local or older models don't support structured output — fall back to plain JSON parsing.
        logger.warning("Structured skill extraction unavailable, falling back to plain parsing. %s", e)
        try:
            try:
                from modules.ai import trace_store
                trace_store.append_event(structured_trace_id, 'retry', 'structured_output_fallback',
                                         {'next_attempt': 2, 'mode': 'text_json'})
            except Exception:
                pass
            started = time.perf_counter()
            raw = _msg_text(queued_call(client.model.invoke, prompt))
            result = convert_to_json(raw)
            record_prompt('skill extraction (text fallback)', prompt, raw,
                          client.model_name, client.provider, parsed_output=result,
                          latency_ms=(time.perf_counter() - started) * 1000,
                          retry_info={'attempt': 2, 'mode': 'text_json', 'fallback_from': 'structured_output'})
            return result
        except Exception as e2:
            record_prompt('skill extraction (text fallback)', prompt,
                          model=client.model_name, provider=client.provider, error=e2,
                          latency_ms=(time.perf_counter() - started) * 1000,
                          retry_info={'attempt': 2, 'mode': 'text_json', 'fallback_from': 'structured_output'})
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
    raw_trace_id: str
    trace_context: dict
    attempt: int


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
        started = time.perf_counter()
        try:
            message = queued_call(model.invoke, state["prompt"])
            raw = _msg_text(message).strip()
            trace_context = dict(state.get('trace_context') or {})
            trace_context['candidate_context'] = state.get('user_information') or ''
            trace_id = record_prompt('application answer', state['prompt'], raw,
                client.model_name, client.provider, latency_ms=(time.perf_counter() - started) * 1000,
                retry_info={'attempt': int(state.get('attempt', 1)), 'mode': 'text'}, **trace_context)
            return {"raw": raw, "raw_trace_id": trace_id}
        except Exception as error:
            trace_context = state.get('trace_context') or {}
            record_prompt('application answer', state['prompt'], model=client.model_name,
                provider=client.provider, error=error,
                latency_ms=(time.perf_counter() - started) * 1000,
                retry_info={'attempt': int(state.get('attempt', 1)), 'mode': 'text'}, **trace_context)
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
    trace_context: Optional[dict] = None,
) -> str:
    '''
    Generate an answer to a single application-form question.
    Returns the answer string, or "" if AI is unavailable or the call fails.
    '''
    if not client or not question:
        return ""
    try:
        trace_metadata = trace_context if isinstance(trace_context, dict) else {}
        request = {
            "question": question,
            "options": options,
            "question_type": question_type,
            "job_description": job_description,
            "about_company": about_company,
            "user_information": user_information_all,
            "trace_context": trace_metadata,
            "attempt": 1,
        }
        final = client.answer_graph.invoke(request)
        answer = final.get("answer", "") or ""
        first_trace_id = final.get('raw_trace_id')
        if not answer.strip() or answer.strip().casefold() == "cannot_answer":
            # A model may still refuse or return an empty choice despite the
            # best-estimate prompt. Retry once with an explicit instruction.
            retry = dict(request)
            retry['attempt'] = 2
            retry["question"] = (str(question) +
                "\n\nProvide your best reasonable estimate from the supplied applicant context. "
                "Do not answer CANNOT_ANSWER.")
            final = client.answer_graph.invoke(retry)
            answer = final.get("answer", "") or ""
            if first_trace_id:
                try:
                    from modules.ai import trace_store
                    trace_store.append_event(first_trace_id, 'retry', 'answer_retry', {'next_attempt': 2})
                except Exception:
                    pass
            if not answer.strip() or answer.strip().casefold() == "cannot_answer":
                trace_metadata['trace_id'] = final.get('raw_trace_id')
                return ""
        trace_id = final.get('raw_trace_id')
        trace_metadata['trace_id'] = trace_id
        if trace_id:
            try:
                from modules.ai import trace_store
                trace_store.complete_trace(trace_id, parsed_output={'final_answer': answer})
                trace_store.append_event(trace_id, 'workflow', 'final_answer_selected', {'answer': answer})
            except Exception:
                pass
        print_lg(f'AI answered "{question}" -> "{answer}"')
        return answer
    except Exception as e:
        _ai_error_alert("Could not generate an AI answer for a question.", e)
        return ""
