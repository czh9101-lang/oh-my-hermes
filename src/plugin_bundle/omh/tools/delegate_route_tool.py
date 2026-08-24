from __future__ import annotations

import json
from typing import Any

from ..delegation_routing import read_delegation_route, write_delegation_route
from ..hermes_delegation import (
    HERMES_MIXTURE_CATEGORY_CHAINS,
    chain_alias_for,
    effective_mixture_category_chains,
    load_mixture_chain_overrides,
    load_model_provider_routes,
    mixture_chain_overrides_path,
    model_provider_routes_path,
    resolve_provider_model,
)
from ..host_observation import OBSERVATION_SCHEMA, attach_public_observation, observe_plugin_tool_call

_EVIDENCE_BOUNDARY = (
    "Prepared route only: the delegation.* keys apply to the NEXT delegate_task "
    "dispatch (Hermes re-reads config.yaml per dispatch). Writing a route is not "
    "execution, dispatch, or completion evidence."
)

def _chain_entry(
    alias: str,
    effort: str,
    routes: dict[str, tuple[str, str]],
) -> dict[str, str]:
    """Render one chain position.

    `model` stays the alias the chain is written in, so a host with no
    provider routes sees exactly what it always saw. The dispatched values
    appear beside it only when a route actually applies, which is also the
    only case where they differ.
    """
    entry = {"model": alias, "reasoning_effort": effort}
    wire_model, provider = resolve_provider_model(alias, routes=routes)
    if provider:
        entry["provider"] = provider
        entry["wire_model"] = wire_model
    return entry


OMH_DELEGATE_ROUTE_SCHEMA = {
    "name": "omh_delegate_route",
    "description": (
        "Route the NEXT Hermes-native delegate_task dispatch onto a mixture model "
        "category (ultrabrain, deep, architect, unspecified-high, unspecified-low, quick, "
        "writing, visual-engineering, artistry) by writing the delegation.model / "
        "delegation.reasoning_effort keys Hermes reads per dispatch. Sequence per lane: "
        "set the route, call delegate_task for that lane, then set the next lane's route "
        "or clear to restore parent inheritance. Children already running keep their model. "
        "Hermes has NO provider-side fallback: a child whose model the billing account "
        "cannot serve dies on an HTTP 400 yet its delegation still reports completed with "
        "the error text as the result — a completed child with no recorded model usage "
        "means exactly this. When that happens call action=fallback to advance the route "
        "to the category chain's next candidate and re-dispatch; an exhausted chain "
        "clears the route so the next dispatch inherits the parent's working model."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["set", "clear", "status", "fallback"],
                "description": (
                    "set writes a route; clear removes the routable keys so children "
                    "inherit the parent again; status reads the current route; fallback "
                    "advances the current route to the next candidate in its category "
                    "chain (clearing to parent inheritance once the chain is exhausted) "
                    "— use it after a dispatched child completed with no model usage."
                ),
            },
            "category": {
                "type": "string",
                "enum": sorted(HERMES_MIXTURE_CATEGORY_CHAINS),
                "description": (
                    "Mixture category to route to; resolves to the chain head "
                    "(e.g. ultrabrain -> gpt-5.6-sol xhigh). Required for set unless "
                    "an explicit model is given."
                ),
            },
            "model": {
                "type": "string",
                "description": (
                    "Explicit model id override; wins over the category's chain head. "
                    "Use for a fallback candidate when the head is unavailable, or to pin "
                    "a model the user named for the run (e.g. 'use fable'): keep the "
                    "fitting category for the lane label and pass the user's model (plus "
                    "reasoning_effort) here on every lane of that run."
                ),
            },
            "reasoning_effort": {
                "type": "string",
                "description": (
                    "Explicit reasoning effort override (e.g. low, medium, high, xhigh). "
                    "Defaults to the chain entry's declared effort; omitted keys inherit "
                    "the parent session's level."
                ),
            },
            "provider": {
                "type": "string",
                "description": (
                    "Optional delegation.provider override for models that live on a "
                    "different provider than the parent session. Requires that provider "
                    "to be configured in Hermes."
                ),
            },
            "hermes_home": {
                "type": "string",
                "description": "Optional HERMES_HOME override. Defaults to ~/.hermes.",
            },
            "omh_home": {
                "type": "string",
                "description": (
                    "Optional OMH home override for the chain-override document "
                    "(routing/model-chains.json). Defaults to ~/.omh."
                ),
            },
            "observation": OBSERVATION_SCHEMA,
        },
    },
}


def omh_delegate_route_handler(args: dict[str, Any], **kwargs) -> str:
    observation = observe_plugin_tool_call("omh_delegate_route", args, kwargs)
    action = str(args.get("action", "") or "set").strip().lower()
    hermes_home = str(args.get("hermes_home", "") or "") or None
    omh_home = str(args.get("omh_home", "") or "") or None
    # Every chain read below honors the user's routing/model-chains.json
    # overrides; the category vocabulary itself stays the shipped closed set.
    chains = effective_mixture_category_chains(omh_home)
    _, override_status = load_mixture_chain_overrides(omh_home)
    # Chains name models the way a person says them. A host that reaches
    # models through a provider needs a provider id and that provider's own
    # (often namespaced) model string; routing/model-providers.json supplies
    # that mapping. With no document every alias dispatches unchanged.
    provider_routes, provider_route_status = load_model_provider_routes(omh_home)

    if action == "status":
        payload: dict[str, Any] = {
            "status": "status",
            "route": read_delegation_route(hermes_home),
            "categories": {
                category: [
                    _chain_entry(alias, effort, provider_routes)
                    for alias, effort in chain
                ]
                for category, chain in chains.items()
            },
            "chain_overrides": override_status,
            "chain_overrides_path": str(mixture_chain_overrides_path(omh_home)),
            "provider_routes": provider_route_status,
            "provider_routes_path": str(model_provider_routes_path(omh_home)),
            "evidence_boundary": _EVIDENCE_BOUNDARY,
        }
        return json.dumps(attach_public_observation(payload, observation), sort_keys=True)

    if action == "clear":
        result = write_delegation_route(hermes_home, clear=True)
        result["evidence_boundary"] = _EVIDENCE_BOUNDARY
        return json.dumps(attach_public_observation(result, observation), sort_keys=True)

    if action == "fallback":
        route = read_delegation_route(hermes_home)
        current_model = str(route.get("model", ""))
        # The live route holds whatever was dispatched, which is the provider's
        # wire model when a route applied. Chain positions are keyed by alias,
        # so translate back before any chain lookup below -- otherwise a routed
        # model reads as absent from the very chain it came from.
        current_alias = chain_alias_for(current_model, provider_routes)
        current_effort = str(route.get("reasoning_effort", ""))
        if not current_model:
            payload = {
                "status": "error",
                "error": "no active route to fall back from; use set with a category first",
            }
            return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
        category = str(args.get("category", "") or "").strip()
        if category and category not in chains:
            payload = {
                "status": "error",
                "error": (
                    f"unknown category {category!r}; choose one of "
                    + ", ".join(sorted(chains))
                ),
            }
            return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
        if not category:
            # A (model, effort) pair can sit in several chains — e.g.
            # glm-5.2-ultrafast:low ends unspecified-low AND heads quick.
            # Prefer the effort-exact match, and within each pool prefer a
            # chain that can still advance: fallback exists to move forward,
            # so an ambiguous route only reads as exhausted when NO matching
            # chain has a next candidate.
            def _chain_index(chain: tuple) -> int:
                return next((i for i, (alias, _) in enumerate(chain) if alias == current_alias), -1)

            exact = [
                name
                for name, chain in chains.items()
                if any(alias == current_alias and chain_effort == current_effort for alias, chain_effort in chain)
            ]
            loose = [
                name
                for name, chain in chains.items()
                if any(alias == current_alias for alias, _ in chain)
            ]
            for pool in (exact, loose):
                if not pool:
                    continue
                advancing = [
                    name
                    for name in pool
                    if 0 <= _chain_index(chains[name])
                    < len(chains[name]) - 1
                ]
                # Head-most wins: a route set from a category starts at that
                # chain's head, so when a (model, effort) pair sits in several
                # advancing chains, the one holding it earliest is the likely
                # origin (glm-5.2-ultrafast:low heads quick but is mid-chain
                # in unspecified-low). Explicit `category` overrides.
                category = min(
                    advancing or pool,
                    key=lambda name: _chain_index(chains[name]),
                )
                break
        if not category:
            payload = {
                "status": "error",
                "error": (
                    f"current route model {current_model!r} is not in any mixture chain; "
                    "pass category explicitly"
                ),
            }
            return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
        chain = chains[category]
        index = next((i for i, (alias, _) in enumerate(chain) if alias == current_alias), -1)
        if index < 0 or index + 1 >= len(chain):
            # Chain exhausted: restore inheritance so the next dispatch runs on
            # the parent's known-working model instead of one more rejection.
            result = write_delegation_route(hermes_home, clear=True)
            if result.get("status") == "cleared":
                result["status"] = "exhausted_to_inherit"
            result["category"] = category
            result["from"] = current_model
            result["evidence_boundary"] = _EVIDENCE_BOUNDARY
            return json.dumps(attach_public_observation(result, observation), sort_keys=True)
        next_model, next_effort = chain[index + 1]
        wire_model, next_provider = resolve_provider_model(
            next_model, routes=provider_routes
        )
        result = write_delegation_route(
            hermes_home,
            model=wire_model,
            reasoning_effort=next_effort,
            provider=next_provider,
        )
        if result.get("status") == "routed":
            result["status"] = "fell_back"
            result["category"] = category
            result["from"] = current_model
            result["fallback_candidates"] = [
                _chain_entry(alias, chain_effort, provider_routes)
                for alias, chain_effort in chain[index + 2 :]
            ]
        result["evidence_boundary"] = _EVIDENCE_BOUNDARY
        return json.dumps(attach_public_observation(result, observation), sort_keys=True)

    if action != "set":
        payload = {
            "status": "error",
            "error": f"unknown action {action!r}; use set, clear, status, or fallback",
        }
        return json.dumps(attach_public_observation(payload, observation), sort_keys=True)

    category = str(args.get("category", "") or "").strip()
    model = str(args.get("model", "") or "").strip()
    effort = str(args.get("reasoning_effort", "") or "").strip()
    provider = str(args.get("provider", "") or "").strip()
    if category and category not in chains:
        payload = {
            "status": "error",
            "error": (
                f"unknown category {category!r}; choose one of "
                + ", ".join(sorted(chains))
            ),
        }
        return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
    if not model:
        if not category:
            payload = {"status": "error", "error": "set needs a category or an explicit model"}
            return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
        head_model, head_effort = chains[category][0]
        model = head_model
        if not effort:
            effort = head_effort
    model, provider = resolve_provider_model(model, provider, provider_routes)
    result = write_delegation_route(
        hermes_home, model=model, reasoning_effort=effort, provider=provider
    )
    if result.get("status") == "routed":
        result["category"] = category
        chain = chains.get(category, ())
        result["fallback_candidates"] = [
            {"model": alias, "reasoning_effort": chain_effort}
            for alias, chain_effort in chain[1:]
        ]
    result["evidence_boundary"] = _EVIDENCE_BOUNDARY
    return json.dumps(attach_public_observation(result, observation), sort_keys=True)
