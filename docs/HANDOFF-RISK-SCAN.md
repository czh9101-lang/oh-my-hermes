# Advisory handoff risk scan

## Agent and operator reference

People can ask Hermes to review a final unattended coding handoff for local
risk signals. Agents and wrappers explicitly invoke this control-plane command
**after composing the final brief and before unattended handoff**. It applies
equally to Codex, Claude Code, Hermes coding and generic selected executors.
It does not run automatically or register a host hook.

```sh
omh handoff-risk-scan --brief-file final-brief.txt --repo . --json
omh handoff-risk-scan --brief-file final-brief.txt --repo . --strict --json
printf '%s\n' 'Run git reset --hard' | omh handoff-risk-scan --brief-stdin --json
omh handoff-risk-scan --repo . --protected-branch production --json
```

At least one brief or repository is required. File and stdin are mutually
exclusive. Briefs must be UTF-8, at most 65536 bytes, read once with a bound.
Repeated `--protected-branch` flags replace the exact defaults `main`, `master`;
there is no glob inference. At most 32 names of at most 200 ASCII characters
are accepted. Missing/unreadable input, malformed command quoting, invalid
repository metadata and overflow are scan errors, never clear reports.

| Result | Default exit | Strict exit | Next step |
| --- | ---: | ---: | --- |
| `completed / clear` | 0 | 0 | Continue the existing approval process; not permission |
| `completed / advisory` | 0 | 0 | Review workspace changes |
| `completed / high_risk` | 0 | 1 | Pause automation for existing confirmation or `security-safety-review` |
| `scan_error / null` | 2 | 2 | Repair the declared input and scan again |

A completed high-risk advisory is not an execution failure. The strict exit
allows an automation to pause; neither mode overrides approval policy. A
changed brief/workspace needs a new explicit scan. There is no durable scanner
state and no automatic rescan.

## Machine contract

`--json` emits `schema_version: handoff_risk_scan/v1`, `status`, `verdict`,
`findings`, `summary`, `input_summary`, `error_category`, and `claim_boundary`.
Each finding has a stable `id`, `severity` (`low`, `medium`, `high`),
`confidence` (`medium`, `high`), `evidence`, and `advice_code`. First-version
signals are high-confidence classifications; no probability of harm is claimed.
There is at most one finding per ID. Summary counts are finding counts, not
command or vulnerability counts. Brief evidence uses a fixed token class,
SHA-256 and UTF-8 line-start offset; repository evidence uses a path-set digest
and class, never a filename. The input summary contains only brief byte length
and digest plus repository tracked/dirty/untracked counts.

| Finding ID | Severity | Meaning |
| --- | --- | --- |
| `destructive_git` | high | Hard reset, forced clean, forced branch deletion |
| `destructive_filesystem` | high | Command-shaped removal or shredding |
| `destructive_database` | high | DROP/TRUNCATE table/database/schema or DELETE FROM without WHERE |
| `test_weakening` | high | Instructions to delete/disable/bypass/skip tests or pytest exclusions |
| `protected_branch_write` | high | Explicit push destination or observed-current-branch writes |
| `dirty_worktree` | medium | Index/tree difference, missing tracked path, or changed file stat |
| `untracked_files` | medium | Git-reported untracked path metadata |
| `tracked_secret_path` | high | Tracked secret-looking path, not credential contents |

`confirm_or_security_review` means obtain explicit confirmation or prepare the
existing security review before proceeding. `review_workspace_changes` means
review the declared workspace changes without discarding them automatically.
A secret-path signal is confidence about the **path only**, never a claim that
credentials exist. Exact case-insensitive basename suffixes `.example`,
`.sample`, `.template`, `.dist` are excluded; an `examples/` directory is not.

## Precision and limits

The scanner lexes command segments with stdlib `shlex`, preserving quoted
arguments and separating shell chains. An imperative followed by inline
backticks is actionable. Leading scoped negation is excluded; negating one
clause does not negate a later chained command. Markdown block quotes and
explicit `Example:`, `For example`, `Explanation:` contexts are illustrative.
A label ending in `:` can introduce an illustrative/negated fenced block;
closing its fence ends that context. Unlabeled code fences are actionable.
Literal commands in explanation and echo/printf arguments are not executable
intent. Unicode lookalikes are not normalized into runnable ASCII commands.

This is a small deterministic vocabulary, **not a shell/SQL interpreter or
complete semantic security analyzer**. Arbitrary shell functions, aliases,
script bodies, encoded payloads, command substitutions, configuration-driven
push destinations and translated instructions are not fully analyzed. A clear
report says only that these declared rules found no signal. Detached HEAD has
no inferred current branch; nested repositories/submodules are not traversed.

Repository collection is bounded to 10000 entries and 1 MiB of retained
metadata, with a five-second timeout per Git subprocess. It uses `rev-parse`,
`symbolic-ref`, index `ls-files --stage --debug -z`, untracked `ls-files`, and
`ls-tree -r -z` (tree metadata, never blob contents). It compares index/tree
entries and `lstat` values; a stat-only change can therefore be advisory even
when file bytes are unchanged. It intentionally does **not** use `git status`
or worktree diff: their index refresh can read secret contents and execute
clean filters. Untracked discovery follows Git ignore metadata. User/system
Git configuration, inherited Git environment, hooks, optional locks and
fsmonitor are disabled. Git stderr is discarded, not persisted as evidence.

The scanner never opens secret file contents, hashes working-file bodies,
invokes a model, accesses the network, mutates the index/refs/checkout,
dispatches executors, or stores prompts or reports. Only an explicitly supplied
brief file/stream is read as text. The caller may redirect metadata-only stdout.

A clear report is not proof of safety, permission, execution, verification,
review, CI, or merge readiness. Existing metadata preflight remains deliberately
pre-expansion and unchanged; permission, approval and host sandbox policy keep
their existing authority.
