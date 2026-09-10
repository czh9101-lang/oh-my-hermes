# Plugin Catalog Coverage

**Audience:** maintainers and Hermes host operators.

OMH ships one packaged ecosystem catalog snapshot and a small hand-curated
outcome matrix. Both are pinned to a repository revision, so a plugin the host
admitted this morning, a reviewed revision that moved under an existing
identity, a withdrawal, or a host-version constraint the host refused stays
invisible until somebody edits the repository.

`plugin_catalog_snapshot/v1` is the input that closes that gap without OMH
acquiring anything. An authorized host or operator exports its own catalog to
a file; OMH validates it, reconciles it against its own workflow ownership,
and reports `plugin_catalog_coverage/v1`.

Read-only, in the strict sense: no network request, no credential read, no
plugin import, no dependency installation, no host configuration change, no
GitHub mutation, no background polling. The supplied file is the only input.
The host keeps everything it already owns — acquisition, admission,
compatibility enforcement, installation, activation, updates, removal blocks,
permissions, and runtime execution. OMH owns interpretation, routing, and
evidence-bounded status.

## Command

```sh
omh ecosystem plugin-catalog coverage \
  --input current-snapshot.json \
  --previous previous-snapshot.json \
  --profile default \
  --now 2026-09-10T10:00:00Z \
  --json
```

Every flag except `--input` is optional. `--previous` is what turns the report
from a listing into a change report. `--profile` refuses a snapshot taken for
a different profile. `--now` pins the clock the freshness decision is made
against.

## The snapshot contract

```json
{
  "schema_version": "plugin_catalog_snapshot/v1",
  "producer": {"kind": "hermes_host", "ref": "hermes-host-a", "host_version": "0.13.2"},
  "profile_ref": "default",
  "observed_at": "2026-09-10T09:00:00Z",
  "catalog_revision": "catalog-rev-2026-09-10",
  "entry_count": 1,
  "entries": [
    {
      "plugin_id": "alpha-provider",
      "content_revision": "rev-2",
      "tier": "verified",
      "declared_capabilities": ["provider"],
      "required_env_names": ["ALPHA_API_KEY"],
      "platform_limits": [],
      "host_version_constraint": ">=0.13",
      "host_version_status": "satisfied",
      "removal_status": "active"
    }
  ]
}
```

Both key sets are closed. An unsupported field is a refusal, not a warning.

| Field | What it is |
| --- | --- |
| `producer.kind` | `hermes_host` or `authorized_operator`. OMH never produces a snapshot. |
| `profile_ref` | Which host profile the snapshot describes. |
| `catalog_revision` | The host's own revision or deterministic digest for the catalog state. |
| `plugin_id` | Stable identity. Change classification keys on this. |
| `content_revision` | The reviewed content behind that identity at snapshot time. |
| `tier` | `official`, `verified`, `community`, or `unreviewed`. |
| `declared_capabilities` | Open vocabulary of lowercase tokens. Claims, not observations. |
| `required_env_names` | Environment variable **names**. A credential value here is refused. |
| `platform_limits` | `desktop`, `headless`, `linux`, `macos`, `windows`. |
| `host_version_constraint` | Opaque text OMH never parses. |
| `host_version_status` | `satisfied`, `unsatisfied`, or `unknown` — the host's own answer. |
| `removal_status` | `active`, `deprecated`, or `removed`. |

**OMH does not solve versions.** `host_version_constraint` is carried through
as opaque text; `host_version_status` is where the host reports the result of
enforcing it, because enforcement is the host's job.

**Names, not values.** `required_env_names` is the one field whose honest
content reads like a secret (`GITHUB_TOKEN`, `ALPHA_API_KEY`). It is screened
with the narrow issued-credential detector rather than the wider
sensitive-word one, so a name is accepted and an issued key is refused. Every
other string is screened as a bounded opaque reference. There is no field a
file body, a repository excerpt, or a credential value can arrive in.

## What the answer says

Each row carries exactly one **coverage class** and exactly one **change
class**. They answer different questions and never merge.

| Coverage class | When |
| --- | --- |
| `mapped` | A declared capability OMH owns a workflow for. |
| `generic_review` | Declared something, but nothing OMH owns — including host-core-only mechanics. |
| `incompatible` | `host_version_status` is `unsatisfied`. |
| `removed` | `removal_status` is `removed`, or the entry left the catalog. |
| `unknown` | Declared no capabilities at all; there is nothing to interpret. |

| Change class | When |
| --- | --- |
| `added` | Identity absent from the prior snapshot. With no prior snapshot, every row is a first sighting. |
| `changed` | Identity present before, and tracked metadata moved. `changed_fields` names what. |
| `unchanged` | Identity present before, nothing tracked moved. |
| `removed` | Identity present before, absent now. |

Withdrawal and incompatibility are decided before capability mapping, so a
withdrawn or refused entry is never routed as adoption guidance for something
the host will not run.

### Routing

| Declared capability | Owner workflow |
| --- | --- |
| `provider` | `provider-profile-posture` |
| `connector` | `external-connector-readiness` |
| `observability` | `ops-observability-card` |
| `memory` | `memory-sync` |
| `media` | `media-input-operator` |
| `host_core` | none — reported with `owner_boundary: hermes_host` |
| anything else, or nothing | `skill-scout` (bounded generic review) |
| held entries | `security-safety-review` (plugin risk review) |

An entry declaring several owned capabilities takes the first in that order as
its primary `route` and lists the rest in `additional_routes`, so the primary
owner is the same on every run.

### What never happens

- **No entry reaches a ready state.** Catalog presence cannot produce one.
  `adoption_guidance` is `route_to_owner`, `generic_review`, `host_owned`, or
  `held`; the owner workflow's own readiness contract is where an adoption
  answer comes from.
- **A removal hold reports a catalog withdrawal only.** It never says an
  installed copy was disabled or uninstalled — OMH cannot see installed copies,
  and `installed_copy_state` is listed in the row's `unavailable_evidence`.
- **Declared capabilities stay claims.** They can be incomplete or drift from
  implementation. `host_observed_behavior`, `host_permission_grant`, and
  `plugin_execution` are in every row's `unavailable_evidence`, always.

## Failing closed

| Input | Result |
| --- | --- |
| No `--input` | `snapshot_state: unavailable`, no entries, exit 1 |
| Missing or unreadable file | `snapshot_state: unavailable`, no entries, exit 1 |
| Oversized file or entry list | Refused before parsing or reconciling; never truncated |
| Malformed, unsupported field, duplicate identity | Refused with bounded diagnostics |
| Snapshot from another profile | Refused — it is a real observation of something else |
| Older than the freshness horizon | Reported with `snapshot_state: stale` and every row held on `stale_snapshot` |

Diagnostics are positional. Each one names an entry index and a field name and
never the value that failed, so an untrusted snapshot cannot route its own
content through OMH's error output. The list is capped and closed with a count.

**Absence never inherits presence.** An `unavailable` answer carries no
entries and no packaged-catalog substitute. The `packaged_reference` block is
still attached with `used_as_host_coverage: false`, so a reader can see that
OMH declined to use it rather than wondering whether it silently did.

## The packaged catalog

`omh ecosystem awesome-hermes summary|list|inspect|outcomes` still work
unchanged. Every one of their payloads now carries
`coverage_scope: snapshot_limited` and a note saying so in words: they project
one pinned upstream revision packaged into an OMH release, and they cannot
report anything the host admitted, revised, or withdrew since. For current
host coverage, reconcile a snapshot.

## Implementation

| Surface | Path |
| --- | --- |
| Input contract and validation | `src/workflows/plugin_catalog_snapshots.py` |
| Reconciliation and routing | `src/workflows/plugin_catalog_coverage.py` |
| CLI adapter | `src/commands/plugin_catalog.py` |
| Tests | `tests/test_plugin_catalog_coverage.py` |
