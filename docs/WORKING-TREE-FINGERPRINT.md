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

## Platform and Git configuration boundary

For agent/operator integrations, a supported fixture or checkout must use an
**effective** `core.autocrlf=false` and no active transforming attributes. Git
lists inherited and overridden configuration values together; the collector
uses the last value per key, matching Git precedence. An earlier system/global
`true` does not defeat a later local `false`. An effective `true` or `input`
remains unsupported; no normalization policy is overridden by collection.

Changed regular files are opened in binary mode where the platform requires
it, so CRLF, NUL, and bytes after Ctrl-Z all participate in identity. `O_NOFOLLOW`
is retained on POSIX. Windows regular files use a narrow `CreateFileW` boundary
with `FILE_FLAG_OPEN_REPARSE_POINT`, not CRT path/descriptor stat comparisons.
Metadata-only path handles share read/write/delete access; the binary data
handle shares only reads, refusing existing writers and preventing write/delete
opens while bytes are read. This matters because Windows can defer write times
until a writer closes. Before the first read, both the data handle and a fresh
named-path handle must match the initial native observation. Descriptor and
named-path observations are checked again after reading.

All Windows regular-file comparisons use `GetFileInformationByHandleEx`:
`FileIdInfo` volume plus full 128-bit identity, `FileBasicInfo` attributes,
creation/write/change times, and `FileStandardInfo` size and link count. Zero
identity, pending deletion, inconsistent types, API failures, and reparse points
in this regular-file boundary fail closed, without fallback or retries. CPython
3.11 directory-find fallback identity and 3.12 path-only creation-time/extension
mode synthesis are therefore not compared with descriptor metadata. Ordinary
symlink-target hashing and POSIX stat checks remain unchanged.

Windows support requires disk files whose filesystem/OS implements these three
information classes and supplies nonzero volume/file IDs. Filesystems lacking
`FileIdInfo`, denied sharing/access, special files, and non-symlink reparse files
are non-authoritative; no claim covers every FAT/network/cloud provider or
Windows version. This is an observation of a workspace, not an atomic snapshot
of all paths or protection against an adversary restoring all observed state.

Modes follow observable filesystem semantics: POSIX executable-bit changes
invalidate identity even with repository `core.fileMode=false`; Windows `chmod`
does not create a POSIX executable bit. Windows `.exe`, `.bat`, `.cmd`, and
`.com` suffixes do not create Git executable modes either (Git's Windows
`file_attr_to_st_mode` does not synthesize them); unchanged bytes retain their
identity through staging and commit. Native Unicode filenames work on
Windows/Darwin; undecodable filename bytes are exercised on Linux. These tests
do not claim equivalent filesystem features or native Windows verification
from a Darwin run. Git path decoding, record terminators, alternate-object
isolation, and the six transforming-attribute guards are unchanged.

## Cost and ratchets

The collector makes 9 Git calls on clean fixtures and 10 on dirty fixtures. `tools/benchmarks/working_tree_fingerprint.py` creates temporary 1k, 10k, and 100k tracked-file repositories, measures one clean and one dirty collection for each, and enforces `benchmarks/working-tree-fingerprint/ratchets.json`.

Run it with:

```sh
PYTHONPATH=tests uv run python tools/benchmarks/working_tree_fingerprint.py
```

The checked-in limits are twice the 2026-09-07 Apple M4 Pro observations, rounded with at least 10 ms slack. They bound both calls and wall time; they are not an unlimited performance allowance.
