"""Adaptive submission-window state for the explicit fanout bridge."""

from __future__ import annotations

from typing import Any, Mapping


class AdaptiveFanoutAdmission:
    """Start conservatively and grow after clean observed completions."""

    def __init__(self, *, ceiling: int) -> None:
        self.ceiling = max(1, int(ceiling))
        self.window = min(2, self.ceiling)

    def available_slots(self, inflight: int) -> int:
        return max(0, self.window - max(0, int(inflight)))

    def observe(self, result: Mapping[str, Any]) -> None:
        if (
            result.get("status") == "completed"
            and result.get("process_succeeded") is True
            and result.get("exit_code") == 0
        ):
            self.window = min(self.ceiling, self.window + 1)
