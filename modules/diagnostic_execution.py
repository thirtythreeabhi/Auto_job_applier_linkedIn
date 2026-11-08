"""Fail-closed controls for a single-job Selenium diagnostic run."""
from __future__ import annotations

import os
import threading


class DiagnosticConfigurationError(RuntimeError):
    pass


class DiagnosticJobNotFound(RuntimeError):
    pass


class DiagnosticJobAmbiguous(RuntimeError):
    pass


class DiagnosticAttemptLimitExceeded(RuntimeError):
    pass


class DiagnosticSubmissionBlocked(RuntimeError):
    pass


class DiagnosticAttemptBudget:
    """Single-use, job-bound attempt budget shared across the whole run."""
    def __init__(self, job_id):
        job_id = str(job_id or '')
        if not job_id.isdigit():
            raise DiagnosticConfigurationError('A numeric LinkedIn job ID is required for diagnostic mode.')
        self.job_id = job_id
        self.consumed = 0
        self._lock = threading.Lock()

    def consume(self, job_id):
        job_id = str(job_id or '')
        with self._lock:
            if job_id != self.job_id:
                raise DiagnosticConfigurationError(
                    f'Diagnostic job mismatch: expected {self.job_id}, received {job_id or "missing"}.'
                )
            if self.consumed >= 1:
                raise DiagnosticAttemptLimitExceeded(
                    f'Diagnostic attempt budget for job {self.job_id} has already been consumed.'
                )
            self.consumed += 1
            return self.consumed


def unique_target_card(cards, target_job_id, get_job_id):
    """Return only the exact requested card, failing if the page duplicates it."""
    target = str(target_job_id or '')
    matches = [card for card in cards if str(get_job_id(card) or '') == target]
    if len(matches) > 1:
        raise DiagnosticJobAmbiguous(
            f'LinkedIn showed {len(matches)} result cards for requested job {target}; refusing to select one.'
        )
    return matches[0] if matches else None


def assert_submission_allowed(*, diagnostic=False):
    if diagnostic or os.environ.get('BOT_DIAGNOSTIC_JOB_ID'):
        raise DiagnosticSubmissionBlocked(
            'Submit clicks are prohibited during a diagnostic execution.'
        )


def perform_submission_click(click, *args, diagnostic=False, **kwargs):
    """Hard guard shared by every actual Submit click path."""
    assert_submission_allowed(diagnostic=diagnostic)
    return click(*args, **kwargs)
