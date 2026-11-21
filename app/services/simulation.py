"""X-Simulate-Failure parser — ARCHITECTURE.md §4.6, API_SPEC.md §4.

Pure function → immutable per-request plan. Nothing here touches global state, key
health or attempt order; the failover loop consults the plan for one request only.

Grammar:  entry ("," entry)*   entry := (key_id | "*") "=" (429|500|502|503)
- whitespace and target case are tolerated
- an explicit key entry beats "*" (whatever the order); among duplicates the last wins
- malformed entries, disallowed statuses and unknown key ids are ignored (recorded)
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

ALLOWED_STATUSES = frozenset({429, 500, 502, 503})
WILDCARD = "*"
MAX_ENTRIES = 64  # a header can't make the parser do unbounded work
MAX_IGNORED_RECORDED = 10

_KEY_ID = re.compile(r"^key_[1-9][0-9]*$")


@dataclass(frozen=True)
class SimulationPlan:
    failures: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    wildcard: int | None = None
    ignored: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return bool(self.failures) or self.wildcard is not None

    def status_for(self, key_id: str) -> int | None:
        return self.failures.get(key_id, self.wildcard)

    def describe(self) -> str:
        parts = [f"{k}={v}" for k, v in sorted(self.failures.items())]
        if self.wildcard is not None:
            parts.append(f"*={self.wildcard}")
        return ",".join(parts) or "none"

    @classmethod
    def none(cls) -> SimulationPlan:
        return _NONE


_NONE = SimulationPlan()


def parse_simulation_header(value: str | None, known_key_ids: Iterable[str]) -> SimulationPlan:
    if value is None or not value.strip():
        return _NONE
    known = set(known_key_ids)
    failures: dict[str, int] = {}
    wildcard: int | None = None
    ignored: list[str] = []

    def ignore(entry: str) -> None:
        if len(ignored) < MAX_IGNORED_RECORDED:
            ignored.append(entry[:40])

    entries = [e.strip() for e in value.split(",")]
    for entry in [e for e in entries if e][:MAX_ENTRIES]:
        parts = entry.split("=")
        if len(parts) != 2:
            ignore(entry)
            continue
        target, raw_status = parts[0].strip().lower(), parts[1].strip()
        if not raw_status.isdigit() or int(raw_status) not in ALLOWED_STATUSES:
            ignore(entry)
            continue
        status = int(raw_status)
        if target == WILDCARD:
            wildcard = status
        elif _KEY_ID.match(target) and target in known:
            failures[target] = status
        else:
            ignore(entry)

    if not failures and wildcard is None and not ignored:
        return _NONE
    return SimulationPlan(MappingProxyType(failures), wildcard, tuple(ignored))
