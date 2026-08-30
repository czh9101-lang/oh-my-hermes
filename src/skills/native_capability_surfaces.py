from __future__ import annotations

from collections.abc import Callable


def native_capability_skill_definitions(feature_surface_skill: Callable[..., object]) -> tuple[object, ...]:
    return (
        feature_surface_skill(
            "decision-recall",
            "[omh] Recall scoped reviewed rejected decisions without elevating them to approved memory.",
            (
                "decision-recall",
                "rejected decision recall",
                "rejected decisions",
                "why was this rejected",
                "previously rejected alternative",
            ),
            "Use for scoped reviewed rejected-decision context; it is not approved memory or execution evidence.",
            category="memory",
            phase="decision-recall",
            next_action="show_rejected_decision_recall",
            boundary="Rejected-decision context is reviewed OMH-local context, not approved memory, Hermes memory, or execution evidence.",
            good_prompt="Show rejected decisions for this project before we choose an alternative.",
            bad_prompt="Claim the recalled rejected decision is an approved memory write or proof the replacement ran.",
            expected_outputs=("rejected_decision_recall/v1", "scoped rejected-decision matches", "claim boundary"),
            artifact_expectations=("rejected_decision_recall/v1 metadata-only recall result",),
            final_checklist=(
                "The query, scope, tags, stale policy, and match limit are explicit.",
                "Only reviewed rejected candidates are returned; expired candidates stay excluded.",
                "Recall output is not presented as approved memory, source freshness, or execution evidence.",
            ),
        ),
        feature_surface_skill(
            "run-efficiency",
            "[omh] Report supplied local run efficiency while provider and host data stay unobserved.",
            (
                "run-efficiency",
                "run efficiency report",
                "local run efficiency",
                "context utilization",
                "tool duration report",
            ),
            "Use for a bounded local efficiency report from supplied metadata with provider and host gaps explicit.",
            category="observability",
            phase="run-efficiency",
            next_action="show_run_efficiency_report",
            boundary="Run efficiency is supplied OMH-local metadata, not provider, billing, cron, or host evidence.",
            good_prompt="Show the local run efficiency report from this run's supplied context budget and timings.",
            bad_prompt="Claim this report proves provider billing, host load, or cron execution without observations.",
            expected_outputs=("run_efficiency_report/v1", "context utilization", "not_observed provider and host gaps"),
            artifact_expectations=("run_efficiency_report/v1 metadata-only report",),
            final_checklist=(
                "The run ID, context budget, surfaces, and supplied observations are explicit.",
                "Provider billing, cron, and host claims remain not_observed unless separately recorded.",
                "The report does not intercept, route, or execute provider or host work.",
            ),
        ),
        feature_surface_skill(
            "provider-profile-posture",
            "[omh] Prepare provider-profile metadata without reading secrets or calling providers.",
            (
                "provider-profile-posture",
                "provider profile posture",
                "provider profile readiness",
                "secret presence confirmation",
                "connector profile posture",
            ),
            "Use for provider/profile capability and secret-presence preparation before connector or credential action.",
            category="operations",
            phase="provider-profile-posture",
            next_action="prepare_provider_profile_posture",
            boundary="Provider/profile posture is OMH-local preparation metadata; it is not credential validation, provider connectivity, model routing, payment/wallet, or host execution evidence.",
            good_prompt="Prepare provider profile posture for this connector using metadata-only secret presence.",
            bad_prompt="Read the secret, validate the credential, call the provider, or create a payment route.",
            expected_outputs=("provider_profile_posture/v1", "metadata-only secret requirements", "allowed and prohibited actions"),
            artifact_expectations=("provider_profile_posture/v1 metadata-only preparation record",),
            final_checklist=(
                "Provider ID, profile ID, requested capabilities, and secret-presence metadata are explicit.",
                "No secret value, credential validation, provider call, model route, wallet, or payment action is claimed.",
                "Any host observation reference remains supplied metadata, not a live connector check.",
            ),
        ),
    )


def native_capability_harnesses(feature_surface_harness: Callable[..., object]) -> tuple[object, ...]:
    return (
        feature_surface_harness(
            "decision-recall",
            "Prepare a scope-limited recall of reviewed rejected decisions.",
            "Use when an operator needs past rejected alternatives before a new decision.",
            ("query", "scope", "optional tags", "stale policy", "result limit"),
            ("rejected_decision_recall/v1", "ranked rejected candidates", "claim boundary"),
            quality_tier="workflow-surface-gated",
            evidence_ladder=("query_scoped", "rejected_candidates_matched", "recall_rendered"),
            wrapper_actions=("show_rejected_decision_recall",),
            overclaim_guard="A rejected-decision recall is not approved memory, Hermes memory, source freshness, or execution evidence.",
        ),
        feature_surface_harness(
            "run-efficiency",
            "Render a deterministic report from supplied local run metadata.",
            "Use when an operator has run/context-budget metadata and needs a bounded latency or context report.",
            ("run ID", "context budget", "surface counts", "supplied observations"),
            ("run_efficiency_report/v1", "context utilization", "not_observed gaps"),
            quality_tier="workflow-surface-gated",
            evidence_ladder=("input_validated", "local_report_prepared", "not_observed_gaps_rendered"),
            wrapper_actions=("show_run_efficiency_report",),
            overclaim_guard="A run efficiency report is not provider billing, cron, host, or performance proof beyond supplied local metadata.",
        ),
        feature_surface_harness(
            "provider-profile-posture",
            "Prepare a provider/profile posture card without accessing a provider or secret value.",
            "Use before adopting a connector profile when capability and secret-presence requirements need a safe review boundary.",
            ("provider ID", "profile ID", "requested capabilities", "secret-presence metadata"),
            ("provider_profile_posture/v1", "allowed actions", "prohibited actions"),
            quality_tier="workflow-surface-gated",
            evidence_ladder=("profile_scoped", "secret_presence_declared", "posture_prepared"),
            wrapper_actions=("prepare_provider_profile_posture",),
            overclaim_guard="A provider/profile posture is not secret access, credential validation, provider connectivity, model routing, payment, wallet, or host execution evidence.",
        ),
    )


def native_capability_surface_exposures(surface_exposure: Callable[..., object]) -> tuple[object, ...]:
    projections = ("routable", "installable", "playbook", "harness", "workflow_reference", "capability")
    return (
        surface_exposure(
            "decision-recall",
            "workflow_skill",
            projections,
            True,
            "primary_workflow_skill",
            "Use as an installed Hermes workflow skill to recall scoped rejected alternatives without promoting them to approved memory or execution evidence.",
        ),
        surface_exposure(
            "run-efficiency",
            "workflow_skill",
            projections,
            True,
            "primary_workflow_skill",
            "Use as an installed Hermes workflow skill to render bounded local run efficiency from supplied metadata and keep provider/host gaps visible.",
        ),
        surface_exposure(
            "provider-profile-posture",
            "workflow_skill",
            projections,
            True,
            "primary_workflow_skill",
            "Use as an installed Hermes workflow skill to prepare provider/profile metadata without reading secrets, calling providers, or routing models.",
        ),
    )
