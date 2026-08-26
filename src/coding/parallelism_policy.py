"""Execution-concurrency policy for the opt-in fanout dispatch path.

Defaults mirror OMO's task engine (per-lane default 5, global ceiling 8) on
owner direction. Every value is a setup-profile tunable under `parallelism`,
read with the same validated-override-plus-disclosure shape as the memory
policy's cadence block: an invalid stored value falls back to the default
and is named in `ignored_keys` instead of failing the read or silently
winning. OMH core still makes no LLM or network calls — this policy only
sizes the explicit `omh coding fanout dispatch` subprocess pool and its
per-owner lanes, and states the advisory lane budget a Hermes-native run
may read; it never patches Hermes and never enforces anything there.
"""

from __future__ import annotations

from typing import Any

from ..profiles.setup import (
    PARALLELISM_DEFAULTS,
    PARALLELISM_POLICY_SCHEMA_VERSION,
    read_setup_profile,
)
from ..system.paths import OmhPaths

# One config typo must not turn the fanout pool into a fork bomb: 32 is far
# above any real local agent-CLI fleet while still catching a stray 500.
_PARALLELISM_MAX = 32

_COUNT_KEYS = ("default_concurrency", "global_concurrency", "lane_budget_default")

PARALLELISM_CLAIM_BOUNDARY = (
    "Parallelism policy sizes the explicit `omh coding fanout dispatch` "
    "subprocess pool only. Hermes-native lanes read `lane_budget_default` as "
    "advisory context; OMH never enforces a lane count inside Hermes."
)


def _bounded_count(policy: dict[str, Any], key: str) -> int | None:
    """One validated lane/pool width from a policy mapping, or None."""
    value = policy.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= _PARALLELISM_MAX:
        return value
    return None


def build_parallelism_policy() -> dict[str, Any]:
    return {
        "schema_version": PARALLELISM_POLICY_SCHEMA_VERSION,
        **dict(PARALLELISM_DEFAULTS),
        "per_owner": {},
        "ignored_keys": [],
        "claim_boundary": PARALLELISM_CLAIM_BOUNDARY,
    }


def read_parallelism_policy(paths: OmhPaths) -> dict[str, Any]:
    """The effective policy: stored `parallelism` overrides on the defaults."""
    policy = build_parallelism_policy()
    setup = read_setup_profile(paths)
    stored = setup.get("parallelism") if isinstance(setup, dict) else None
    if not isinstance(stored, dict):
        return policy
    ignored: list[str] = []
    for key in _COUNT_KEYS:
        if key not in stored:
            continue
        value = _bounded_count(stored, key)
        if value is None:
            ignored.append(key)
        else:
            policy[key] = value
    raw_owners = stored.get("per_owner")
    if isinstance(raw_owners, dict):
        for owner, value in raw_owners.items():
            label = owner.strip() if isinstance(owner, str) else ""
            width = (
                value
                if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= _PARALLELISM_MAX
                else None
            )
            if label and width is not None:
                policy["per_owner"][label] = width
            else:
                ignored.append("per_owner." + (label or repr(owner)))
    elif "per_owner" in stored:
        ignored.append("per_owner")
    # A per-lane default above the global ceiling would promise lanes the
    # pool cannot grant; clamp and disclose rather than refuse the profile.
    if policy["default_concurrency"] > policy["global_concurrency"]:
        policy["default_concurrency_clamped_from"] = policy["default_concurrency"]
        policy["default_concurrency"] = policy["global_concurrency"]
    policy["ignored_keys"] = ignored
    return policy


def resolve_fanout_concurrency(policy: dict[str, Any], requested: int | None) -> dict[str, Any]:
    """The pool width one dispatch runs with, and where it came from.

    An explicit `--concurrency` flag wins over the policy default; the
    global ceiling always clamps, and a clamp is disclosed so the operator
    sees the requested and applied widths side by side.
    """
    global_cap = int(policy.get("global_concurrency", PARALLELISM_DEFAULTS["global_concurrency"]))
    if requested is None:
        wanted = int(policy.get("default_concurrency", PARALLELISM_DEFAULTS["default_concurrency"]))
        source = "policy_default"
    else:
        wanted = max(1, int(requested))
        source = "cli_flag"
    resolution: dict[str, Any] = {
        "schema_version": PARALLELISM_POLICY_SCHEMA_VERSION,
        "requested": requested,
        "applied": min(wanted, global_cap),
        "source": source,
        "global_concurrency": global_cap,
        "clamped": wanted > global_cap,
        # The reader's disclosures ride along so the dispatch record — the
        # one surface an operator reads after the fact — names an ignored
        # profile key or a clamped default instead of discarding the finding.
        "ignored_keys": list(policy.get("ignored_keys", [])),
    }
    clamped_from = policy.get("default_concurrency_clamped_from")
    if source == "policy_default" and clamped_from is not None:
        resolution["clamped"] = True
        resolution["policy_clamped_from"] = clamped_from
    return resolution
