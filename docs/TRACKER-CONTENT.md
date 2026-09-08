# Tracker content ingress

`tracker_content_envelope/v1` is the closed ingress contract for untrusted GitHub tracker evidence. It prevents provider text from becoming an OMH operator instruction.

## Adapter contract

A connector supplies one closed payload container, either `tracker_content` or `github_event`:

```json
{
  "tracker_content": {
    "provider": "github",
    "event_type": "issues",
    "payload": {
      "repository": {"id": "literal-provider-id"},
      "issue": {"id": "literal-object-id", "number": 1381, "title": "...", "body": "..."}
    }
  }
}
```

Supported `event_type` values are `issues`, `pull_request`, and `issue_comment`. An `issue_comment` must declare subtype `issue` or `pull_request`; inline review comments (`pull_request_review_comment`) are terminally unsupported until they receive their own adapter contract.

The host supplies independent observed facts to `normalize_tracker_content()` or `build_chat_interaction_payload()`:

```json
{
  "authenticated": true,
  "fetch_status": "ok",
  "delivery_id": "provider-delivery-id",
  "replay_digest": "optional previously stored envelope digest"
}
```

Authentication, fetch success, delivery identity, replay state, and a future accepted scope decision are host facts. They are never read from a tracker body, label, author, association, display name, or claimed approval.

## Result and safety boundary

The envelope contains only immutable coordinates, event metadata, exact SHA-256 digests, lengths, truncation-free bounded status, and a fixed `github-event-ops` route. It always sets:

- `trust: "untrusted"`
- `authority_effect: "none"`
- `route: "github-event-ops"`

Raw title, body, comment, and diff text are transient while digesting and are never projected into `user_message`, wrapper metadata, a role marker, routing selector, executor choice, target-path parser, approval transition, or coding handoff. The wrapper produces a metadata-only card and keeps `coding_enabled: false`; a later host-observed scope acceptance must create a separate coding request. Replaying a delivery never creates a task, handoff, artifact, write, retry, or model turn.

When a host invokes the existing `pre_llm_call` hook for a tracker turn, it passes the original event separately as `tracker_event` (or `github_event`). The hook then skips role parsing of the flattened transient message. This is a suppression boundary only: it does not authenticate, normalize, route, or authorize the event.

`accepted` is the first host-observed delivery. `already_seen` means the same delivery ID had the exact stored digest. A different stored digest for the same delivery returns terminal `delivery_conflict`.

Malformed, oversized, unsupported, unauthenticated, ambiguous, fetch-failed, and missing-delivery inputs are terminal `blocked` envelopes. They do not fall back to generic chat extraction. The parser rejects duplicate JSON keys and non-finite values; normalization also bounds aggregate input size (72 KiB), content bytes (64 KiB), nesting depth (12), and content-part count (3).

## CLI behavior

`omh chat interact --event-json FILE` and `omh chat route --event-json FILE` use the same ingress boundary. The CLI does not authenticate a provider, so tracker events passed directly to it become an `unauthenticated` metadata-only blocked card. A real connector must call the public wrapper with separately observed host facts.
