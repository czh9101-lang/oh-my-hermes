"""Deterministic, fail-closed unittest sharding tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class ShardingError(ValueError):
    """A configuration or reconciliation defect in test sharding."""

    reason: str

    def __str__(self) -> str:
        return self.reason
