from ..plugin_bundle.omh.domain_signals import (
    _contains_cue_phrase as contains_domain_cue_phrase,
    DomainOperatorOverride,
    DomainRouteSignal,
    specialist_domain_operator_override,
    specialist_domain_route_signal,
)

RELEVANCE_POLICY = "shared_domain_signal/v1"

# These practitioner terms only establish whether a clarification candidate
# shares the request's domain. They are intentionally separate from the
# dispatch trigger catalog in the plugin bundle.
_RELEVANCE_ONLY_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "finance-analysis",
        (
            "dso",
            "revenue cutoff",
            "revenue recognition",
            "asc 606",
            "burn multiple",
            "nrr",
            "net revenue retention",
        ),
    ),
    (
        "legal-compliance-review",
        (
            "indemnity",
            "liability cap",
            "gdpr article 35",
            "dpia",
        ),
    ),
    ("sales-development", ("meddpicc",)),
)

# The source observation classified this vocabulary as specialist-shaped, but
# it is not sufficient to nominate a catalog skill. It therefore activates the
# relevance gate while deliberately yielding no named candidate.
_UNOWNED_SPECIALIST_CUES = (
    "four-fifths rule",
    "4/5 rule",
    "bloom backward design",
    "bloom taxonomy",
)


def clarification_relevance_skills(message: str) -> tuple[str, ...] | None:
    """Return relevant candidate skills, or None when this policy does not apply.

    An empty tuple is meaningful: specialist vocabulary was recognized, but no
    catalog candidate owns a sufficiently shared canonical signal.
    """
    matched = tuple(
        skill
        for skill, cues in _RELEVANCE_ONLY_CUES
        if any(contains_domain_cue_phrase(message, cue) for cue in cues)
    )
    if matched:
        return matched
    if any(contains_domain_cue_phrase(message, cue) for cue in _UNOWNED_SPECIALIST_CUES):
        return ()
    return None


__all__ = (
    "DomainOperatorOverride",
    "DomainRouteSignal",
    "RELEVANCE_POLICY",
    "clarification_relevance_skills",
    "specialist_domain_operator_override",
    "specialist_domain_route_signal",
)
