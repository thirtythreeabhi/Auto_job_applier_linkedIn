"""Deterministic candidate evidence parsing and job-specific context assembly.

No model calls are made here. Markdown headings and source lines are retained so
the suitability classifier can distinguish confirmed work from project evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re


ALIASES = {
    "RAG": ("rag", "retrieval augmented generation", "retrieval-augmented generation"),
    "LLM integration": ("llm integration", "large language model integration", "llm"),
    "AI agents": ("ai agent", "ai agents", "agentic", "agent capabilities", "agent"),
    "tool calling": ("tool calling", "function calling", "agent tools", "tools/actions", "agent function calling"),
    "TalentStream": ("talentstream",),
    "PostgreSQL": ("postgres", "postgresql"),
    "Node.js": ("node.js", "nodejs", "node js"),
    "TypeScript": ("typescript", "ts"),
    "JavaScript": ("javascript",),
    "Next.js": ("next.js", "nextjs"),
    "React": ("react", "react.js", "reactjs"),
    "Python": ("python",),
    "Django": ("django",),
    "GCP": ("gcp", "google cloud platform"),
    "Docker": ("docker",),
    "REST APIs": ("rest api", "restful api"),
    "webhooks": ("webhook",),
    "Redis": ("redis",),
    "MongoDB": ("mongodb", "mongo db"),
    "SQL": ("sql",),
    "embeddings": ("embedding",),
    "PostgreSQL vector search": ("pgvector", "vector search"),
    "LiveKit": ("livekit",),
    "WebRTC": ("webrtc",),
    "CRM integrations": ("crm integration", "crm workflow"),
    "Kubernetes": ("kubernetes", "k8s"),
    "Terraform": ("terraform",),
    "AWS": ("aws", "amazon web services"),
}

_RELEVANT_HEADINGS = ("education", "professional experience", "technical skills",
                      "major professional projects", "personal projects",
                      "job search preferences", "experience limitations",
                      "information that is not yet confirmed")
def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9+#.]+", " ", str(text).casefold()).strip()


@dataclass
class CandidateContext:
    facts: list[str] = field(default_factory=list)
    skills: dict[str, list[str]] = field(default_factory=dict)
    projects: list[str] = field(default_factory=list)
    experience: list[str] = field(default_factory=list)
    preferences: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


def parse_candidate_context(markdown: str, confirmations=None) -> CandidateContext:
    """Parse relevant Markdown sections and optional verified registry records."""
    result = CandidateContext()
    section = ""
    for raw in str(markdown or "").splitlines():
        if raw.strip().casefold() == "verified application facts:":
            section = "Verified user confirmations"
            continue
        if raw.startswith("## "):
            section = raw[3:].strip()
            continue
        if not section or (section != "Verified user confirmations" and
                           not any(name in section.casefold() for name in _RELEVANT_HEADINGS)):
            continue
        line = raw.strip()
        if not line or line.startswith("|") or line.startswith("---"):
            continue
        if line.startswith("### "):
            line = line[4:].strip() + ":"
        else:
            line = re.sub(r"^[-*]\s+", "", line)
        line = line.strip()
        if not line:
            continue
        labeled = f"[{section}] {line}"
        low = section.casefold()
        if "technical skills" in low:
            for skill, aliases in ALIASES.items():
                if any(re.search(r"(?<![\w])" + re.escape(a) + r"(?![\w])", line, re.I) for a in aliases):
                    result.skills.setdefault(skill, []).append(labeled)
        elif "project" in low:
            result.projects.append(labeled)
        elif "experience" in low or "verified user confirmations" in low:
            result.experience.append(labeled)
        elif "preferences" in low or "not yet confirmed" in low:
            result.preferences.append(labeled)
            if re.search(r"do not|don't|not confirmed|avoid|without|never", line, re.I):
                result.limitations.append(labeled)
        else:
            result.facts.append(labeled)

    # Registry entries marked verified are subsequent explicit user confirmations.
    # Keep only job-relevant professional keys; never expose personal/contact values.
    allowed = {"total_experience_years", "nodejs_experience_years", "postgresql_experience_years",
               "additional_experience_months", "current_employer", "current_role",
               "education_degree", "graduation_year", "technical_skill_summary"}
    for record in confirmations or ():
        if not isinstance(record, dict) or record.get("verification_status") != "verified":
            continue
        key, value = record.get("key"), record.get("answer")
        if key in allowed and value not in (None, ""):
            result.facts.append(f"[User-confirmed registry: {key}] {value}")
    return result


def extract_job_requirements(job_description: str, title: str = "") -> list[str]:
    """Find known skills deterministically, including common aliases."""
    text = _norm(f"{title}\n{job_description}")
    found = []
    for canonical, aliases in ALIASES.items():
        if any(re.search(r"(?<![a-z0-9])" + re.escape(_norm(a)) + r"(?![a-z0-9])", text) for a in aliases):
            found.append(canonical)
    return found


def build_job_context(markdown: str, job_description: str, title: str = "", *,
                      confirmations=None, max_chars: int = 9000) -> str:
    """Select evidence by job skill overlap, then add core facts and limitations."""
    parsed = parse_candidate_context(markdown, confirmations)
    requirements = extract_job_requirements(job_description, title)
    out = ["Candidate evidence (source labels identify documented evidence):"]
    remaining = max(0, max_chars - len(out[0]))

    def add(text):
        nonlocal remaining
        if not text or remaining <= 0:
            return
        text = text.strip()
        if len(text) > remaining:
            # Keep a whole evidence line when possible; never splice a claim in half.
            return
        out.append(text)
        remaining -= len(text) + 1

    # Strongly relevant source lines first, with project evidence above generic skills.
    for skill in requirements:
        for line in parsed.projects + parsed.experience + parsed.skills.get(skill, []):
            if skill.casefold() in _norm(line) or any(a in _norm(line) for a in map(_norm, ALIASES.get(skill, ()))):
                add(line)
    for section in (parsed.experience, parsed.facts):
        for line in section:
            add(line)
    # Keep the complete evidence block for projects whose title or content
    # overlaps the role. This retains adjacent delivery/reliability evidence.
    project_blocks, block = [], []
    for line in parsed.projects:
        if line.endswith(":") and block:
            project_blocks.append(block)
            block = []
        block.append(line)
    if block:
        project_blocks.append(block)
    for block in project_blocks:
        normalized = _norm(" ".join(block))
        if any(any(re.search(r"(?<![a-z0-9])" + re.escape(_norm(alias)) + r"(?![a-z0-9])", normalized)
                   for alias in ALIASES.get(skill, (skill,))) for skill in requirements):
            for line in block:
                add(line)
    if parsed.skills:
        all_skills = ", ".join(f"{k} (documented)" for k in parsed.skills)
        add("[Technical skills] " + all_skills)
    for line in parsed.preferences + parsed.limitations:
        add(line)
    if requirements:
        add("[Deterministic job requirements detected] " + ", ".join(requirements))
    # Explicitly guard the distinction between skills and duration.
    add("[Experience limitation] Technology familiarity or project evidence does not establish professional years; use only explicitly confirmed durations.")
    return "\n".join(out)


def estimate_tokens(text: str) -> int:
    """Conservative, dependency-free budget estimate for mixed English text."""
    return (len(str(text)) + 2) // 3


def _select_job_description(text: str, limit: int) -> str:
    """Pack the most informative job sentences, preserving source order."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    chunks = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip()]
    requirements = extract_job_requirements(text)
    ranked = []
    for index, chunk in enumerate(chunks):
        normalized = _norm(chunk)
        score = sum(3 for skill in requirements if any(
            re.search(r"(?<![a-z0-9])" + re.escape(_norm(alias)) + r"(?![a-z0-9])", normalized)
            for alias in ALIASES.get(skill, (skill,))))
        if re.search(r"\b(required|must have|must-have|responsibilit|qualification|preferred|minimum)\b", normalized):
            score += 2
        # Preserve a useful introductory statement as well as targeted evidence.
        if index == 0:
            score += 1
        ranked.append((score, index, chunk))
    chosen, used = [], 0
    for score, index, chunk in sorted(ranked, key=lambda row: (-row[0], row[1])):
        if used + len(chunk) + 1 <= limit:
            chosen.append((index, chunk))
            used += len(chunk) + 1
    if not chosen:
        # A single enormous paragraph: retain a complete bounded fragment.
        return chunks[0][:limit] if chunks else text[:limit]
    return "\n".join(chunk for _, chunk in sorted(chosen))


def fit_context_budget(candidate_context: str, job_description: str, *, budget_tokens=8192,
                       response_reserve=700):
    """Bound full prompt inputs by deterministic content priority under token budget."""
    fixed_tokens = 716  # system/template and job metadata allowance, plus rounding slack
    available_chars = max(1200, (budget_tokens - response_reserve - fixed_tokens) * 3)
    # Retain candidate evidence first; then select job sentences by requirements.
    candidate_limit = min(len(candidate_context), min(10000, available_chars // 2))
    context = candidate_context[:candidate_limit]
    job_limit = max(500, available_chars - len(context))
    job = _select_job_description(job_description, job_limit)
    return context, job
