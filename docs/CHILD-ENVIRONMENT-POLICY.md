# Fanout child-environment policy

Fanout child processes use `child_environment_policy/v1`. The default is least privilege: a child receives only portable process variables, its selected owner's state location, dispatch lineage, and capability names explicitly declared for that child. It never inherits the ambient parent environment wholesale.

## CLI declarations

`omh coding fanout dispatch` and `omh coding run` accept name-only declarations:

```sh
omh coding fanout dispatch --owner-env codex:ISSUE_TRACKER_TOKEN \
  --project-env CI --verification-env TEST_DATABASE_URL --run-verification
```

- `--owner-env OWNER:NAME` grants an existing parent value only to that owner's work process. Repeat it for multiple grants.
- `--project-env NAME` grants an existing parent value to owner and verification environments.
- `--verification-env NAME` grants an existing parent value to verification environments only.
- `--deny-env NAME` removes a parent-only variable from every child; an unrelated denied parent name remains ready, while denying an explicitly requested grant fails before spawn.
- `--allow-broad-environment` is an explicit temporary compatibility mode. It restores parent inheritance and labels every policy receipt `compatibility_explicit`; remove it after migrating declarations.

Arguments accept variable *names*, not values. Missing required names fail the affected unit before spawn. A verification environment does not receive owner-only grants. Verification-command environment overrides are also checked: sensitive names require an explicit verification grant. Existing file, keychain, socket, and native-agent authentication flows remain untouched because the policy neither reads nor requires credentials as environment values.

## Programmatic API

Pass the same shape to `dispatch_fanout(..., environment_policy=...)`:

```python
{
    "owner_capabilities": {"codex": ["ISSUE_TRACKER_TOKEN"]},
    "project_variables": ["CI"],
    "verification_capabilities": ["TEST_DATABASE_URL"],
    "allow_broad_inheritance": False,
}
```

`resolve_child_environment()` returns the filtered `environment` and a receipt. The receipt records schema, owner, purpose, status, bounded names passed/removed/approved/denied/missing, counts and truncation markers, classification/reason/provenance, and a digest of the full effective name-only decision. It contains no environment values. The journal retains only this validated, bounded name-only receipt for the owner process; integration and unit verification receipts remain in the dispatch summary. Operator note: nonportable parent names are omitted from journalable previews, while their full name-only decision remains reflected in counts, truncation markers, and the digest.

This is a process-environment boundary, not proof that a tool used a capability or that the capability was protected after it reached that process. Child subprocesses can still pass their received variables onward.
