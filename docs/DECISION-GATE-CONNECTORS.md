# Connector decision-gate adapter contract

Audience: connector and wrapper maintainers. This is an agent/operator ingress, not normal chat usage.

OMH does not implement Discord, Slack, or other network clients. An adapter authenticates its own event, extracts opaque identifiers, and calls one of these equivalent surfaces:

- CLI: `omh runtime decision-gate answer`
- Hermes plugin: `omh_decision_gate`

The CLI is an explicit local operator ingress. The plugin is a host-adapter ingress: it uses the same receipt writer and wrapper-consumption operation, but fails closed unless the host passes a separate trusted event context.

## Required binding

Supply only metadata, never message text, tokens, credentials, URLs, transcripts, or display names:

- `gate_id` and `expected_resume_digest` rendered with the open gate;
- declared `choice` (`approve`, `decline`, or `defer`);
- stable authenticated `actor`, plus `authentication_method` of `host_authenticated` or `signed_connector_event`;
- opaque `connector`, `channel_ref`, `thread_ref`, `interaction_ref`, and stable `event_id`;
- ISO-8601 `observed_at` time;
- the named `wrapper_session_ref` and the session `wrapper_expected_revision`.

`actor` is checked for equality with the gate approver. Bare actor text, free-form replies, or an adapter claim without one of the accepted authentication methods are invalid. The adapter remains responsible for authentication; OMH records only the bounded method label and does not claim credential verification.

`authentication_method` in model tool arguments is not host authority. A plugin host must pass the same complete, closed event mapping through the adapter-only `trusted_connector_event=` keyword. OMH parses that host mapping through the same validator and requires it to equal the model request across actor, connector/channel/thread/interaction/event references, gate and digest, choice, reviewed wrapper session/revision, authentication label, and observed time. An unconfigured host context, an unknown field, or any mismatch is refused before OMH resolves a state path.

## Behavior

OMH takes the existing OS lock on `runtime/decision_gates.jsonl`, validates the current bound gate and wrapper session revision, durably records an event-to-answer transaction, then appends the v1 answer and immutable `connector_decision_gate_receipt/v1` record. The receipt binds the gate, answer record, actor, choice, observed time, expected revision digest, connector references, event id, and wrapper session revision. The matching wrapper transition consumes that receipt afterwards.

The durable transaction makes a retry recover an append-after-answer/before-receipt crash only for the same event. `event_id` is the idempotency key: an identical event returns `already_applied`; changed content under the same id returns `invalid` with `conflicting_replay`; a new event cannot reuse an answered gate. Two concurrent answers cannot both win under the shared store lock. Connector observation time must be between gate opening and trusted application time; gate expiry is evaluated at trusted application and wrapper-consumption time.

Results use bounded statuses: `applied`, `already_applied`, `expired`, `superseded`, `unauthorized`, `invalid_choice`, `stale_revision`, `unknown_gate`, `cancelled`, or `invalid`. No result echoes message bodies, tokens, credentials, or raw connector data.

## Runnable opening and answer flow

A connector must open a **wrapper-bound** gate, not a generic gate. The helper reads the current wrapper session and derives the session subject, reviewed revision, `plan_accepted` transition, and checkpoint. Those fields cannot be supplied later by the connector event.

```py
from omh.paths import resolve_paths
from omh.wrapper_sessions import open_wrapper_session_decision_gate

paths = resolve_paths(omh_home, hermes_home)
gate = open_wrapper_session_decision_gate(
    paths,
    session_id="ws-opaque",              # an existing plan_presented session
    approver="authenticated-actor-opaque",
    safety_profile_revision="safety-revision-opaque",
)
# Render only gate["gate_id"] and gate["resume_digest"] to the adapter.
```

The adapter then executes:

```sh
omh --omh-home "$OMH_HOME" --hermes-home "$HERMES_HOME" runtime decision-gate answer \
  --gate-id "$GATE_ID" --expected-resume-digest "$RESUME_DIGEST" \
  --choice approve --actor authenticated-actor-opaque \
  --connector slack --channel-ref channel-opaque --thread-ref thread-opaque \
  --interaction-ref interaction-opaque --authentication-method host_authenticated \
  --observed-at 2026-09-07T10:05:00Z --event-id delivery-opaque \
  --wrapper-session-ref ws-opaque --wrapper-expected-revision 1
```

The plugin accepts the same fields through `omh_decision_gate`, but `omh_decision_gate_handler()` additionally requires host-owned `trusted_connector_event=` metadata that independently contains the identical closed event. Tool/model arguments alone are refused with `untrusted_host_context`; differing host data is refused with `host_context_mismatch`. This is an adapter capability boundary, not cryptographic verification by OMH. The plugin rejects unknown top-level fields, including raw/body fields, and writes only to the configured OMH home; it has no caller-selected mutable path.

A host adapter calls the handler after authenticating and parsing its connector event. It must not derive the host event from tool arguments:

```py
result_json = omh_decision_gate_handler(
    model_tool_args,
    trusted_connector_event=host_parsed_connector_event,
)
```

Both mappings must independently contain exactly the required metadata fields above with equal values. Passing no host event fails closed.

## Integration pending

`src/plugin_bundle/omh/metadata.py` must add `omh_decision_gate` and `decision_gate_tool` to `PROVIDED_TOOLS` and `TOOL_FILE_STEMS`, with the corresponding generated/plugin-distribution projections and exact-count tests updated by the shared registration owner. This lane registers the runtime tool in `__init__.py` but intentionally does not edit that shared metadata registry.
