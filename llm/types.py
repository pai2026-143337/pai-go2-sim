"""Immutable outcomes; no SDK objects or credentials enter recordings."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RunResult:
    status: str
    explanation: str
    failure_reason: str | None
    llm_response_s: float | None
    motion_start_s: float | None
    total_wall_s: float
    tool_calls: tuple[dict, ...] = ()
    input_wall_ns: int | None = None
    failure_detail: dict | None = None
    llm_usage: tuple[dict, ...] = ()
    total_tokens: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)
