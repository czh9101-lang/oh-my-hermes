# Temporal source receipts

Point-in-time web evidence for `web-research`, `research`, and every workflow
that cites a page as of a date. The contract lives in
`src/workflows/temporal_source_receipts.py`; this page explains what it proves,
what it refuses, and why three clocks stay apart.

## The user goal

A person asks Hermes what a page said as of a date, what was known on the web
before a cutoff, or how an earlier snapshot compares with the page today. The
answer must bind every historical claim to a capture that existed at or before
the cutoff, and must report a claim it cannot bind as a gap instead of quietly
answering from the current page.

## Three clocks, none of which stands in for another

| Clock | Field | What it proves | What it does not prove |
| --- | --- | --- | --- |
| Capture time | `captured_at` | An archive or capture provider observed the page at that instant. | That the time is correct beyond the provider's word (`captured_at_attribution` is always `provider_reported`). |
| Publication time | not a receipt field | What the page says about itself. | That the page existed, or was unchanged, before the cutoff. It never enters a receipt. |
| Retrieval time | `retrieved_at` | When Hermes or a wrapper fetched the capture or the live page. | Anything about the page's content at any earlier time. |

A page retrieved today with a publication date before the cutoff is current
evidence. It becomes historical evidence only through a capture whose
provider-reported time is at or before the cutoff.

## `temporal_source_receipt/v1`

One receipt binds one cited source to one requested cutoff.

| Field | Meaning |
| --- | --- |
| `as_of` | `{"kind": "date", "date": ...}` or `{"kind": "interval", "start": ..., "end": ...}`. The cutoff is the date (whole day, UTC) or the interval's end. |
| `source_url`, `source_class` | Canonical http(s) URL without credentials; `upstream_official`, `practitioner`, or `unattributed`. |
| `evidence_kind` | `historical_capture` or `live_page`. The two are typed apart and never merged. |
| `retrieved_at` | When the capture or live page was fetched. Required on an available receipt; never defaulted by the builder. |
| `capture_provider`, `capture_provider_class` | Opaque provider identity and `archive_service`, `native_tool`, `external_connector`, or `none`. A historical capture must name its provider; a live page must not. |
| `captured_at`, `captured_at_attribution` | Provider-reported capture time, or empty with attribution `unknown`. Nothing else fills it in. |
| `capture_ref`, `content_digest` | Stable capture identifier from the provider and a sha256 of the cited text. An eligible capture carries at least one. |
| `cutoff_relation` | `at_or_before`, `after`, or `unknown`. Derived from `captured_at` against the cutoff and re-checked at validation. |
| `availability`, `failure_reason` | `available`, `unavailable`, `access_denied`, `provider_authority_exhausted`, or `not_attempted`, with one bounded reason line when not available. |

`build_temporal_source_receipt` mints a receipt or raises; `validate_temporal_source_receipt`
returns every violation. The validator refuses:

- a live page presented as historical (a capture time, capture id, provider,
  or cutoff relation on `evidence_kind: live_page`);
- a historical capture with no provider;
- a `captured_at` that is not an ISO-8601 timestamp;
- a `cutoff_relation` that disagrees with the timestamps, including
  `at_or_before` with no `captured_at`;
- an available receipt with no `retrieved_at`, or an unavailable one with no
  `failure_reason`.

It accepts a receipt whose capture time, capture id, and digest are empty when
the provider reported none, because an unknown stays unknown.

## Eligibility and gaps

`receipt_supports_as_of_claim` is the one predicate: an available historical
capture, at or before the cutoff, with a capture id or digest. Anything else
yields a `temporal_retrieval_gap/v1` from `as_of_claim_gap`, and the claim it
would have backed goes to the research artifact's unresolved annex.

| Gap kind | When |
| --- | --- |
| `capture_after_cutoff` | The only capture postdates the cutoff. |
| `capture_time_unknown` | The provider reported no capture time. |
| `capture_unverifiable` | No stable capture id or digest, or the receipt itself is invalid. |
| `capture_unavailable` | The provider had no capture. |
| `archive_access_unavailable` | Access was denied or no archive was reachable from this run. |
| `provider_authority_exhausted` | A paid or credentialed provider's authority or budget refused the retrieval. |
| `live_page_only` | Only the live page was retrieved. |

Every gap carries `network_action: none` and `resolution: abstain`. A missing
archive or an exhausted provider is reported, never worked around, and paid
access keeps flowing through the existing readiness and cost-authority gates.

## Then versus now

`build_temporal_evidence_surfaces` splits the receipts for one cutoff into
`historical_captures`, `live_evidence`, and `unresolved_annex`. A comparison
answer cites from the first two separately; a receipt for a different `as_of`
is refused rather than re-dated.

## Routing and consumers

- `guard:point_in_time_web` fires on an explicit cutoff or capture phrase
  ("as of 2026-06-01", "archived capture", "then versus now", "as it was on",
  and their Korean, Japanese, and Chinese forms) combined with a web context
  (page, site, docs, pricing, archive, snapshot). "as of" alone, a snapshot
  test, or archived logs do not fire it; the negative controls in
  `src/quality/routing_precision.py` pin that.
- The web research card then carries `temporal_evidence` naming the receipt,
  surfaces, and gap schemas and stating that historical claims require a
  receipt and that a live page is not historical evidence.
- `research_briefing/v1` sources may carry a `temporal_source_receipt`
  unchanged; the briefing validates it with the receipt's own validator,
  refuses a citation whose class or URL contradicts it, and renders the
  capture clock next to the retrieval clock.

## Boundaries

OMH ships no archive service, no network client, and installs no provider.
Captures come from installed native tools or external connectors the operator
already has; a receipt attributes what such a tool reported and is prepared
research context, not proof that any retrieval was executed. A historical
capture can be incomplete, corrected, or removed: the receipt proves the
observed capture, not the truth of the page.
