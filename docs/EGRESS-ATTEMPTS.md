# Write-ahead egress attempts

Audience: Hermes integrators and operators. Normal users can ask Hermes to
inspect an uncertain tool action; the commands below are control-plane references.

The optional installed-plugin guard records an `external_effect_attempt/v1`
before invoking a configured registry handler. It observes final arguments after
sibling hook rewrites, strips its private correlation token before forwarding,
and records at most one terminal observation. A handler returning is not remote
delivery. A missing outcome is unknown, and replaying a recorded call never
invokes the handler again.

## Explicit configuration

Configure only already-registered tools whose arguments and effect class are
known to the operator. The feature is absent by default.

```yaml
plugins:
  entries:
    omh:
      allow_tool_override: true
      settings:
        egress_attempts:
          enabled: true
          tools:
            send_probe:
              action_class: message_send
              destination_class: chat_channel
              destination_arg: channel
              payload_arg: body
```

`send_probe` is an illustrative registry name, not a newly installed transport.
An optional `omh_home` inside `egress_attempts` selects the journal home; otherwise
the guard uses `OMH_HOME` or the normal OMH home. The registration binds that home
once. It does not change configuration or grant its own override permission.
Missing permission or unavailable handler binding blocks the configured tool.

Supported action classes are `message_send`, `review_submit`, `ci_dispatch`,
`merge`, and `external_write`. Destination classes are `chat_channel`,
`repository`, `review_thread`, `workflow`, and `endpoint`. Classification comes
from operator settings, never model arguments. Unavailable approval and provider
idempotency references remain null.

## Storage and inspection

The private runtime journal is
`runtime/journal/external_effect_attempts.sqlite3` under the selected OMH home.
Every attempt transaction commits with SQLite `synchronous=FULL` and
`journal_mode=DELETE` before the handler may run; terminal transactions use the
same settings. Indexed call identity prevents duplicate attempts. On Windows,
SQLite's native sync uses `FlushFileBuffers`; OMH does not attempt the unsupported
CRT directory open/fsync. On POSIX, first database creation additionally fsyncs
the immediate parent directory. A SQLite write/commit/sync error or a POSIX
directory-sync error blocks the handler, even if the attempt row already committed.
An unresolved committed row remains non-replayable.

Cold schema creation groups both tables and both indexes in one explicit
transaction, with the same FULL/DELETE settings, instead of four autocommit
transactions. A schema statement or commit failure rolls back that transaction
on close and blocks dispatch. The attempt still has its own commit before the
handler runs. Reopening an initialized schema does not commit database changes
or reserve a writer lock for schema checks; there is no cache or prewarming.

This is SQLite's FULL/native-sync contract, not an additional Windows directory
flush or an unconditional power-loss guarantee. FULL with a DELETE rollback
journal does not guarantee that the last transaction survives power loss on
every filesystem. The extra POSIX flush is only on first creation and does not
sync newly created ancestor directories. Storage must honor SQLite's locking and
flush operations. See [SQLite synchronization](https://www.sqlite.org/pragma.html#pragma_synchronous)
and [filesystem assumptions](https://www.sqlite.org/atomiccommit.html#_incomplete_disk_flushes).

Public digest references encode all 256 SHA-256 bits as unpadded base64url. Raw
arguments, destinations, payloads, results, and correlation tokens are not stored.

```sh
omh runtime egress-attempts --limit 50
```

Set `OMH_OUTPUT=json` in the invoking environment for machine-readable output.
The reader is explicit and bounded to 200 attempts. Ordinary runtime status
does not probe the attempt database. A later observed external-effect receipt
may cite `attempt:<attempt_id>` through its existing `evidence_refs`; the closed
approval and outcome-receipt schemas are unchanged.

## Supported boundary and verification

Coverage is limited to the native executor / model-tools / registry route.
Inline tools, unrelated direct handlers, connector callbacks, and arbitrary
non-tool egress are not covered. Entries with `max_result_size_chars` or
`dynamic_schema_overrides` cannot be faithfully replaced through the current
host API and are refused. The host restores the original registration on unload.

Run the portable native proof with an installed Hermes checkout:

```sh
uv run --isolated --offline --python 3.11 tools/benchmarks/native_omh_egress_proof.py \
  --hermes-root /path/to/hermes-agent --omh-source "$PWD/src/plugin_bundle/omh"
uv run python tools/benchmarks/egress_attempts.py
```

The native proof uses an in-process safe spy, not an external send. The separate
benchmark measures non-egress overhead, append latency, bounded rows, and indexed
lookup against the fixed limits; it does not measure remote delivery.
