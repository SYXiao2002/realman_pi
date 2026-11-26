"""Public reachability and full-solution API."""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

from .analytic_ik import solve_analytic
from .config_loader import RobotConfig, load_config
from .types import ReachabilityResult, SolveAllResult


class IKSolver:
    """Configured analytic reachability checker for the RealMan RM65-B."""

    def __init__(self, config_path: str | Path | None = None, *, config: RobotConfig | None = None):
        if config is not None and config_path is not None:
            raise ValueError("pass either config_path or config, not both")
        self.config = config if config is not None else load_config(config_path)

    def check_reachable(
        self,
        target_xyzrpy: Sequence[float],
        tool_xyzrpy: Sequence[float] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    ) -> ReachabilityResult:
        """Return as soon as one limit-compliant solution is found."""
        solutions, reason = solve_analytic(
            target_xyzrpy,
            tool_xyzrpy,
            self.config,
            first_solution_only=True,
            enumerate_shoulder_singularity=True,
        )
        return {
            "reachable": bool(solutions),
            "valid_solution_count": 1 if solutions else 0,
            "reason": None if solutions else reason,
        }

    def solve_all(
        self,
        target_xyzrpy: Sequence[float],
        tool_xyzrpy: Sequence[float] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    ) -> SolveAllResult:
        """Return all distinct legal joint configurations in radians."""
        solutions, reason = solve_analytic(
            target_xyzrpy,
            tool_xyzrpy,
            self.config,
            enumerate_shoulder_singularity=True,
        )
        return {
            "reachable": bool(solutions),
            "valid_solution_count": len(solutions),
            "reason": None if solutions else reason,
            "solutions": solutions,
        }


@lru_cache(maxsize=1)
def _default_solver() -> IKSolver:
    return IKSolver()


def check_reachable(
    target_xyzrpy: Sequence[float],
    tool_xyzrpy: Sequence[float] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
) -> ReachabilityResult:
    """Module-level convenience wrapper using the bundled RM65-B model."""
    return _default_solver().check_reachable(target_xyzrpy, tool_xyzrpy)


def solve_all(
    target_xyzrpy: Sequence[float],
    tool_xyzrpy: Sequence[float] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
) -> SolveAllResult:
    """Module-level convenience wrapper using the bundled RM65-B model."""
    return _default_solver().solve_all(target_xyzrpy, tool_xyzrpy)
