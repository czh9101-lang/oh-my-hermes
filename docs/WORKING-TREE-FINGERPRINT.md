# Working-tree fingerprint

`working_tree_content_fingerprint/v1` is the local freshness authority for `omh goal checkpoint` and `omh quality-evidence assess`. It identifies final Git-visible working-tree content, not index staging state or only `HEAD^{tree}`.

## Contract

- The identity is a 64-character SHA-256 digest over the canonical final Git tree. It is independent of the current `HEAD` and of whether final bytes are represented by the committed tree, index, or working-tree overlay.
- Clean state is accepted only after an isolated porcelain status probe proves it clean. Dirty state streams changed regular-file bytes in 1 MiB chunks and hashes symlink targets without buffering file content.
- Staging and unstaging do not alter the result when final bytes, mode, type, and path are unchanged. Reverting restores the prior identity.
- The real index is copied to a temporary index. Git reads use a temporary object directory with the repository object directory as a read-only alternate. The collector never writes the checkout, refs, or real object store.
- Every Git argv disables `core.fsmonitor`, forces `core.filemode=true`, disables optional locks, strips ambient `GIT_*` redirection, and uses no diff or textconv command. The mode override prevents a repository's `core.fileMode=false` from hiding an executable-bit change. Enabled `core.autocrlf` and configured `filter`, `text`, `eol`, `ident`, legacy `crlf`, or `working-tree-encoding` transformations fail closed: Git-normalized blobs cannot prove the exact bytes a local executor reads. Attributes are checked before status refresh, without reading clean tracked content. Sparse checkout, split index, assume-unchanged/skip-worktree entries, gitlinks, nested repositories, non-SHA-1 Git object format, observed races, unreadable paths, timeouts, and unavailable repositories also produce explicit non-authoritative states.
- No source bytes, absolute paths, or ignored path names are persisted in evidence. Only the digest is passed to evidence and checkpoint consumers.

`HEAD^{tree}` remains available only through the legacy compatibility helper. Fresh local CLI paths use the complete fingerprint and fail source freshness closed when collection is not authoritative.

## Cost and ratchets

The collector makes 9 Git calls on clean fixtures and 10 on dirty fixtures. `tools/benchmarks/working_tree_fingerprint.py` creates temporary 1k, 10k, and 100k tracked-file repositories, measures one clean and one dirty collection for each, and enforces `benchmarks/working-tree-fingerprint/ratchets.json`.

Run it with:

```sh
PYTHONPATH=tests uv run python tools/benchmarks/working_tree_fingerprint.py
```

The checked-in limits are twice the 2026-09-07 Apple M4 Pro observations, rounded with at least 10 ms slack. They bound both calls and wall time; they are not an unlimited performance allowance.
