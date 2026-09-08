# Browser workflow traces

`browser_workflow_trace/v1` is an operator-facing, offline evidence contract. It does not launch a browser, execute a submission, call a model, or promote a skill.

A trace is admitted only from an explicitly selected source with a closed success proof and source, environment, and adapter lineage digests. The storage adapter verifies a real Git control directory (a bare `.git` folder is rejected), injects the root identity and adapter/origin binding, and never persists a root path. The pure parser requires closed semantic fixture data, not caller-supplied match counts or schema booleans.

## Operator commands

These are agent/operator control-plane commands, not normal user setup commands.

```sh
omh web-qa trace record --project-root . --input trace.json
omh web-qa trace inspect --project-root . --trace-id bwt-0123456789abcdef01234567
omh web-qa trace approve --project-root . --trace-id bwt-0123456789abcdef01234567 --digest <sha256>
omh web-qa trace replay --project-root . --trace-id bwt-0123456789abcdef01234567 --observation observation.json
omh web-qa trace status --project-root . --trace-id bwt-0123456789abcdef01234567
```

Traces live only at `.omh/web-visual-qa/traces/` below the observed Git root. Running outside a Git root fails closed; `--omh-home` never becomes a fallback store.

## Public trace shape

```json
{
  "schema_version": "browser_workflow_trace/v1",
  "project": {"identity": "<verified project sha256>"},
  "origins": ["https://example.test"],
  "adapter_version": "adapter/v1",
  "parser_version": "parser/v1",
  "source": {"run_ref": "run-42", "evidence_ref": "evidence-42", "selected": true, "success": {"state": "success", "evidence_digest": "<sha256>"}, "binding": {"project_identity": "<sha256>", "origin": "https://example.test", "adapter_version": "adapter/v1"}, "lineage": {"source_digest": "<sha256>", "environment_digest": "<sha256>", "adapter_digest": "<sha256>"}},
  "steps": [{"action": "click", "locators": [{"kind": "role", "role": "button", "name": "Continue"}]}],
  "output_schema": {"kind": "object", "fields": ["confirmation"]},
  "fixtures": [{"fixture_id": "negative", "kind": "negative", "origin": "https://example.test", "nodes": [], "output_fields": ["confirmation"], "digest": "<sha256>"}, {"fixture_id": "positive", "kind": "positive", "origin": "https://example.test", "nodes": [{"role": "button", "name": "Continue"}], "output_fields": ["confirmation"], "digest": "<sha256>"}]
}
```

The parser canonicalizes case and IDN hostnames, strips URL query and fragment data, and retains no free-form browser metadata: cookies, storage, credentials, headers, form values, screenshots, DOM, response bodies, transcripts, and unknown metadata are discarded before persistence. It rejects traces or fixtures over 256 KiB, traces over 64 steps, and steps with over eight locator candidates.

Replay accepts exactly one stored `fixture_id` and resolves its bounded semantic nodes against each locator candidate. It verifies the fixture digest and output-field schema before simulating the result; it never reports browser execution. Zero and ambiguous matches stop without fallback. The first mismatch is `stale`; a distinct later stored mismatch, host mismatch, or mutating ambiguity is `quarantined`. A `transient` fixture may consume one retry only after a fresh semantic locator resolution and only when every action is `read` or `navigate`; submission and every mutating action are never retried. There is no auto-heal, reapproval, reactivation, browser action, or loop.

A web visual-QA package may consume only a resolved `browser_workflow_trace_reference/v1` minted by `resolved_browser_workflow_trace_reference`; arbitrary interaction dictionaries are rejected at construction, and package persistence resolves each reference again against the project-local store. The reference names the trace ID, digest, project identity digest, origin allowlist, and approved lifecycle state, so dependent QA and promotion work can resolve it through the project-local store.
