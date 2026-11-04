"""Append local, inspectable records of prompts sent to an AI model."""

from datetime import datetime, timezone
import json
from pathlib import Path
import threading

PROMPT_LOG_PATH = Path(__file__).resolve().parents[2] / '.ai_prompt_log.jsonl'
_LOCK = threading.Lock()


def record_prompt(category, prompt, response='', model='', provider='', error=''):
    """Store one model call without allowing logging errors to interrupt automation."""
    record = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'category': str(category),
        'provider': str(provider),
        'model': str(model),
        'prompt': str(prompt),
        'response': str(response),
        'error': str(error),
    }
    try:
        with _LOCK, PROMPT_LOG_PATH.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + '\n')
    except OSError:
        pass
