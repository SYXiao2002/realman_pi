"""Public result and reason-code definitions."""

from __future__ import annotations

from enum import Enum
from typing import TypedDict


class FailureReason(str, Enum):
    OUT_OF_REACH = "OUT_OF_REACH"
    JOINT_LIMIT = "JOINT_LIMIT"
    INVALID_POSE = "INVALID_POSE"
    NUMERICAL_FAILURE = "NUMERICAL_FAILURE"


class ReachabilityResult(TypedDict):
    reachable: bool
    valid_solution_count: int
    reason: str | None


class SolveAllResult(ReachabilityResult):
    solutions: list[list[float]]
