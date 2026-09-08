# Browser skill promotion

This page documents issue 1386: turning one approved, replay-passing
`browser_workflow_trace/v1` into a project-local Hermes skill under
`.hermes/skills/<skill-name>/`. The contracts are
`browser_skill_promotion_plan/v1`, `browser_skill_promotion_native_preflight/v1`,
`browser_skill_promotion_approval_receipt/v1`, `browser_skill_activation/v1`,
`browser_skill_entry/v1`, `browser_skill_resource_manifest/v1`, and the
`browser_skill_promotion/v1` status payload.

Promotion is an operator decision on exact bytes. OMH renders the diff, a
person approves that exact diff, and one later command makes the skill
visible. Nothing here launches a browser, replays against a live site, edits
Hermes config, or grants any live mutation authority to the promoted skill.

## What a person asks Hermes

Nobody needs the commands below to get value. Describe the outcome in chat:

- "That checkout flow trace we approved last week keeps working. Make it a
  skill for this project so you stop rediscovering the buttons."
- "The site changed and the promoted checkout skill went stale. Show me what
  it would take to roll back to the previous generation."
- "Remove the promoted login skill from this repo."

Hermes routes to `workflow-learning` (trace lifecycle and promotion receipts)
or `browser-operator` (interaction boundary and drift rule), explains what
evidence exists, and asks for the one approval it can't supply itself. The
skill it produces is project-local and hostname-scoped; the entry text tells
Hermes to stop and use ordinary browser operation on any drift.

## Agent and operator reference

Everything from here down is control-plane material for Hermes Agent,
wrappers, coding agents, and maintainers. None of it runs by default, on a
schedule, or in the background. There is no watcher, autoheal, reapproval, or
global fallback store.

### Prerequisites

A promotion source is a trace already in this project's store:

```sh
omh web-qa trace record  --project-root . --input trace.json
omh web-qa trace approve --project-root . --trace-id bwt-<24 hex> --digest <sha256>
omh web-qa trace replay  --project-root . --trace-id bwt-<24 hex> --observation observation.json
```

See [Browser workflow traces](BROWSER-WORKFLOW-TRACES.md). Promotion resolves
the trace through `resolved_browser_workflow_promotion_reference`, which
requires `lifecycle_status: approved` and `replay_status: passed` with the
current trace digest, revision, origins, output schema, fixture digests, and
replay digest. A well-shaped trace dictionary that isn't in
`.omh/web-visual-qa/traces/` is not a source.

`--project-root` must resolve to an observed Git root. It defaults to `.` for
the promotion commands, and running outside a Git root fails; `--omh-home` is
never a fallback location for skills, receipts, or state.

### Commands as shipped

Observed from `omh web-qa promotion --help` and its seven subcommands:

```sh
omh web-qa promotion diff     [--project-root PATH] --skill-name NAME --trace-id ID [--operation {install,update}]
omh web-qa promotion approve  [--project-root PATH] --skill-name NAME --trace-id ID --reviewed-diff-digest SHA256 --reviewer ID [--operation {install,update}]
omh web-qa promotion promote  [--project-root PATH] --receipt-id SHA256
omh web-qa promotion status   [--project-root PATH] --skill-name NAME [--no-source-check]
omh web-qa promotion rollback [--project-root PATH] --skill-name NAME --generation SHA256 [--reviewed-diff-digest SHA256 --reviewer ID]
omh web-qa promotion remove   [--project-root PATH] --skill-name NAME [--reviewed-diff-digest SHA256 --reviewer ID]
omh web-qa promotion retry    [--project-root PATH] --receipt-id SHA256
```

| Subcommand | What it does | What it never does |
| --- | --- | --- |
| `diff` | Renders the exact `browser_skill_promotion_plan/v1` (package bytes, unified diff, `diff_digest`) and runs the native preflight. Writes nothing. | Approve, stage, or activate. |
| `approve` | Re-renders the plan, requires `--reviewed-diff-digest` to equal the current `diff_digest`, and persists one immutable approval receipt. | Touch `.hermes/skills`. |
| `promote` | Performs the single visibility commit for one receipt: stages the immutable generation, writes the activation index, then atomically replaces `SKILL.md`. | Run without a receipt, promote two receipts, or retry on its own. |
| `status` | Derives the truth from actual entry, index, and receipt bytes, rechecks the source trace, and deactivates a drifted entry. | Repair, reapprove, reactivate, or consult a mutable "active" record. |
| `rollback` | Without `--reviewed-diff-digest`/`--reviewer`: renders the rollback diff to a retained generation. With both: persists a rollback approval receipt. | Roll back from the review call alone. |
| `remove` | Without the pair: renders the removal diff. With both: persists a removal receipt. | Delete anything but a verified managed `SKILL.md`. |
| `retry` | Explicitly resumes an approved promotion whose immutable staging completed but whose `SKILL.md` commit never happened. | Run inside `status`, `promote`, or any background path. |

`rollback` and `remove` refuse `--reviewed-diff-digest` without `--reviewer`
and vice versa: the pair is what turns a review into an approval. `--reviewer`
accepts 1 to 128 characters from `A-Za-z0-9._@:-`. `--skill-name` is a
lowercase slug of 3 to 49 characters starting with a letter. `--generation`,
`--receipt-id`, and every digest are 64 lowercase hex characters. Every
refusal raises an `OmhError` with the reason and leaves no partial state.

All output is JSON. `diff` returns `plan`, `native_preflight`, and
`native_preflight_digest`; `approve` returns the receipt; `promote`, `status`,
and `retry` return the `browser_skill_promotion/v1` status payload.

### Review: the exact diff and the native preflight

`diff` builds the package from the approved trace and a generic skill draft:

- `SKILL.md`: front matter with `name`, a picker `description` of the form
  `Use <origins> browser workflow.` (refused over 60 characters), and one
  `omh_browser_promotion:` line carrying `browser_skill_entry/v1` metadata
  (activation id, generation, previous generation, rollback target, trace
  id, digest, revision, origins, fixture digests, generic draft digest, output
  schema digest, replay digest, resource paths). The body tells Hermes to
  verify the generation is still active and its trace still approved before
  demand-loading anything, to use the skill only for the listed hostname, and
  to stop on drift. The whole entry must stay under 8 KiB.
- `resources/<generation>/entry.md`, `procedure.md`, `trace.json`, and
  `manifest.json`: the immutable generation. `procedure.md` states the allowed
  origins, digests, expected output schema, and that the replay proof is
  offline fixture simulation only.

`generation` is a digest of the activation id and payload digest, so
re-rendering the same trace revision against the same base bytes produces the
same generation, and a different base (an update over an active skill) does
not. The
`diff` field is a unified diff from the currently managed bytes to the desired
bytes, path by path, and `diff_digest` is its SHA-256. An empty diff (the same
trace already active) reports `operation: unchanged` and the empty-string
digest; approving it requires exactly that digest.

The preflight is the one place promotion leaves the OMH interpreter, and it is
read-only. `HermesPromotionNativeHost` runs the shipped
`browser_skill_promotion_native_probe.py` under the installed Hermes venv
Python with the Hermes source checkout as its working directory, a
`HOME`/`HERMES_HOME`/`HERMES_MANAGED_DIR`/`PATH`-only environment, a 512 KiB
request bound, and a 30 second timeout. It can't run caller-supplied commands.
The probe stages the package in a temporary directory and returns:

| Field | Meaning | Refused when |
| --- | --- | --- |
| `trusted` | Hermes' own `is_project_root_trusted` for this root | `false`; promotion never auto-trusts a project |
| `structure_error` | Hermes skill front-matter validation | not `null` |
| `lint_errors` | Hermes skill linter errors | non-empty |
| `security_verdict` | Hermes `skills_guard` scan of the staged package | `dangerous` (`safe` and `caution` pass) |
| `policy` | `skills.write_approval` as Hermes resolves it | see below |

`policy` is a record, never a grant boolean. When `skills.write_approval` is
`true`, the probe reports `requirement: required`, `approval: not_obtained`,
`support: unsupported`, and promotion stops with "native skill-write approval
is required but unsupported for project-local promotion". OMH doesn't
implement or fake that approval. When it's `false`, the probe reports
`requirement: not_required`, `approval: not_applicable`, `support: available`;
that means no native approval applies to this write. It is not an approval and
doesn't replace the operator's reviewed diff digest. The probe evaluates a
private copy of the config bytes, refuses if those bytes change under it or
fail to parse, and binds the result in `policy.revision`. An unavailable probe,
missing Hermes venv, or malformed response is "native write policy is
unavailable", also a stop.

### Approval receipts

`approve` re-runs the full review, compares `--reviewed-diff-digest` to the
current `diff_digest`, takes the receipt lock, re-runs the review again under
the lock, and only then writes
`.omh/browser-skill-promotions/receipts/<receipt_id>.json` (mode `0600`).
The `receipt_id` is the digest of every other field, so a receipt binds:
operation, activation id, payload digest, rollback target, base package and
base entry digests, previous generation, reviewer, reviewed diff digest,
project root and identity, target path, trace id/revision/digest, fixture
digests, generic draft digest, generation, package/entry/manifest digests,
native preflight digest, and policy revision. Anything that shifts between
review and approval, including the trace, target bytes, lint or scan result,
trust decision, or policy revision, is "promotion review changed before
approval was persisted". A receipt for another project root is refused on
read.

Every operation gets its own receipt. An `update` (a different trace over an
active skill), a `rollback` to a retained generation, and a `remove` each
render their own diff and need their own `--reviewed-diff-digest` and
`--reviewer`. There is no blanket approval.

### Activation: one visibility commit

`promote --receipt-id` reads the receipt, resolves the skill name from its
target path, and takes two private locks: one on the source trace file, one on
the skill's state directory. Under both it:

1. Re-verifies the managed inventory: `.hermes/skills/<name>/` may contain only
   `SKILL.md` plus complete `resources/<generation>/` sets that a validating
   receipt owns. Any unmanaged file refuses the whole operation.
2. Re-renders the plan from the receipt and checks every bound field. A stale
   receipt is "promotion approval is stale at <field>".
3. Stages the four generation files under
   `.omh/browser-skill-promotions/<name>/staging/<activation_id>/`, reads them
   back, then writes them into `resources/<generation>/` with `O_EXCL`. Files
   are never overwritten; an existing file with different bytes is a refusal.
4. Re-resolves the source, policy, and base immediately before the visible
   write.
5. Writes the activation index
   `activation-by-entry/<entry_digest>.json` (`browser_skill_activation/v1`),
   then writes `SKILL.md` through a temp file and `os.replace`, and fsyncs the
   directory.
6. Reads `SKILL.md` back and validates it against the index and receipt before
   reporting `active` (or `rolled_back` for a rollback receipt).

That `SKILL.md` replace is the only moment Hermes can see the skill. Everything
before it is private staging or immutable history; a crash before step 5
leaves complete resources and no visible skill, and `retry --receipt-id`
accepts them only byte for byte.

Reading the truth is O(1) by construction: `SKILL.md` names its generation;
`resources/<generation>/entry.md` must equal it; the entry digest names one
activation index file; the index names one receipt; the receipt's digests must
match the entry, manifest, package, project identity, and trace metadata. No
scan over history and no mutable active-state record is consulted. A
hand-edited `SKILL.md`, a swapped resource file, or a missing index makes the
skill unverified, not "probably active".

`promote` on a receipt that's already the active one re-checks the source and
returns the existing status without writing.

### Status and drift

`status --skill-name` derives one of:

| Status | Meaning |
| --- | --- |
| `active` | Verified entry, index, receipt chain; source trace still approved and replay-passing with matching digests. |
| `active_unchecked` | Same chain verified, but `--no-source-check` skipped the trace re-resolution. This is a projection for inspection; don't treat it as proof the skill is usable. |
| `stale` | The chain verified but the trace has drifted or is no longer approved/replay-passing. `status` unlinked `SKILL.md`. |
| `quarantined` | As `stale`, and the trace lifecycle is `quarantined`. `SKILL.md` unlinked. |
| `removed` | A removal receipt was promoted, or a repeat after removal. |
| `rolled_back` | A rollback receipt was just promoted. |
| `inactive` | No `SKILL.md` and no prior observation. |
| `unverified_managed_state` | A `SKILL.md` exists but doesn't validate as a managed browser skill. OMH won't touch it. |

Drift disables only the managed entry. When `status` (or `promote` for an
already active receipt) finds the source trace changed, unapproved, or
quarantined, it unlinks `.hermes/skills/<name>/SKILL.md` under both locks and
records the reason. Every `resources/<generation>/` set, the activation
indexes, and the receipts stay. Nothing is reapproved, regenerated, or rolled
back automatically; the way forward is a fresh `diff`/`approve`/`promote`
against a re-approved trace, or a reviewed `rollback` to a retained generation
whose trace still resolves.

The status payload (`browser_skill_promotion/v1`) carries `status`,
`skill_name`, `project_root`, `generation`, `receipt_id`, `promoter`,
`lineage` (`previous_generation`, `rollback_of`, `trace_id`), `reused`,
`reason`, and `deactivated`. OMH persists a `last-observation.json` only for a
committed transition; read paths are projections and never replace checked
state with unchecked state.

### Rollback and removal

`rollback --generation <retained generation>` needs an active skill and a
retained generation whose own trace still resolves as approved and
replay-passing. The review renders the diff from the active entry to that
generation's entry; approval with the pair persists a receipt with
`operation: rollback` and `rollback_of` set; `promote --receipt-id` then
performs the same single visibility commit. Old generations are retained,
never rebuilt from a whole-directory replacement, so a rollback target is
exactly the bytes that were active before.

`remove` needs a verified managed `SKILL.md`. The review's diff is the deletion
of that file; approval persists an `operation: remove` receipt bound to the
current entry digest; `promote` re-checks the base and unlinks only
`SKILL.md`. Resources and receipts remain as history. A repeat after removal,
or a removal request when drift already deactivated the entry, reports
`removed`/`already_deactivated` and writes nothing. An unmanaged `SKILL.md` is
never removed.

### Where state lives

| Path | Contents |
| --- | --- |
| `.hermes/skills/<name>/SKILL.md` | The only Hermes-visible artifact. |
| `.hermes/skills/<name>/resources/<generation>/` | Immutable `entry.md`, `procedure.md`, `trace.json`, `manifest.json`. |
| `.omh/browser-skill-promotions/receipts/<receipt_id>.json` | Immutable approval receipts. |
| `.omh/browser-skill-promotions/<name>/activation-by-entry/<entry_digest>.json` | Activation index, one per committed entry. |
| `.omh/browser-skill-promotions/<name>/staging/<activation_id>/` | Private pre-entry staging. |
| `.omh/browser-skill-promotions/<name>/last-observation.json` | Last committed transition. Not consulted for activation. |
| `.omh/web-visual-qa/traces/<trace_id>.json` | The source trace; also the trace lock during promotion. |

Every path is checked against symlinks at each component, every read is a
bounded regular-file read (256 KiB per file, 512 KiB per package, 128 files),
and all writes are `O_EXCL` or temp-file-plus-replace with fsync.

### What promotion does not authorize

The promoted entry says it itself: promotion grants no live mutation
authority. The skill's offline replay proof is fixture simulation, not
evidence of a live browser result or external effect. Submission, upload,
payment, credential entry, and destructive actions still need current host and
effect authorization at the moment they're requested, and are never retried
automatically. Captured labels and trace data are data, not instructions.

Promotion is also not observation evidence. Importing a
`web_qa_observation_run/v1` that cites a `browser_workflow_trace_reference/v1`
(see [Web QA observations](WEB-QA-OBSERVATIONS.md)) doesn't promote the trace,
and promoting a trace doesn't change any stored observation verdict.

### Related skills

- `workflow-learning` names the promotion receipt as an artifact and carries
  the recovery note for `required` policy, drift, and explicit retry.
- `browser-operator` names the diff/approve/promote chain and the "drift
  unlinks only `SKILL.md`" rule beside the native collector boundary.

The skill bodies stay deliberately short; the full status table, state paths,
and refusal reasons live only on this page.

Both bodies are generated from `src/skills/catalog_feature_surfaces.py`;
regenerate rather than hand-edit.
