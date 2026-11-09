# Corrected Historical Job Application Lifecycle

Snapshot generated 2026-11-09T00:31:33.989831+05:30. Source artifacts were read locally and sequentially; some source files were actively appended during the investigation, so this is not an atomic multi-file snapshot. No LinkedIn access or automation execution occurred.

## Scope and integrity

- Unique job IDs observed: **1590**. Total discovered is unavailable without a complete search/discovery ledger.
- Distinct audit attempts: **736**; missing IDs: **0**. The attempt export has one row per ID.
- Jobs with an audit attempt: **640**.
- Previous reviewed export had 1,587 jobs, 720 attempts and 100 confirmed submissions. The live local audit advanced during this targeted revision; current source snapshot has 1590 jobs, 736 attempts and 107 confirmed submissions.
- `previous_coverage_category` remains only as a historical crosswalk; `corrected_lifecycle_classification` is the new mutually exclusive result.

## Current job-level lifecycle metrics

- **unique_job_ids_observed:** 1590
- **unique_jobs_discovered_total:** unavailable: no complete discovery/search-result ledger with job IDs
- **AI_evaluated:** 791
- **AI_APPLY_ever:** 164
- **AI_REVIEW_ever:** 552
- **AI_SKIP_ever:** 174
- **latest_nonAI_skip_with_known_provenance:** 988
- **latest_true_eligibility_filter_rejections:** 567
- **latest_history_protection_skips:** 421
- **ever_deterministic_filter_rejected:** 1027
- **application_attempt_started:** 640
- **opened_job_directly_observed:** 16
- **opened_job_unknown:** 1574
- **easy_apply_found_directly_observed:** 277
- **application_modal_opened:** 274
- **form_interaction:** 268
- **draft_saved:** 114
- **draft_save_or_dialog_cleanup_evidence:** 124
- **dialog_cleanup_evidence:** 13
- **submit_clicked:** 11
- **confirmed_submissions:** 107
- **confirmed_per_attempted_jobs_rate:** 0.1672
- **submission_uncertain:** 3
- **submit_clicked_direct_evidence:** 11
- **submit_action_attempted_direct_evidence:** 15

### Latest application status by job

| Status | Jobs |
|---|---:|
| error | 73 |
| in_progress | 1 |
| skip | 454 |
| submitted | 107 |
| submitting | 2 |
| uncertain | 3 |
| unknown | 950 |

### Corrected mutually exclusive classifications

| Classification | Jobs |
|---|---:|
| AI_rejected_no_application_attempt | 382 |
| application_attempted_outcome_unknown | 27 |
| application_started_incomplete_or_error | 67 |
| blocked_by_application_history_or_duplicate_protection | 18 |
| confirmed_submitted | 107 |
| decision_provenance_unknown | 2 |
| deterministic_eligibility_rejection | 547 |
| draft_saved_incomplete | 108 |
| easy_apply_selector_failure | 90 |
| easy_apply_unavailable | 236 |
| not_evaluated_or_state_unknown | 1 |
| submission_pending_unconfirmed | 2 |
| submission_uncertain | 3 |

The independent event flags use `true`, `false`, or `unknown`. `unknown` means no direct evidence was captured. Positive states now have source references in the job row `source_provenance`; successful submission never sets a Submit-click flag by itself.

## Attempt-level metrics

- **unique_attempts_started:** 736
- **attempts_with_form_interaction:** 256
- **draft_saves:** 139
- **failed_attempts:** 366
- **easy_apply_unavailable_attempts:** 243
- **easy_apply_selector_failures:** 95
- **application_attempts_other_skip:** 7
- **completed_form_attempts:** 109
- **uncertain_submissions:** 3
- **submitting_unconfirmed:** 2
- **confirmed_submissions:** 107
- **confirmed_per_started_attempt_rate:** 0.1454
- **confirmed_per_form_interaction_attempt_rate:** 0.418
- **attempt_ids_missing:** 0
- **audit_attempt_ids_distinct:** 736
- **diagnostic_events:** 22
- **diagnostic_events_with_attempt_id:** 22
- **unique_attempt_ids:** 736
- **submit_action_attempted_direct:** 15
- **submit_clicked_direct:** 11
- **confirmed_but_no_separate_click_event:** 97
- **explicit_draft_saves:** 139
- **attempt_ids_in_attempts_csv:** 736

Supported conversion rates: 107/640 jobs with attempt evidence = 16.7%; 107/736 attempts = 14.5%; 107/256 form-interaction attempts = 41.8%.

## AI decision to application attempt investigation

The raw audit yields 156 jobs with latest AI REVIEW/SKIP, no recorded APPLY, and an application attempt; there are 171 distinct attempts for them. 142 starts across 142 jobs came after the latest AI decision; 29 attempts were at or before it.
Among post-decision attempts, 13 confirmed after REVIEW and 10 after SKIP. Start deltas were 0.007585–0.023348 seconds (mean 0.012573).

The paired audit records do not contain run IDs; no shared run ID was found. Prompt and trace records are absent inside those decision-to-attempt windows, and there are no run result files dated Nov 2–3. This is a historical chronology anomaly, not proof of a current same-run gate bypass. See `decision_attempt_linkage.csv` and `decision_gate_investigation.md` for each record and version analysis.

## Submit evidence semantics

- Submit action attempted: 15 attempt records with direct stage/status evidence.
- Submit clicked: 11 attempts with a click-specific diagnostic or explicit “Submit was clicked” text.
- Confirmed submitted: 107 attempts.
- Confirmed attempts without a separate click event: 97. These remain confirmed; the click flag stays unknown.

The separate fields are `submit_action_attempted`, `submit_clicked`, and `submission_confirmed` in attempts.csv, and their `ever_` forms in jobs.csv.

## Candidate-profile bottlenecks

| Question group | Distinct jobs | Distinct attempts | Question occurrences |
|---|---:|---:|---:|
| Candidate identity and education | 4 | 4 | 5 |
| Compensation | 9 | 9 | 9 |
| Legal and employment attestations | 6 | 6 | 6 |
| Location and relocation | 10 | 10 | 10 |
| Notice period and availability | 6 | 6 | 6 |
| Other/unclassified | 17 | 17 | 17 |
| Technical experience | 44 | 55 | 56 |
| Written project descriptions | 10 | 13 | 20 |

Question groups are based on explicit unanswered, ambiguous or verification-failure wording. Entered personal values are excluded. Full labels are in the summary JSON and failure CSV.

## Rejection provenance

Filter, application-history, duplicate protection, and unknown-source decisions remain separate in `job_rejections.csv`. Exact reasons and provenance are retained; unrecognized provenance remains unknown.

## Discrepancies from the previous report

- The previous 21 AI APPLY/no-start jobs had audit attempts; 15 had explicit draft-save evidence.
- The previous 874 deterministic category was mixed: the raw source reasons resolve the reviewed set into 522 eligibility filters, 350 history/duplicate protections, and 2 unknown.
- The previous 77 incomplete/uncertain jobs had audit attempts; the revised exports now preserve each attempt and separate draft, error, uncertain, submitting, click and confirmed states.
- Previous click semantics inferred a click from successful submission. The revised direct-click count is {clicked_attempt}; {confirmed_no_click} confirmed attempts have no separate click event evidence.
- The current local source grew during analysis from the reviewed 720 attempt / 100 submission export to the current source snapshot above. No source artifacts or automation code were modified.

## Files

- `jobs.csv`
- `attempts.csv`
- `job_application_failures.csv`
- `job_rejections.csv`
- `confirmed_submissions.csv`
- `decision_attempt_linkage.csv`
- `decision_gate_investigation.md`
- `job_coverage_summary.json`
