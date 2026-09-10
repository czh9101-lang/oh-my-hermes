# Documentation claim audits

## Maintainer and agent surface

Ask Hermes to check selected public claims against current implementation.
The docs-specialist lane can consume the following operator command; normal
users do not need to memorize it:

```sh
uv run python -m omh.cli docs claims --check
uv run python -m omh.cli docs claims --check --json
uv run python -m omh.cli docs claims --claim release.checklist-prepared --check --json
```

The reviewed catalog is `src/catalogs/documentation_claims.py`. Each entry has
a stable ID, question, reviewed invariant and expected machine fact, affected
pages, source-path/symbol anchors, mode, probe identifier, risk, and repair
owner. Enrollment is prepared coverage configuration, not observed evidence.
This is a selected inventory, not proof that every document is accurate,
complete, readable, or usable. Rewording a page does not require sentence pins.

## Enrolled contracts

| ID | Implementation fact | Evidence |
| --- | --- | --- |
| `release.checklist-prepared` | `release checklist --json` returns `observed=false` | CLI probe |
| `release.checklist-symbol` | `release_readiness_checklist` is importable and callable | Symbol check |
| `reporting.rate-schema` | A reported rate uses `omh_reported_rate/v1` | Schema assertion |
| `reporting.empty-rate` | Empty rates have `percent=null`, `basis=no_observations` | Fixture behavior |
| `generated.roles-equality` | Shipped role-reference content equals the canonical renderer after CRLF-to-LF translation | Generated equality, not semantic support |
| `docs.evidence-language` | This guide separates prepared work from observed checks | Optional advisory model evaluation |

The evaluator uses the actual public CLI parser/handler for the checklist,
parses only its `observed` field, and exercises production functions for the
schema and empty-rate fixtures. It does not search tests or pin prose sentences.
Changing the checklist's observed flag yields `stale`; a missing symbol, failed
import, malformed result, or failed probe yields `unresolved`, never supported.

## Report and exit contract

`documentation_claim_audit/v1` orders rows by stable claim ID. `--claim` is
repeatable; unselected rows remain `not_run`. Without selection, the reviewed
deterministic set runs and the advisory row remains `not_run`. Unknown IDs are
invocation errors, not successful empty audits.

- `supported`: a completed probe returned the expected typed machine fact.
- `stale`: a completed probe returned a different fact. The row names pages,
  anchors, observed/expected facts, and repair owner. Nothing edits docs.
- `unresolved`: a selected check could not establish a valid result.
- `not_run`: not selected, model disabled/unavailable, or model run cap reached.

`--check` exits 0 when selected deterministic checks are supported and 1 when
any are stale or unresolved. Invalid invocation exits 2. Without `--check`,
the report is informational. An advisory-only selection can exit 0 while its
row is stale or not_run; read the row state, not just the process exit code.
Reports contain counts, not unsupported headline percentages.

Generated-render equality carries `evidence_class=generated_artifact_drift`.
It must not be counted as semantic support. The report's separate generated
summary covers only the selected render probe, not the full drift registry.
For role-reference equality, CRLF checkout line endings are translated to LF
after enforcing the raw input byte cap, matching the public generated check
on Windows checkouts. All remaining content must equal the renderer exactly,
including spaces and the final newline. Source and model-evidence reads are
unchanged; this does not normalize arbitrary content or grant semantic support.

Run `omh release drift --json` alongside it for the unchanged generated-file,
count, and budget checks. The audit never pretends that command ran.

`omh docs navigation --check` is the third evidence class and is deliberately
not folded into this one. It settles documentation *structure* — whether a page
is still reachable from a declared root, whether local link targets resolve, and
whether an unreachable page is classified with a reason — and never inspects
what a page asserts. A claim can be perfectly true on a page nobody can reach,
and a page can be reachable while every sentence on it has gone stale; the two
failures are repaired by different people doing different work. See
[the documentation checks table](README.md#documentation-checks) for which
command answers which question.

`release checklist` only prepares an audit command (`observed=false`).
`release product-readiness` runs the deterministic audit and includes its
observed rows; `release evidence-bundle` packages that same result. The
docs-specialist consumes those rows separately from prepared documentation
edits. Neither an audit nor an updated document proves live Hermes selection,
executor work, review, CI, merge, or release publication.

## Bounded execution

The default lane has no network or model calls and no shell commands.
Only the reviewed probe identifiers in `documentation_claims_worker.py` can
execute. Each runs once in an isolated, killable Python child with a 10-second
deadline (`--timeout` may lower it or raise it up to 30). Source/page reads cap
at 512 KiB per file; captured output and returned messages cap at 64 KiB.
Reports project only reviewed fields and typed facts, never raw stdout/stderr,
exceptions, prompts, transcripts, credentials, or arbitrary adapter fields.
Children are reaped, including on timeout. There is no automatic doc repair.

`--root` selects a **trusted** source tree containing the enrolled source files
and pages. The closed probes compile the current anchored module bytes rather
than stale bytecode; other dependencies use the running OMH installation.
This permits small copied-source regression fixtures. It is not a sandbox for
hostile Python. Use matching checkout dependencies for a whole-tree audit;
missing source/pages in an installed-wheel working directory are unresolved.

## Optional advisory adapter

Core ships no provider client, model executable discovery, or network adapter.
This command explicitly records `not_run/model_unavailable`:

```sh
uv run python -m omh.cli docs claims --claim docs.evidence-language --enable-model --check --json
```

A trusted host can call `documentation_claims_report` with explicit
`claim_ids`, `enable_model=True`, and a spawn-picklable `ModelAdapter` descriptor
from `documentation_claims_worker`. It declares `provider`, exact `model`,
`run_id`, `adapter` identity, and an `evaluate` callable before any invocation.
The callable receives `ModelRequest`: claim ID, question, reviewed invariant,
expected fact, a bounded public-page excerpt (maximum 16 KiB), and an input
digest binding those values. The host, not OMH core, owns provider access and
consent. Tests use local injected adapters, never providers.

The result must contain `state` (supported/stale/unresolved), `provider`, exact
`model`, `run_id`, `adapter` identity, matching `claim_id` and `input_digest`,
`cost_status` (reported/unknown/not_incurred), and `cost_usd` (finite nonnegative
number when reported, otherwise null). Missing provenance, mismatched binding,
unsafe identifier text, or invalid cost makes the row unresolved. Extra fields
are discarded. Requests, excerpts, and raw adapter responses are not reported.
Unknown billing stays explicitly unknown, never fabricated zero cost.

A bounded start signal records the declared identity and input digest before
calling the adapter. Failed and timed-out runs retain that provenance with
unknown cost; a source/provenance failure before the signal is an attempt,
not an observed model run. The report distinguishes `attempts` from `runs`.

An adapter runs only for explicitly selected advisory claims. The default cap
is one run, the hard cap three, and `model_run_cap=0` performs none. There are
no retries. The same enforced child-process deadline applies even if an
adapter blocks; a timeout is unresolved. Adapters must not spawn descendants
or detach work: this local seam owns only its Python worker, not remote
provider cancellation or host-launched process trees. A missing adapter is
not_run. Model results are always advisory and cannot affect `--check` or any
release gate; release readiness never enables this lane automatically.
