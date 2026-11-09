# Historical LinkedIn draft answer audit

Generated: 2026-11-09T01:10:30.925132+05:30  
Scope: local artifacts only; no LinkedIn or browser interaction. Candidate values in this report are redacted. Audit records and evidence files were read-only. The report is a retrospective assessment of historical behavior; it does not assess the newly implemented pipeline or its tests.

## Scope and source integrity

- `application_field_audit.json`: 744 application attempt records; captured statuses: {'skip': 519, 'in_progress': 3, 'submitted': 108, 'error': 107, 'review': 2, 'submitting': 2, 'uncertain': 3}.
- `.ai_traces.sqlite3`: 137 application-answer traces from 3300 total traces.
- Attempt event files: 51 diagnostic events across 15 files.
- Existing datasets inspected: `job_application_coverage.csv`, `job_application_coverage.jsonl`, `historical_job_lifecycle_20261109_000303_IST/*`; latest pre-existing lifecycle summary reports 736 attempts/139 draft saves, while live audit artifact currently has 744 records. Historical snapshot differs from current files and was not used as a one-to-one attempt ledger.
- Attempt selection for this answer-level audit: all audit entries with captured fields, plus attempts identified as saved/incomplete in source records. Attempt JSONL/worker events identify failure phase, but many attempts lack field-level browser snapshots.
- Sources: see row-level source columns and `logs/runs/<run_id>/...`; no screenshots were opened because static audit fields and event records did not expose additional field values. Artifacts exist in some runs, but no DOM snapshot/option capture is available as a complete record.

## Stage distinctions

`draft_field_level_audit.csv` preserves captured audit value and any Qwen raw/parsed answer. Historical records do not consistently retain validator-accepted answer, normalized answer, Selenium argument, live DOM value, browser acceptance, maxlength, options, or manual edit history. Those columns therefore say `NOT_CAPTURED` unless the prompt included metadata. Prompt metadata is not proof of the live DOM or validation outcome. `application_field_audit.json`'s `filled`/AI-generated field entries are evidence of an audit-recorded value, not standalone DOM proof. The known React edit is separately attributed to your explicit instruction.

## Findings

- Distinct attempts with at least one classified problematic field: **15**.
- Attempts with unsupported or contradictory claims: **1** (strict class count; unverifiable claims are reported separately).
- Attempts with malformed/type-mismatched captured values: **14**.
- Attempts flagged for manual review including incomplete or uncertain applications: **191**.
- Qwen traces preserved: **137**; this is trace count, not answerable-field count.
- Captured data is not sufficient to claim a comprehensive DOM-level audit of every draft. Rows with `NOT_CAPTURED` are explicitly unresolved, not assumed correct.

### Known example: Alternative Path, Full Stack Engineer, job 4485592376

Attempt `90559fcd0bc74d0ab5bf8622491c81d2`, run `72ef06801bcf4f28ac9ce7f60bba303a`, started 2026-11-08T18:48:59.712574+00:00; audit result says form failed required validation and draft saved. The field-level CSV has the per-question source and trace links.

- React rating: Qwen trace contains raw/parsed `7 out of 10`; audit value also says `7 out of 10`. You report manually editing it to `7`; this correction is not attributed to automation. Exact point-in-time DOM values and edit timestamp are not present in local artifacts.
- Project count: the answer is a prose paragraph claiming “four” while listing five projects (TalentStream, MLS, HMEL, E-Commerce, Wordbase). Candidate context supports the named projects, but does not say the exact number built “from scratch”; the count is unsupported and the response type mismatches “How many”.
- AI/LLM developer tools: captured `1` year. Candidate context supports AI work and user-confirmed one year generative AI experience, but not clearly “AI/LLM developer tools” tenure. **UNVERIFIABLE** without definition/confirmation.
- CI/CD workflows: captured `1` year; candidate context lists GitHub Actions and CI/CD skills but no confirmed duration. **UNVERIFIABLE**.
- Current and expected CTC: captured values match verified annual INR figures; supported. Notice period (15 days) and joining date align with candidate context/user-confirmed facts, though local evidence doesn't show browser acceptance.
- Required Yes/No: this attempt has no captured binary answer among fields; the ending result says two required fields remained. Which exact questions were unanswered is **NOT_CAPTURED**.
- Draft status: saved draft with incomplete required fields, not a submitted application.

## Repeated prompt/error patterns

Prompt-level repetitions are listed in `qwen_prompt_response_audit.csv`. Of 137 answer traces, 136 carry a legacy prompt instruction to “give the best reasonable estimate” even for eligibility, identity, certification, consent, and legal-attestation questions, and to answer consent from apparent intent. One later trace uses a validation-oriented prompt that requests exact evidence and human review. This is historical behavior and should not be conflated with the newly implemented pipeline.

Observed repeated or concrete wrong-answer patterns:

- The repeated legacy instruction above is the broadest prompt defect: it directly licenses guessing for personal/legal answers and durations.
- For machine-learning framework, NLP/computer-vision, and model-training years (job `4486239068`), the raw Qwen response is `0` in three traces, while parsed output changes each to `1`; the candidate context does not evidence professional tenure in those fields. These are unsupported parsed claims. Trace rows retain raw and parsed values separately.
- For CI/CD workflows on the Alternative Path draft (job `4485592376`), raw responses are `0` on two traces and parsed output is `1`; field audit captures `1`. Context lists GitHub Actions/CI tooling but does not verify a year, so the entered year is unverifiable. For AI/LLM developer tools, raw `1 year` and parsed `1` also lack category-specific tenure evidence.
- For “Years of experience in e-commerce” (job `4425104015`), raw `1-3 years` is normalized to parsed `Less than 1 year` in two traces. Candidate evidence does not provide an exact e-commerce professional duration.
- Rating prompts can return prose (`7 out of 10` or longer descriptions) where a numeric field is requested. On the known React question, the manually edited `7` is user-reported and must not be credited to automation.

1. Broad experience estimates: machine-learning related prompts returned raw `0` but parsed `1` for three separate questions; CI/CD raw `0` became parsed `1` twice.
2. Numeric project-count prompts: Qwen can produce explanatory prose and an unsupported number instead of a bare count; example above.
3. Numeric ratings: Qwen sometimes emits prose instead of the numeric token; user edit is distinct from model output.
4. E-commerce tenure: raw range `1-3 years` became parsed `Less than 1 year` twice.
5. Required radio/Yes-No questions: the legacy prompt allowed inference, but traces and application audit do not always prove that the answer was retained in the browser.

## Repeated application failure questions

The logs give `field_label`, phase, and exception for some fill failures, but do not consistently identify the exact question causing required-field validation. Failure phase/event rollup and repeated labels are included in field CSV; specific unanswered required questions cannot be reconstructed without form snapshots. The Alternative Path example ended with two “This field is required” messages, with no labels captured. Therefore this audit does not assert a repeated question-level failure beyond the demonstrated numeric/count/radio issue families.

## Risk/manual review

See `high_risk_drafts.csv` for drafts with HIGH/CRITICAL classified risks. All attempts in `draft_answer_audit.csv` are candidates for review when status is incomplete, uncertain, saved, or when a field was classified. Prioritize any record with unsupported project count, AI-generated numeric tenure, personal/legal Yes/No, unverified dropdown/radio selection, or failed required-field validation. Do not infer option lists where prompt metadata omitted them.

## Candidate facts to add or clarify in verified answer registry

- Exact number of projects built from scratch, and criteria for counting employer/client PoCs versus personal projects.
- Whether AI/LLM developer-tool work has a distinct professional duration, with scope definition (using AI coding tools vs building LLM products).
- CI/CD workflow duration (if desired), distinguishing use of GitHub Actions from professional tenure.
- Verified Yes/No responses for recurring GitHub commit threshold and other binary personal/eligibility questions, with exact question wording.
- A canonical numeric rating per requested skill (rating scale and skill label); only DSA 7/10 is specifically documented; do not generalize it to Python/React/Node.
- Verified application field rules/semantics where prompts use vague terms (e.g., “projects from scratch”, “AI/LLM developer tools”).

## Final answers

1. Problematic answer drafts: **15** under the captured-field classification; incomplete drafts with only missing answers are additionally in manual review list.
2. Unsupported/contradictory factual claims: **1** strict classification; unverifiable claims are not counted as unsupported.
3. Malformed/type-mismatched values: **14**.
4. Repeated risky Qwen prompt classes: numerical project counts, ratings, and experience tenure inferred from broad skills. See trace CSV for exact prompts and raw responses.
5. Repeated failure questions: exact question-level pattern cannot be proven from current artifacts. Required-field errors recur, but generally lack label snapshots; see source logs via run_id/attempt_id.
6. Manual review: **191** attempt IDs in `draft_answer_audit.csv` (includes saved/incomplete/uncertain, and classified field issues); HIGH/CRITICAL subset in `high_risk_drafts.csv`.
7. Registry updates: clarify project count, AI/LLM tools tenure, CI/CD tenure, per-skill numeric ratings, and recurring binary responses.

## Limitations

Audit classification is conservative and based on local candidate context, registry, Qwen trace, audit record, and diagnostic logs. A field audit value is not treated as proof of the browser's final DOM. “SUPPORTED” indicates evidence matches candidate facts; no browser acceptance is implied. Missing prompt metadata/options or source records remain `NOT_CAPTURED`/`UNVERIFIABLE`. User-supplied manual edit fact is only asserted for the React rating as requested.
