# Host-owned browser effects

Operator/integrator reference. This consumer is opt-in, not a browser daemon
or default mutation surface. Ordinary `omh_browser` remains inert/read-only;
explicit trusted host setup can add the closed effect operations below to that
same tool, only inside active request admission.

`omh.workflows.browser_effect_attempts.BrowserEffectEngine` orchestrates trusted
injected callbacks. Core makes no browser, subprocess, network, or provider call.
The host owns the browser, deadlines, request admission, and authentic approval.

## Registered Hermes tool

The existing `browser_adapter` settings require `enabled: true` and
`effects_enabled: true`. Before plugin registration the trusted host supplies
`ctx.browser_adapter` and a bounded, indexed
`ctx.browser_effect_approval(identity, task, intent_digest)` resolver. `identity`
is the actual `(session_id, principal, transport_family, message_id)` tuple.
The resolver must return the current `approval_receipt/v1` head for that exact
identity/task/intent, including denial or revocation, not scan approval history.
An absent resolver or absent adapter methods leaves the original inert schema.
The adapter must advertise `last_mile` interception; unsupported capabilities
are refused before preview. The host adapter is trusted code, not a sandbox.

The host binds Hermes session ContextVars, then enters `ctx.browser_task(task)`
around both definition assembly and dispatch. Nonbrowser, inactive, foreign,
expired, and exited request scopes expose zero browser schemas. Both host schema
caches must support request-bound bypass; unsupported hosts fail closed.

`omh_browser` accepts these additional operations only when opted in:

- `effect_preview`: `action` plus exactly `lease_id`, `tab_id`, `revision`,
  `handle`, `trace_revision`, and `expected_postcondition` as defined below.
- `effect_execute`: only `intent_digest` besides `operation`.
- `effect_abort`: only `intent_digest` besides `operation`.

The registered schema has closed operation branches. Hermes may normalize away
top-level schema unions for provider compatibility; the registered handler still
enforces every required field and rejects all extra effect fields. Model JSON
cannot supply owner, approval, event identity, engine, callback, or store path.
Execution reads the actual `_approval_tool_call_id` and `_approval_session_id`
ContextVars bound by Hermes around registry dispatch. An omitted/foreign event
fails closed. JSON and handler keyword arguments cannot substitute for it.
An event is bound even when approval is absent, so it cannot switch intents.

Each invocation constructs its engine against the same manager adapter and
canonical OMH root, with transaction-local callbacks. No profile-cached engine
captures the first request's authority or mutates shared callbacks. Admission,
request identity, adapter binding, task ownership and approval are rechecked at
execution checkpoints. Host approval resolution must be bounded and must not
reenter lease lifecycle operations while the lease lock is held.

`BrowserSessionManager.effect_boundary` holds the existing lease-store OS lock
through preview/execute/abort, serializing them with inert reads, observations,
release and cleanup. New approved attempts reserve the same `action_count` and
cap, durably before resume. Preview, denied approval and exact replay spend no
new action. Upload requires lease `upload`; other supported effects require
lease `click`. Approval cannot widen either scope. A consumed/crashed budget
reservation is never refunded. Release and scope/session cleanup reap held
bytes through the owning adapter; admission is revoked before scope cleanup.
Native `browser_*` mutators remain blocked, even with effects enabled.

## Engine contract

Construct with an explicit OMH home, the #1391 adapter, an indexed
`lease_source(owner, lease_id)` returning the current verified lease view, and
an indexed `approval_source(intent_digest)` returning the current host-owned
`approval_receipt/v1` head (including revocation/denial). Neither callback is a
model argument. The engine cannot authenticate a malicious injected host.

`preview(owner, request)` accepts exactly:

- `lease_id`, `tab_id`, `revision`, `handle`
- `operation`: submit, send, publish, upload, purchase, payment,
  credential_change, or destructive
- `trace_revision`: null or an exact SHA-256 revision reference
- `expected_postcondition`: confirmation

An inert `read` returns `read_only` without touching a store or adapter.
Unknown actions, eval, raw click/navigation, and opaque payloads are blocked.
No tool name, label, HTTP method, or same-origin relationship establishes safety.
Only an adapter advertising `mutation_interception=last_mile` can preview effects.

Preview resolves the exact existing handle, reobserves state, and asks the host
to inspect/hold without firing events or discovering intent through requests.
It freezes owner/adapter/lease/tab/state/URL/target/operation/trace/postcondition
and the closed `browser_pending_effect/v1` metadata. Raw tab identifiers are
hashed in persistence; canonical origin is retained, query values are not.
The result is `awaiting_approval`, not an attempt or delivery observation.

The host displays that redacted intent and obtains explicit approval. Existing
approval vocabulary is reused: `external_posting`, scope class `tool`, scope
reference equal to the complete intent digest, owner equal to owner hash, run
equal to lease id, safety revision equal to state digest. Use `approval_scope`
to build those exact dimensions. The existing receipt validator, lifecycle
predicate, and TTL apply; the live lease/capability deadline further bounds use.
A model-supplied boolean or receipt is never accepted by the engine's API.

`execute(owner, intent_digest, host_event_id)` rechecks the actual page and held
metadata, validates the current trusted approval, then calls the unchanged
generic `AttemptStore.open_attempt` with `external_write` / `endpoint`. Only
`created` may resume. Both the generic FULL SQLite commit and browser unknown
reservation commit precede resume. The host rechecks actual target/state/bytes
atomically and releases only the exact approved request, once.

One intent has one attempt even with different host-event ids. Reusing an event
for another intent is refused. Replays return the stored result; an in-flight
or crashed reservation returns `unknown`, without reobservation or resend.
No automatic reconnect, retry, selector repair, or approval transfer exists.
`abort(owner, intent_digest)` permanently cancels an unexecuted held intent.

## Readback and persistence

The return value of `resume` is discarded. A separate bounded `observe` must
produce a valid #1391 page and a closed `effect_readback` object containing:
`schema_version=browser_effect_readback/v1`, `observed=true`, exact `attempt_id`
and `preview_ref`, `postcondition=confirmation`, and SHA-256 `object_digest`.
Only then is `external_effect_receipt/v1` minted with `browser_mutation` /
`endpoint` / `browser_adapter_readback`. It cannot satisfy review, CI, or merge
claims. Missing, malformed, ambiguous, crashed, or late readback is `unknown`.

The existing journal paths remain authoritative:

- `runtime/journal/external_effect_attempts.sqlite3`: unchanged generic attempts
- `runtime/journal/external_effect_receipts.jsonl`: existing validated append/fsync
- `runtime/browser/effects.sqlite3`: bounded browser bindings, event index,
  stable results, and indexed copies of the exact linked receipt

Bindings/events are capped at 256 without eviction; each JSON column is capped
at 4096 bytes. Primary/unique indexes cover intent, held preview, host event,
and receipt lookup. The engine never scans receipt/approval history. Legacy
receipt append checks only the tail, while replay/receipt lookup uses SQLite.
Raw payloads, headers, credentials, forms, selectors, DOM, screenshots, page
text, and transcripts never enter these stores. Diagnostics/screenshots remain
explicit host-owned evidence outside runtime stores.

## Local reference and proof

`tools/browser_last_mile_adapter.py` + `.mjs` are an explicit, local-only host
reference using already installed Node Playwright and Chromium. No dependency
or browser installation is performed. A fresh context has JavaScript disabled,
service workers blocked, and default-deny routing. Only a host-named loopback
fixture form endpoint is supported. Preview inspects transient form data;
resume rechecks it and emits a fixed host request carrying the exact frozen
bytes. Routing checks frame, endpoint, method, an ephemeral host nonce, and
byte equality. Forwarding has zero retries and zero redirect following; every
other request is aborted. Upload is a preselected synthetic fixture file with
deterministic multipart bytes. This is not universal website classification.

The QA script supplies trusted synthetic-fixture approvals, not a real human
connector observation. It proves browser interception, durable ordering,
readback, and cleanup without using accounts, models, or providers:

```sh
uv run python .omc/artifacts/issues-1391-1397/qa_browser_effects.py \
  --artifacts .omc/artifacts/issues-1391-1397/effects-1392/my-unique-run
uv run python .omc/artifacts/issues-1391-1397/qa_browser_effects_plugin.py \
  --artifacts .omc/artifacts/issues-1391-1397/effects-1392/my-plugin-run
PYTHONPATH=tests uv run python -m unittest tests/test_browser_effects_plugin.py -v
PYTHONPATH=tests uv run python -m unittest tests/test_browser_effect_attempts.py -v
PYTHONPATH=tests uv run python tools/benchmarks/browser_effects.py
```

The QA artifact directory must not exist. JSON goes to stdout, progress to
stderr, so concurrent callers select their own receipt destinations. Contexts,
process descendants, loopback server, and temporary HOME/TMPDIR are owned by
`finally`/context managers. A forced cleanup is not reported as clean evidence.
The plugin driver uses installed Hermes `PluginContext`, registered `ToolEntry`
handlers and real `PluginManager` hooks, including inner/outer definition-cache
transitions. Its parent owns temporary roots through worker process exit.
See `effects-1392/plugin-integration-report.md` in the issue artifacts for the
original acceptance checklist mapped to final evidence. This local fixture proof
does not claim arbitrary websites or a real human approval connector were tested.
