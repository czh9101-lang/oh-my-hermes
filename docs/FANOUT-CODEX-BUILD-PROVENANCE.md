# Reviewed Codex admission builds

Reviewed 2026-09-10. These exact binaries support only the source-derived fresh
`exec --json` initial `turn/start` queue-rejection classifier. This registry is
not a minimum executor version, provider-quota claim, or native saturation test.
No install, network lookup, or operator attestation is needed by the resolver.

## Source and release

- Official [rust-v0.154.0 release](https://github.com/openai/codex/releases/tag/rust-v0.154.0)
  ([asset digest API](https://api.github.com/repos/openai/codex/releases/tags/rust-v0.154.0)).
- Annotated tag object `36eab01061df3cde5f95ec20a526777b430091ba`
  [peels](https://api.github.com/repos/openai/codex/git/tags/36eab01061df3cde5f95ec20a526777b430091ba)
  to source commit **`6b9826e3aa83b1a5947db50f4332cb9c65f1b340`**.
  The annotated tag is unsigned and the release API says `immutable: false`;
  the source commit and measured executable hashes, not a moving tag, are pinned.
- [Release workflow at that commit](https://github.com/openai/codex/blob/6b9826e3aa83b1a5947db50f4332cb9c65f1b340/.github/workflows/rust-release.yml)
  validates version `0.154.0`, builds with Rust `1.95.0`, exports the checkout
  SHA as `STABLE_GIT_COMMIT`, then strips/signs/packages the binaries.
  [Run 34408513029](https://github.com/openai/codex/actions/runs/34408513029)
  has that `head_sha`; the relevant build/sign/package/verify/release jobs
  succeeded. The overall run failed in the separate `winget` job.

## Exact artifacts

| Platform | Extracted executable SHA-256 | Published and measured archive SHA-256 |
| --- | --- | --- |
| Darwin ARM64 | `4f85982624b3898c8991cb80c0981b2aa71070e3537046c9a95950318a95afcc` | `344310a0a591c1b192e04feff304321a69907c9498baaac331ca7e16ebcef9d7` |
| Linux ARM64/musl | `9b7c1c7abdc26fc3c4f47c77656a8e9121def5483dbae830ef1ee561758448a9` | `583b48df32804213bdcd338c2e5adb06b34340821fa757a726cc0a524fa33c27` |

Download URLs:

- [Darwin archive](https://github.com/openai/codex/releases/download/rust-v0.154.0/codex-aarch64-apple-darwin.tar.gz)
  (asset `553706549`, 88,080,735 bytes; extracted executable 222,655,232 bytes).
- [Linux archive](https://github.com/openai/codex/releases/download/rust-v0.154.0/codex-aarch64-unknown-linux-musl.tar.gz)
  (asset `553706505`, 91,768,360 bytes; extracted executable 227,482,840 bytes).
- [Linux Sigstore bundle](https://github.com/openai/codex/releases/download/rust-v0.154.0/codex-aarch64-unknown-linux-musl.sigstore)
  (asset `553706519`; measured/published SHA-256
  `b4c22362775c17ef58aff248494367f84bdf262b391f4fd338d412d0d96920d6`).

Archive digests matched the official API before regular-file extraction.
Linux `file` identified a stripped, statically linked AArch64 ELF executable;
Darwin `file` identified an ARM64 Mach-O executable.

## Verification actually observed

Linux's legacy `sign-blob` bundle binds the extracted executable's SHA-256,
signature, and certificate in its Rekor `hashedrekord`. OpenSSL verified the
binary signature and Rekor SignedEntryTimestamp (`Verified OK` for each), and
the Fulcio leaf chain at the signed integration time (`OK`). The certificate
SAN is `https://github.com/openai/codex/.github/workflows/rust-release.yml@refs/tags/rust-v0.154.0`;
issuer is `https://token.actions.githubusercontent.com`; source and workflow
SHA extensions identify the exact `6b9826e...` commit above.

Trust material came from [Fulcio](https://fulcio.sigstore.dev/api/v2/trustBundle)
and [Rekor](https://rekor.sigstore.dev/api/v1/log/publicKey) over official HTTPS.
Fulcio root certificate DER SHA-256:
`3ba7b6cc4e95469d4d334b49cb257ad8537076fa84b0ca87ff4ecfe6a54680c1`.
Rekor key DER SHA-256 / bundle log ID:
`c0d23d6ad406973f9559f3ba2d1ca01f84147d8ffc5b8445c224f98b9591801d`.
Log index `2773691469`, signed timestamp `1788991257` (`2026-09-09T22:00:57Z`),
within the certificate's ten-minute validity interval.

Darwin `codesign --verify --strict --verbose=2` returned success with
`valid on disk` / `satisfies its Designated Requirement`. The inspected signer
is `Developer ID Application: OpenAI OpCo, LLC (2DC432GLL2)`, chaining to Apple
Root CA. Its source association uses the official digest, source-pinned release
workflow, and publisher signature; Linux's certificate does not sign Darwin.

On Darwin ARM64, this exact executable then ran only `--version`, `--help`, and
`exec --help`, all exit 0 with empty stderr, in an empty isolated HOME,
CODEX_HOME, TMPDIR and cwd, without inherited credentials. Version output was
`codex-cli 0.154.0`. The existing help/version capability negotiator observed
`codex_exec_json` and the new default resolver returned this executable's
exact digest, release source revision and `source_verified`. No prompt,
provider/inference call, resume, or queue saturation was executed. The isolated
directory was removed and the downloaded binary's original mode restored.
Linux was inspected as data, not executed on the Darwin workstation.

## Narrow source comparison

The definition remains **`b83105710695b70b6d96a64d1e4612bdf68d5f92`**;
`source_revision` separately identifies the reviewed build commit above.
Paths below are under `codex-rs/` at the release commit.

- `app-server/src/in_process.rs:579-607` binds the request ID to its response
  channel and rejects `try_send` Full with `-32001`, exact message
  `in-process app-server request queue is full`, and `data: None`. The rejected
  request is not enqueued/retried; processing follows dequeue at `498-509`.
- `app-server-client/src/lib.rs:137-151,467-486` preserves the request method,
  code and message. Worker event buffering does not print CLI events. Diffs
  against the definition pin remove unrelated user-verification cancellation,
  not the initial `turn/start` refusal contract.
- `exec/src/lib.rs` and `exec/src/event_processor_with_jsonl_output.rs` are
  byte-identical to the definition pin: initial `turn/start` failure propagates
  at `exec/src/lib.rs:1171` before the event loop at `1210-1236`. Configuration
  emits `thread.started`; no CLI `turn.started` can precede this rejection.
- CLI/arg0 propagation adds no error context; arg0 is byte-identical. Rust
  toolchain and anyhow `1.0.103` lock checksum are unchanged from the accepted
  framing trace. With backtrace capture disabled, the exact stderr line is:

```text
Error: turn/start: turn/start failed: in-process app-server request queue is full (code -32001)
```

The classifier still requires a normal positive nonzero failure exit, complete
capture with that exact LF-terminated stderr and one valid `thread.started`,
no conflicting/turn/item evidence, no result sidecar, and fresh exec rather
than resume/fork/review. It claims initial-request admission only,
`process_local`; startup writes and independent dirty recovery remain possible.

Limitations: trust official HTTPS endpoints, signing authorities and the
identified release workflow. No reproducible build or SLSA attestation is
claimed. Verification used OpenSSL chain/signature/SET checks, not full cosign;
CT SCT, Merkle inclusion/consistency, TUF root distribution and current
notarization assessment were not independently verified. The signed Rekor
timestamp is an inclusion promise, not an independently verified Merkle proof.
