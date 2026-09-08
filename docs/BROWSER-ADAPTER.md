# Browser adapter and bounded host leases

This is an **operator/host integration reference**, not a new everyday CLI.
People request a browser task in Hermes chat. Browser-operator, web-QA, trace,
Hermes runtime/handoff, and generic wrappers can share this lifecycle; preparing
a task card alone does not acquire a browser or prove execution.

## Explicit host binding

The plugin reads only `ctx.get_config("browser_adapter", {})` at registration.
Enable with `plugins.entries.omh.settings.browser_adapter.enabled: true` in
host-owned configuration. A host integration supplies a trusted
`ctx.browser_adapter` object before calling the existing plugin `register(ctx)`.
No object is loaded from model arguments, an import path, or configuration code.
Optional `browser_adapter.omh_home` selects one private OMH root.

Enabled registration adds a schema-gated `omh_browser`, a pre-tool refusal
hook, a session-end cleanup hook, and the **host-only** `ctx.browser_task`
context manager. Configuration and adapter presence are not admission. An
integrating wrapper enters `with ctx.browser_task(accepted_task):` only for an
explicitly accepted browser request, before assembling that task's tool list,
and keeps the scope around execution. `accepted_task` must equal the tool's
`task` on acquisition; model arguments cannot enter this scope.

The scope reads Hermes' actual task-local session ID, authenticated browser
principal, transport family, and current message/request ID. They must already
be bound by the host in `gateway.session_context`; environment fallbacks and
model-supplied owners are never used. A different owner, principal, transport,
or message immediately hides the schema and blocks the handler. Admission
expires after at most 300 seconds. Exiting the scope or ending the session
revokes it, including copies carried into worker contexts, and reaps the
owner's resources through the registered adapter/version. Unknown cleanup is
not successful release; explicit session-end cleanup also supports recovery.

The installed Hermes boundary requires `_SESSION_ID`, `_SESSION_MESSAGE_ID`,
`_BROWSER_CONTROL_PRINCIPAL`, `_BROWSER_CONTROL_TRANSPORT_FAMILY`,
`no_cache_check_fn`, and `check_fn_cache_scope() == CHECK_FN_CACHE_BYPASS`.
These are host APIs, not an OMH patch. The registry's ordinary availability
cache has a TTL and last-good grace period; the outer `model_tools` definition
cache is profile-scoped. Admission therefore requires the host's existing
request-bound bypass of **both** caches, not cache invalidation or a persistent
availability flag. Missing/unbound APIs fail closed (`host_scope_unavailable`).
In particular, an unscoped classic CLI session is not automatically supported.
The wrapper must assemble tools within each admitted task and must not reuse
an admitted agent's frozen tool list for unrelated tasks. OMH does not patch
Hermes' agent runtime or infer acceptance from natural-language routing.

The tool accepts `acquire`, `observe`, `act`, and `release`, revalidating live
admission and current host ownership before any action or store access. Without
admission it returns `admission_required`, even if a caller retained a handler
or supplied admission fields in JSON. OMH does not install a daemon, invent a
browser connection, or claim that native tools consume these leases.

With the feature absent/false, there is no browser import, tool schema, hook,
capability discovery, context injection, store access, or browser start. The
enabled but unadmitted path exposes zero browser schema/context and makes no
browser starts, capability/model calls, or lease writes. The browser pre-hook
also returns immediately on non-browser calls. These
are incremental browser costs; existing unrelated OMH hooks retain their own
behavior. No policy CLI or always-on browser schema is introduced.

## Host-neutral API

`omh.workflows.browser_adapter.BrowserAdapter` defines the host callback seam.
`BrowserSessionManager(BrowserLeaseStore(omh_home), adapter, clock=...)` is the
usable lifecycle consumer for any wrapper, not just Hermes.

1. `acquire(owner, request)`: request declares task, opaque auth boundary,
   allowed origins/actions, and mode. The lease identity hashes the owner,
   adapter ID/version, canonical scope, auth boundary, mode, and task identity.
2. `adapter.capabilities()` returns a bounded `browser_adapter_capabilities/v1`
   snapshot. Capability discovery is local/bounded host work, at most once per
   lease. The manager adds observation/expiry times and an adapter-version plus
   lease cache identity. Expiry terminates that lease rather than silently
   refreshing or restarting it. A new explicit task/version may acquire anew.
3. `adapter.start(lease_id, scope, deadline)` receives metadata-only identity
   scope, including allowed origins/actions, mode, and auth-boundary digest.
   The host resolves auth material itself and returns its owned tab IDs.
4. `adapter.observe(lease_id, tab_id, deadline)` returns transient URL, host
   revision, explicit readback boolean, and bounded semantic elements
   `{role, name, key}`. Names and keys are hashed immediately; only role and
   opaque digests/handles survive. DOM and unrelated fields are never projected.
5. `operate(owner, request)` validates owner, adapter, live lease, tab, exact
   OMH revision and unique handle/semantic match before reserving the action.
   For `act`, a fresh observation catches intervening state changes before
   calling `adapter.act(lease_id, tab_id, host_revision, key_digest, "read",
   deadline)`. The host must atomically recheck revision before interaction.
   There is no bare index, coordinate fallback, or first-match repair.
6. `release(owner, lease_id)` calls the host reap and records a terminal only
   after `{reaped: true}`. `cleanup(owner)` and the registered session-end hook
   reap that owner's bounded retained leases for the manager's adapter ID and
   version, including unknown starts. Foreign adapters/versions are skipped,
   not invoked and not allowed to abort the sweep. Their original managers
   remain responsible for cleanup; foreign actions/releases are still refused.

Every successful read has post-action observed URL/state readback. URL metadata
contains canonical `observed_origin` plus `observed_url_digest`, not paths,
queries, fragments, credentials, or a fabricated full URL. Missing readback is
`unknown`, never successful navigation/read evidence. State revisions increase
on host revision or semantic/URL change; one count-only delta replaces the last
delta. The acquisition result remains immutable; callers must use an explicit
fresh observation when its original handles become stale.

## Execution and effect boundary

The default Hermes hook bridge declares `mutation_interception: none`.
**All native `browser_*` tool calls are blocked while this explicit bridge is
enabled**, because they cannot attest to these lease/revision checks. The bound
tool defaults to an adapter's inert semantic `read`, not arbitrary page script.
Explicit `effects_enabled` host setup adds `effect_preview`, `effect_execute`,
and `effect_abort` to this same admitted tool; see [BROWSER-EFFECTS.md](BROWSER-EFFECTS.md)
for trusted approval/event binding and supported operation classes. Raw navigation,
click, type, press, download, console/evaluate and opaque actions stay blocked.
Upload requires the opted-in held-byte effect path. The host may initialize
an explicitly scoped session, but cannot infer permission for arbitrary effects.
This is not an allowance based on a GET method, tool name, button label, or
same origin; none of those proves side-effect-free behavior.

For #1392, the concrete extension is `mutation_interception: none|last_mile`,
`preview(lease_id, handle, operation)`,
`resume(lease_id, preview_ref, attempt_id)`, and `abort(lease_id, preview_ref)`.
`pending_effect()` parses the closed `browser_pending_effect/v1` metadata:
held flag, method, origin, payload byte count/digest/closed shape, target
role/name digest, new-tab/redirect flags, and opaque preview digest. It does
**not** classify GET as safe or authorize dispatch. A last-mile host must hold
all effects before egress, bind a preview to exact lease/tab/revision/target,
resume exactly once against a durable authorized attempt, and abort/reap on
refusal. Preview cannot speculatively execute a click or script to learn its
effects. The effect consumer supplies exact-intent approval, one durable generic
attempt, and a linked receipt only after bounded observed readback. Inert and
effect operations share the manager's existing action budget and OS lock;
preview, unapproved execution and replay do not reserve a fresh action.

## Hard limits and recovery

OMH ceilings (the host may lower them): four concurrent leases, four tabs per
lease, 64 observations/actions, 300-second lease TTL, 60-second capability TTL,
32 semantic elements, 4096 JSON bytes per page state, 2048 capability bytes,
32 retained task identities, and one 1 MiB store. The store uses repository OS
file locks and private paths under `runtime/browser/leases.json`; it does not
scan repository files or historical journals. It rejects malformed nested state
and unsafe managed symlinks/hardlinks rather than treating them as an empty store.

Named terminal reasons include `released`, `orphaned`, `expired`,
`capability_expired`, `action_capped`, `tab_capped`, `state_capped`,
`deadline_exceeded`, and `capability_invalid`. New acquisitions can be refused
as `concurrency_capped` or `metadata_capped`. Tombstones are never automatically
evicted, since forgetting one could restart an unknown acquisition after a crash.
At retention exhaustion the operator must choose a new explicit task namespace/
OMH root only after host resources are reaped; automatic reset is prohibited.

Reservations are written before host callbacks. A crash/exception leaves
`unknown`; repeating acquisition never auto-starts it. Concurrent in-flight
acquisition returns `pending` to its manager or `unknown` to another manager.
Once completed, identical acquisition returns identical bytes without repeated
start, observe, capability, or metadata writes. Cleanup is OS-lock serialized,
idempotent after success, and recoverable after a failed reap; unknown cleanup
is never relabeled released. Host exceptions propagate without raw host content
being persisted.

The host **must** enforce deadlines autonomously, bound capability discovery,
serialize start/reap by lease ID, revoke IDs on reap, close every session/tab
after partial starts, and prevent a concurrent start from reviving a revoked
lease. OMH cannot cancel an arbitrary synchronous Python callback without
becoming a process supervisor; it checks elapsed deadlines before further
callbacks and refuses late results. A nonconforming injected adapter cannot
claim these host-resource guarantees. Auth profiles, cookies, storage, raw DOM,
form values, headers, bodies, and screenshots stay host-owned, not lease metadata.

## Verification and evidence

Focused suites: `PYTHONPATH=tests uv run python -m unittest
tests/test_browser_adapter.py tests/test_browser_adapter_boundaries.py
tests/test_browser_admission.py -v`.
The issue artifact `qa_browser_adapter.py` exercises actual Hermes
`PluginContext`, registered tool handlers and `PluginManager.invoke_hook`, with
the already-installed local Playwright/Chromium. It isolates both homes and
closes contexts, browser, Node, HTTP server, and temporary roots. The screenshot
is an explicitly captured synthetic fixture artifact, never ordinary retained
page content. This is local host-surface evidence, not a model/provider run or
evidence that arbitrary live sites are safe.

QA compares actual `registry.get_definitions` with public
`model_tools.get_tool_definitions` through inactive, admitted, foreign/unrelated,
completed and expired requests. It preserves the acquire/read/reuse/stale/reap
Chromium exercise. Output defaults to an invocation-owned temporary directory;
operators can retain evidence with `--output-dir <isolated-artifact-directory>`.

`uv run python tools/benchmarks/browser_adapter.py --repetitions 20` runs bounded
repeated/stale/concurrency/crash/cleanup controls without private checkout
artifacts or browser startup; real-host timing is `not_run` by default.
An operator can pass `--host-qa` with an explicit local driver implementing the
QA protocol above, plus `--output-dir` to retain its evidence. That arm measures
host callback wall time separately from OMH validation/store/serialization
overhead. It makes no percentage, quality, or provider-performance claim.
