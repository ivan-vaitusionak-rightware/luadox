# Copyright 2026 Rightware
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

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
        Reports each non-allowed category with all of its entries, and returns
        the process exit code: 1 if any non-allowed category has entries.
        """
        failing = {cat: entries for cat, entries in self.entries.items()
                   if cat not in self.allowed}
        for category, entries in sorted(failing.items()):
            log.error('%d %s problem(s) leave the documentation incomplete:',
                      len(entries), category)
            for entry in entries:
                log.error('  %s:%s: %s', entry.file, entry.line, entry.message)
        if failing:
            log.error('fix the documentation source, or set allow_incomplete = %s '
                      'to accept the omission', ', '.join(sorted(failing)))
            return 1
        return 0
