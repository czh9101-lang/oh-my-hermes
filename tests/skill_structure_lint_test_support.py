"""Shared deterministic fixtures for skill structure lint tests."""

from __future__ import annotations

import socket
from contextlib import contextmanager
from unittest import mock

from omh.skills.catalog import builtin_definitions


@contextmanager
def blocked_sockets():
    """Fail loudly if the lint reaches for the network.

    Patching `connect` rather than the whole module keeps local-only socket
    construction legal while making an actual dial an immediate error.
    """
    with mock.patch.object(
        socket.socket,
        "connect",
        side_effect=AssertionError("skill structure lint attempted a network connection"),
    ):
        yield


def definition(name: str):
    for catalog_definition in builtin_definitions():
        if catalog_definition.name == name:
            return catalog_definition
    raise AssertionError(f"missing catalog fixture skill: {name}")


def violated_rules(payload: dict[str, object]) -> set[str]:
    violations = payload["violations"]
    assert isinstance(violations, list)
    return {str(violation["rule"]) for violation in violations}
