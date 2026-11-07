"""Detached, redacting log reader for the automation worker subprocess."""
from __future__ import annotations

import sys

from modules.run_diagnostics import sanitize_log_line


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    try:
        with open(sys.argv[1], 'a', encoding='utf-8') as log_file:
            for line in sys.stdin:
                log_file.write(sanitize_log_line(line))
                log_file.flush()
    except OSError:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
