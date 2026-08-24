__all__ = ['Diagnostics']

import re
from configparser import ConfigParser
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from .log import log


@dataclass
class Entry:
    file: Optional[str]
    line: Optional[int]
    message: str


class Diagnostics:
    """
    Collects problems that leave rendered documentation incomplete or wrong --
    missing snippets, bad type names -- without aborting the run.

    Each problem is logged as it is found and the run continues, so every
    problem surfaces in one run and rendering still completes for inspection.
    At the end of the run, summarize() reports each category and the run exits
    non-zero unless the category was explicitly accepted with the
    allow_incomplete config option (or --allow-incomplete on the command
    line), which downgrades its reports to warnings.
    """

    def __init__(self, allowed: Set[str]):
        self.allowed = allowed
        self.entries: Dict[str, List[Entry]] = {}

    @staticmethod
    def from_config(config: ConfigParser) -> 'Diagnostics':
        value = config.get('project', 'allow_incomplete', fallback='') or ''
        return Diagnostics({cat for cat in re.split(r'[,\s]+', value.strip()) if cat})

    def add(self, category: str, file: Optional[str], line: Optional[int], message: str) -> None:
        emit = log.warning if category in self.allowed else log.error
        emit('%s:%s: %s', file, line, message)
        self.entries.setdefault(category, []).append(Entry(file, line, message))

    def summarize(self) -> int:
        """
        Reports each category with all of its entries -- allowed categories as
        warnings, the rest as errors -- and returns the process exit code:
        1 if any non-allowed category has entries.
        """
        failing = False
        for category, entries in sorted(self.entries.items()):
            allowed = category in self.allowed
            failing = failing or not allowed
            emit = log.warning if allowed else log.error
            emit('%d %s problem(s) leave the documentation incomplete:',
                 len(entries), category)
            for entry in entries:
                emit('  %s:%s: %s', entry.file, entry.line, entry.message)
        if failing:
            log.error('fix the documentation source')
            return 1
        return 0
