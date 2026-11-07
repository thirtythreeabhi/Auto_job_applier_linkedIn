from modules.ai.context_builder import (
    ALIASES, build_job_context, estimate_tokens, extract_job_requirements,
    fit_context_budget, parse_candidate_context,
)


SAMPLE = """# Candidate\n## Personal Information\n- Email: private@example.com\n## Professional Experience\n- Current role: Software Engineer (Full-time) at Ravan.ai, November 2025–Present.\n- Total professional experience: 1 year, explicitly confirmed.\n- Node.js: 1 year professional experience.\n- React projects are documented; professional duration is not confirmed.\n## Technical Skills\nAI engineering: LLM integration, AI agents, tool/function calling, RAG, embeddings.\n## Major Professional Projects\n### TalentStream AI — Recruitment SaaS\n- Production platform with RAG, configurable AI-agent capabilities and candidate scoring.\n- Built retries, recovery workflows, background processing, webhooks and AI calling integrations.\n### Other project\n- Made a static website with CSS.\n## Job Search Preferences\n- Prioritize backend and AI application engineering; prefer 0–2 years.\n- Do not assume foreign work authorization or willingness to relocate internationally.\n"""


def test_parser_sections_confirmations_and_experience_limits():
    parsed = parse_candidate_context(SAMPLE, [
        {"key": "total_experience_years", "answer": "1", "verification_status": "verified"},
        {"key": "work_authorization", "answer": "Yes", "verification_status": "verified"},
    ])
    assert "RAG" in parsed.skills
    assert any("TalentStream" in line for line in parsed.projects)
    assert any("Do not assume" in line for line in parsed.limitations)
    assert any("total_experience_years" in line for line in parsed.facts)
    assert not any("private@example" in line for line in parsed.facts + parsed.projects)
    assert not any("work_authorization" in line for line in parsed.facts)


def test_job_requirement_extraction_understands_aliases_and_ai_backend_skills():
    requirements = extract_job_requirements(
        "Build RAG pipelines, LLM integrations, agent function calling, and PostgreSQL services",
        "Backend AI Engineer")
    assert {"RAG", "LLM integration", "AI agents", "tool calling", "PostgreSQL"} <= set(requirements)
    assert ALIASES["RAG"]


def test_retrieval_prioritizes_talentstream_rag_and_tool_call_evidence():
    selected = build_job_context(SAMPLE, "Develop RAG and agent tool calling systems", "AI Backend Engineer")
    assert "TalentStream" in selected
    assert "configurable AI-agent capabilities" in selected
    assert "retries, recovery workflows" in selected
    assert "LLM integration" in selected and "tool/function calling" in selected
    assert "Total professional experience: 1 year, explicitly confirmed." in selected
    assert "static website" not in selected
    assert "professional duration is not confirmed" in selected


def test_missing_skills_are_not_fabricated():
    selected = build_job_context(SAMPLE, "Expert Kubernetes and Terraform required", "Platform Engineer")
    assert "Kubernetes" in selected  # captured as a requirement, not candidate evidence
    assert "[Technical skills]" in selected
    parsed = parse_candidate_context(SAMPLE)
    assert "Kubernetes" not in parsed.skills


def test_long_job_description_and_overflow_fit_within_ollama_budget():
    context = build_job_context(SAMPLE, "RAG", "Engineer")
    description = ("RAG PostgreSQL background processing details. " * 5000)
    bounded_context, bounded_job = fit_context_budget(context, description)
    assert len(bounded_job) < len(description)
    assert estimate_tokens(bounded_context) + estimate_tokens(bounded_job) + 1200 <= 8192
    assert "TalentStream" in bounded_context


def test_long_description_keeps_late_requirement_instead_of_only_prefix():
    description = "Routine role overview with no specific stack. " * 1200
    description += "Required experience: Kubernetes and Terraform deployment pipelines."
    _, selected = fit_context_budget(build_job_context(SAMPLE, description, "Platform Engineer"), description)
    assert "Kubernetes" in selected and "Terraform" in selected
    assert len(selected) < len(description)


def test_historical_classifier_input_comparison_is_local_and_no_inference(tmp_path, monkeypatch):
    """Compare a saved historical excerpt with deterministic current evidence."""
    from modules.ai import prompt_log
    import json

    history = tmp_path / "history.jsonl"
    historical_prompt = "CANDIDATE CONTEXT:\n" + SAMPLE[:500] + "\nJOB:\nRAG agent tool calling"
    history.write_text(json.dumps({"category": "job suitability", "prompt": historical_prompt}) + "\n")
    monkeypatch.setattr(prompt_log, "PROMPT_LOG_PATH", history)
    saved = json.loads(history.read_text().splitlines()[0])["prompt"]
    previous = saved.split("CANDIDATE CONTEXT:\n", 1)[1].split("\nJOB:", 1)[0]
    current = build_job_context(SAMPLE, "RAG agent tool calling", "AI Engineer")
    assert "TalentStream" not in previous
    assert "TalentStream" in current
    assert "RAG" in current and "tool/function calling" in current
