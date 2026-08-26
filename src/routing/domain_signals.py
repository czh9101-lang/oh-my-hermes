from dataclasses import dataclass
import re

from ..plugin_bundle.omh.domain_signals import (
    _fold_for_match,
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


@dataclass(frozen=True)
class ClarificationRelevance:
    skills: tuple[str, ...] | None

    @property
    def applies(self) -> bool:
        return self.skills is not None


def _relevance_pattern(cue: str) -> re.Pattern[str]:
    phrase = _fold_for_match(cue)
    pattern = re.escape(phrase).replace(r"\ ", r"[\s_-]+")
    return re.compile(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])")


_RELEVANCE_ONLY_PATTERNS = tuple(
    (skill, tuple(_relevance_pattern(cue) for cue in cues))
    for skill, cues in _RELEVANCE_ONLY_CUES
)
_UNOWNED_SPECIALIST_PATTERNS = tuple(_relevance_pattern(cue) for cue in _UNOWNED_SPECIALIST_CUES)


def _normalized_relevance_message(message: str) -> str:
    return _fold_for_match(message)


def classify_clarification_relevance(message: str) -> ClarificationRelevance:
    """Classify request relevance after exactly one request normalization."""
    normalized = _normalized_relevance_message(message)
    matched = tuple(
        skill
        for skill, patterns in _RELEVANCE_ONLY_PATTERNS
        if any(pattern.search(normalized) is not None for pattern in patterns)
    )
    if matched:
        return ClarificationRelevance(matched)
    if any(pattern.search(normalized) is not None for pattern in _UNOWNED_SPECIALIST_PATTERNS):
        return ClarificationRelevance(())
    return ClarificationRelevance(None)


def clarification_relevance_skills(message: str) -> tuple[str, ...] | None:
    """Compatibility projection for callers that do not retain classification."""
    return classify_clarification_relevance(message).skills


__all__ = (
    "ClarificationRelevance",
    "DomainOperatorOverride",
    "DomainRouteSignal",
    "RELEVANCE_POLICY",
    "clarification_relevance_skills",
    "classify_clarification_relevance",
    "specialist_domain_operator_override",
    "specialist_domain_route_signal",
)
