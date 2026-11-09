# Decision-to-Application Gate Investigation

Snapshot: 2026-11-09T00:30:33.253543+05:30 (local workspace time). Read-only artifact review; no LinkedIn access or automation execution.

## Result

The audit contains **156 jobs** with a latest AI decision of REVIEW or SKIP, no recorded APPLY recommendation anywhere in the available AI decision history, and at least one application audit attempt. These jobs have **171 distinct attempt IDs**. **142 attempt starts across 142 jobs occurred after** the latest AI decision; **29 attempts** occurred at or before the latest decision. The base is Counter({'REVIEW': 105, 'SKIP': 51}) by latest AI recommendation.

Among the 142 post-decision attempts, **23 are confirmed submissions**: **13 after REVIEW and 10 after SKIP**. Attempt outcomes are {'submitted': 23, 'skip': 107, 'error': 10, 'in_progress': 1, 'uncertain': 1}. The application/audit status is not treated as evidence that a click was separately recorded.

Start time follows decision time by **0.007585–0.023348 seconds** (mean 0.012573 s), consistent with the review's millisecond-level timing observation. The CSV includes each attempt ID and exact timestamps.

## QuickLaunch example

- Job: Technical Architect at QuickLaunch (`4483024165`).
- Latest model decision: SKIP at `2026-11-03T06:02:34.324043+00:00` (decision ID `0429a784a165493094e6fa69348807e0`).
- Application attempt: `4ce87351987f4c849a5b11ab9a237d41`, started `2026-11-03T06:02:34.331739+00:00`, **0.007696 seconds later**; audit status is submitted.
- The audit has no run ID on either record. Prompt log has no job-ID record for this job. No trace is timestamped in the decision-to-attempt interval. No `logs/runs/*/result.json` covers Nov 3.

## Run and code-version correlation

Exact run correlation is **unavailable for these historical records**. In the 156-job set, both `run_id` fields are absent on the audit decision and application attempt; no same-run diagnostic event was found. The prompt log has no records for these 156 job IDs in the decision-to-attempt windows; SQLite traces have zero records within those intervals. There are no run result bundles dated Nov 2–3, and the attempt diagnostic JSONL does not cover these historical attempts. `decision_attempt_linkage.csv` lists direct run IDs as `unknown`; it also records any time-overlap candidate separately and does not claim it as attribution.

The nearest earlier committed code version is `7212e59` (`2026-11-03T09:35:48+05:30`). Its committed `modules/linkedin_modern.py` application loop has no suitability decision gate (and no suitability workflow in that module). The next relevant commit, `b798bae` (`2026-11-04T09:59:31+05:30`), adds AI workflow after these application timestamps. The historical runtime commit/hash was not recorded, so the audit decisions and application attempts cannot be tied to either commit or to the current dirty working tree. The current workspace source has an explicit `decision != 'APPLY'` skip at `modules/linkedin_modern.py:2353-2358`, but this does not establish which code executed on Nov 3.

**Conclusion:** The 142 post-decision attempts are a real chronology anomaly in the records. They warrant investigation of historical runtime artifacts, but the available artifacts do not distinguish a gate bypass from missing/misaligned decision-to-attempt provenance. This report does not label it a confirmed current bug.

## Submit evidence semantics

Job and attempt data now separate:

- `ever_submit_action_attempted` / `submit_action_attempted`: direct submitting state, diagnostic submit stage, or explicit “Submit was attempted/clicked” wording.
- `ever_submit_clicked` / `submit_clicked`: only an explicit “Submit was clicked” result or a diagnostic that advanced to waiting for confirmation after the click call. A successful submitted status does **not** populate click evidence.
- `ever_confirmed_submitted` / `submission_confirmed`: only explicit confirmed audit status/result.

There are **11 attempts with direct click evidence** and **107 confirmed submissions** in this updated source snapshot; **97 confirmed attempts have no separate click event record**. `unknown` is retained when the action cannot be established.

## Deliverables updated

`jobs.csv` now includes `corrected_lifecycle_classification` plus evidence-backed independent lifecycle flags. `attempts.csv` remains one row per distinct source attempt ID and now separates submit action, click, and confirmation. `decision_attempt_linkage.csv` contains one row per attempt for all 156 jobs in the REVIEW/SKIP no-APPLY denominator. The required failure, rejection, confirmed-submission, summary JSON, and main report files remain in this directory and were refreshed against the same source read.
