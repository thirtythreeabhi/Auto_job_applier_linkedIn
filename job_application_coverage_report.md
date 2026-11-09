# Job Application Coverage & Failure Analysis

Read-only local artifact snapshot (the run files showed active timestamp updates during inspection; no automation was started by this analysis). 1575 unique LinkedIn job IDs. Source timestamps retain ISO strings and offsets. Collection range: 2026-11-02T20:20:33.523799+00:00 to 2026-11-08T18:14:45.864499+00:00.

## Mutually exclusive categories

Precedence: confirmed submission; started but incomplete/uncertain; AI APPLY with no attempt; AI SKIP/REVIEW; deterministic rejection; not evaluated. Records are consolidated by LinkedIn job ID.
- `C_rejected_by_deterministic_filter`: 874
- `B_rejected_by_AI`: 440
- `F_successfully_submitted_confirmed`: 99
- `E_application_started_incomplete_or_uncertain`: 77
- `A_not_evaluated_by_AI`: 64
- `D_AI_APPLY_no_application_started`: 21

## Sources and schemas

- `application_field_audit.json`: 715 applications and 5237 suitability decisions. Application statuses: {'skip': 513, 'in_progress': 3, 'submitted': 99, 'error': 93, 'review': 2, 'submitting': 2, 'uncertain': 3}. Decision records include recommendation, score/confidence, skills, experience gap, blockers, reason and timestamp.
- `.ai_traces.sqlite3`: 1890 rows in `ai_traces`; also `ai_trace_events` and `ai_trace_imports`. Categories: application answer=95, deterministic job decision=1559, job suitability=220, job suitability (text fallback)=16.
- `.ai_prompt_log.jsonl`: timestamp/category/provider/model/prompt/response/error. Prompt bodies and generated answers were not copied into the report.
- 35 run results; events use timestamp/event/stage plus failure metadata. Application attempt events use schema v2, with phase, field label, exception class/message, last operation, and timestamp.
- Application history CSV has 106 rows; used as prior-history/context evidence, not submission confirmation.
- No separate suitability export or standalone discovery/search log was found. Per-job search_term is null because none of the structured job records associates a term reliably.

## Application outcomes and failures

Only explicit audit status `submitted` counts as confirmed; 99 application records have that status. `submitting` and `uncertain` are not confirmed. Field audit states also include error/in_progress; their field labels and latest known failure details are in the dataset.
Attempt exception phases: {'FILLING_FIELDS': 3, 'ADVANCING_FORM': 2, 'CLEANING_UP': 2, 'WAITING_FOR_MODAL': 2, 'VERIFYING_SUBMISSION': 2, 'OPENING_JOB': 2}.
Attempt exception classes: {'StaleElementReferenceException': 2, 'CannotCompleteApplication': 2, 'TimeoutException': 8, 'ApplicationCleanupIncomplete': 1}.
- 2026-11-08T18:10:43.797147+00:00 — Associate Software Engineer / Miamin Systems (ID 4485065088): `WAITING_FOR_MODAL`; field `None`; `TimeoutException`; last operation: wait for application modal and page indicator.
- 2026-11-08T18:07:36.930519+00:00 — Software Engineer / PFI Innovation Centre India (ID 4480551329): `OPENING_JOB`; field `None`; `TimeoutException`; last operation: wait for selected job Easy Apply control.
- 2026-11-08T18:05:22.478798+00:00 — Associate Software Engineer / TalentRabbit (ID 4485068155): `WAITING_FOR_MODAL`; field `None`; `TimeoutException`; last operation: wait for application modal and page indicator.
- 2026-11-08T18:04:44.095090+00:00 — Generative AI Engineer / Evnek (ID 4486348390): `OPENING_JOB`; field `None`; `TimeoutException`; last operation: wait for selected job Easy Apply control.
- 2026-11-08T18:03:01.297422+00:00 — Software Engineer / Numeric Technologies (ID 4479856745): `CLEANING_UP`; field `None`; `ApplicationCleanupIncomplete`; last operation: verify application modal closure.
- 2026-11-08T18:03:01.244647+00:00 — Software Engineer / Numeric Technologies (ID 4479856745): `CLEANING_UP`; field `None`; `TimeoutException`; last operation: verify application modal closure.
- 2026-11-08T18:02:55.871037+00:00 — Software Engineer / Numeric Technologies (ID 4479856745): `ADVANCING_FORM`; field `None`; `CannotCompleteApplication`; last operation: wait for form page transition.
- 2026-11-08T18:02:55.823298+00:00 — Software Engineer / Numeric Technologies (ID 4479856745): `ADVANCING_FORM`; field `None`; `TimeoutException`; last operation: wait for form page transition.
- 2026-11-08T18:02:11.396402+00:00 — Artificial Intelligence Engineer / Envisstack (ID 4486690688): `VERIFYING_SUBMISSION`; field `None`; `TimeoutException`; last operation: wait for confirmation dialog.
- 2026-11-08T18:01:37.771193+00:00 — Full Stack Developer (AI-Enabled Applications) / VRINDA INTERNATIONAL (ID 4481667588): `FILLING_FIELDS`; field `Highest Qualification Held*`; `CannotCompleteApplication`; last operation: select verified option for application field.
- 2026-11-08T18:01:22.799598+00:00 — Full Stack Developer (AI-Enabled Applications) / VRINDA INTERNATIONAL (ID 4481667588): `FILLING_FIELDS`; field `None`; `StaleElementReferenceException`; last operation: wait for matching city suggestion.
- 2026-11-08T18:01:04.172644+00:00 — BACKEND DEVELOPER / GreatHR Solutions (ID 4487643934): `VERIFYING_SUBMISSION`; field `None`; `TimeoutException`; last operation: wait for confirmation dialog.
- 2026-11-08T18:00:46.442985+00:00 — BACKEND DEVELOPER / GreatHR Solutions (ID 4487643934): `FILLING_FIELDS`; field `Are you comfortable commuting to this job's location?`; `StaleElementReferenceException`; last operation: select verified radio option.

### Additional failure evidence

- The AI `application answer` traces contain two errors for job 4483652208 (AI Engineer (Noida), Polestar Analytics): `Server disconnected without sending a response.` and `[Errno 111] Connection refused`. No answer content was copied here.
- The attempt log records the exact field `Highest Qualification Held*` failing during `FILLING_FIELDS` with `CannotCompleteApplication` after “select verified option for application field”; `Are you comfortable commuting to this job's location?` failed during `FILLING_FIELDS` with `StaleElementReferenceException` after “select verified radio option”. A city suggestion also hit a stale element. Other exact phases and last operations are listed above and in `attempt_failures` in the dataset.
- Across worker logs, exception text occurs repeatedly for `CannotCompleteApplication` (93 occurrences), `TimeoutException` (62), `StaleElementReferenceException` (40), and `SubmissionUncertain` (16). These are log occurrences, not unique jobs. Several traces explicitly say the incomplete application could not be saved or closed; the canonical rows retain that text through `failure_reason` where keyed.
- The available evidence does not establish a confirmed case of an unsupported control or an unanswered mandatory question. It does show required field-selection failures, radio handling and stale elements, navigation/modal timeouts, answer-generation connectivity errors, cleanup failures, and uncertain submission verification. These distinctions are retained without inferring causes beyond the captured diagnostics.

## Run-level interruptions

Run result reason/status counts: {'running': 5, 'BROWSER_SESSION_LOST': 4, 'UNKNOWN_FAILURE': 10, 'SELECTOR_FAILED': 3, 'USER_STOPPED': 6, 'LOCAL_AI_UNAVAILABLE': 7}. The most recent records show missing `langchain_ollama` at AI startup, Selenium search timeouts/click interception, lost browser sessions, and application-stage failures. Job level categorization requires a linked job ID; runs without one are not counted as job-level records.

## Deterministic filters and suitability risk

The effective `user_config.json` search override sets search location India, Past week, experience levels Internship/Entry level/Associate, Full-time, current_experience 1, and the listed 28 backend/AI/product search terms. It excludes company names/description bad words Crossover, Staffing, Recruitment Agency. Its about_company_good_words contains a broad allow-list including US-citizenship/security-clearance phrases and unrelated occupations (Embedded Programming, CNC, Mechanical Engineer, Civil Engineer, Electrical Engineer, Sales Executive, Telecaller, BPO, Data Entry, Commission only, Unpaid internship); this could permit matching companies through configured exceptions. `config/search.py` differs (United States, broader defaults, Crossover only), so the runtime override should be treated as effective. Search filters can exclude jobs before IDs/descriptions are captured. The logs/audit do not consistently record a machine-readable filter subtype for every deterministic decision; exact recorded reason is kept in rejection_reason and subtype is not fabricated. Inspect C rows for exact per-job deterministic reasons.

## AI-rejected roles that may merit human review

No model decisions were changed. Use B rows sorted by match_score and matched_skills to locate apparently relevant SKIP/REVIEW decisions; the row retains exact reason, skills, confidence and hard blockers. This flags evidence for human review without reclassifying the job.

### Higher match score examples for human review

The snapshot has 18 AI-rejected jobs with score at least 60 and at least three matched skills. These remain classified exactly as the classifier recommended; the examples show surface relevance alongside the classifier’s stated concern:

| Job ID | Role | Company | AI decision | Score / confidence | Evidence and exact reason |
|---|---|---|---|---|---|
| 4485149298 | Gen AI Engineer | VRINDA INTERNATIONAL | REVIEW | 75 / 0.85 | Matched LLM integration, RAG, AI agents, tool calling, Python, LiveKit/WebRTC. Reason: Core AI/LLM/RAG skills confirmed; AWS/Kubernetes experience absent. Years gap exceeds candidate's confirmed 1 year. |
| 4487635534 | Client-facing roles | guidix.ai - Campus Placements Reimagined | REVIEW | 75 / 0.80 | Matched AI agents, LLM integration, RAG, WebRTC, TypeScript, React. Reason: Candidate has AI integration skills but lacks confirmed client-facing experience; role emphasizes in-person client interaction. |
| 4485190718 | AI Engineer | Recro | REVIEW | 70 / 0.85 | Matched RAG, LLM integration, AI agents, tool calling, Python, LiveKit/WebRTC. Reason: Candidate lacks explicit ML training/fine-tuning and production model ownership. RAG/agents are demonstrated, but not tied to ML lifecycle or deployment. |
| 4487628813 | Full Stack Engineer | Scoutit | REVIEW | 70 / 0.75 | Matched AI agents, PostgreSQL, SQL, prompt engineering, tool calling, backend integrations. Reason: AI agents, PostgreSQL, and SQL are confirmed; Go and Angular are not. Angular and Go are hard blockers lacking evidence. |
| 4483358971 | Artificial Intelligence Engineer | HTC Global Services | REVIEW | 65 / 0.85 | Matched AI agent workflows, RAG, Next.js/TypeScript/PostgreSQL, REST APIs, LLM/tool calling, LiveKit. Reason: Candidate lacks AWS and 8+ years of Python/PostgreSQL experience. Core AI/LLM skills match, but critical infrastructure requirements are unmet. |

## Data limitations

- One record per unique job ID, combining audit, traces, attempts, run URL and prior history.
- Latest audit decision is used for decision fields; first_seen/last_seen and attempt count preserve the presence of repeated history.
- `a` is exported null because its definition is missing from the supplied schema. `search_term` and `seniority_mismatch` are null unless direct evidence is available; no structured seniority field exists.
- Application records with no job ID cannot be safely joined to a unique job.
- An APPLY decision, Submit click, `submitting` state, run counter, or “Easy Applied” history value is not proof of submission; only explicit `submitted` status is treated as confirmed. Application audit `error` results frequently contain generic `Message:` or explicit cleanup failures; see `failure_reason`.

## Ten most recently seen jobs

| Job ID | Title | Company | Last seen | Category | Latest decision |
|---|---|---|---|---|---|
| 4442619876 | Software Engineer – GoLang | The Future University | 2026-11-08T18:14:45.864499+00:00 | B_rejected_by_AI | REVIEW |
| 4442631587 | Software Engineer – GoLang | The Future University | 2026-11-08T18:14:41.258940+00:00 | C_rejected_by_deterministic_filter | SKIP |
| 4482930826 | Software Engineer – Full Stack (.NET 8 / C# / Golang / ReactJS) | IT Consulting LLP | 2026-11-08T18:14:40.914857+00:00 | C_rejected_by_deterministic_filter | SKIP |
| 4484825924 | Software Engineer - Java | Leegality | 2026-11-08T18:14:40.607915+00:00 | F_successfully_submitted_confirmed | SKIP |
| 4464971349 | Software Engineer - IAM (OIDC/OAuth) | Portainer.io | 2026-11-08T18:14:40.290221+00:00 | C_rejected_by_deterministic_filter | SKIP |
| 4486360400 | Intermediate Backend Engineer, Authorization | Jobgether | 2026-11-08T18:14:39.894100+00:00 | B_rejected_by_AI | REVIEW |
| 4479667837 | Python Developer | Training Basket | 2026-11-08T18:14:35.752433+00:00 | C_rejected_by_deterministic_filter | SKIP |
| 4487771384 | Software Engineer | Eurofins | 2026-11-08T18:14:35.333366+00:00 | B_rejected_by_AI | REVIEW |
| 4442604154 | AI-Native Full-Stack Software Engineer | The NineHertz | 2026-11-08T18:14:29.073626+00:00 | C_rejected_by_deterministic_filter | SKIP |
| 4486834782 | Developer | GetixHealth | 2026-11-08T18:14:28.650897+00:00 | B_rejected_by_AI | SKIP |
