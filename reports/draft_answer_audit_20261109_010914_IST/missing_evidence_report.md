# Missing evidence report

Generated 2026-11-09T01:09:14.703799+05:30.

## Capture gaps

- Historical records do not consistently persist validator-accepted answer, normalized answer, Selenium argument, actual DOM value, or browser acceptance. In field CSV these are labeled `NOT_CAPTURED`.
- Exact maxlength, numeric bounds, required status and choice option lists are absent in many prompt traces. Never infer choices.
- Failure screenshots are present for some run IDs, but not every attempt; diagnostic event fields usually provide failure phase/label but not a serialized DOM.
- Some errors report only a count such as “This field is required”; the missing labels are unknown.
- Attempt-level status and Qwen `application_outcome` can differ in timestamp/scope; neither substitutes for a persisted draft DOM.
- Candidate evidence gaps: precise project count; exact AI/LLM developer tooling tenure; CI/CD tenure; per-technology self-ratings (except specifically documented DSA); answers to recurring binary/personal/legal questions.
- Resume files exist locally; this audit used the explicit candidate knowledge base and verified registry as authoritative sources and did not extract personal facts from PDFs beyond those already recorded there.

## Reconstruction levels

For each question, the field-level CSV keeps source captured value, trace raw response and parsed response separately. “Validator accepted”, “Selenium sent”, “DOM value”, and “browser validation” remain unavailable unless independently captured; `NOT_CAPTURED` is intentional. The known React correction is user-reported and not encoded as an automation value.

## Evidence locators

Use job_id, attempt_id, run_id and trace_id columns in the CSV files. Source artifacts: `application_field_audit.json`, `.ai_traces.sqlite3`, `.ai_prompt_log.jsonl`, `logs/runs/<run_id>/events.jsonl`, `application_attempts.jsonl`, `worker.log`, and any `failure.png` in the same run.
