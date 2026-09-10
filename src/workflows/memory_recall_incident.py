"""Read-only, expected-record diagnosis over canonical OMH recall selection.

A prepared pack and an aggregate provider count cannot prove rendering,
delivery, or model use. The canonical live receipt dependency is #1452.
"""
from __future__ import annotations

import json
import re

from ..plugin_bundle.omh.hermes_memory import HERMES_MEMORY_FILES, read_hermes_memory_file
from ..system.local_store import atomic_write_json, read_json_object_result
from ..system.paths import OmhPaths
from . import memory
from .memory_recall_incident_model import (
    EvidenceSurface, Incident, RecallIncidentRequest, diagnosis, digest,
    diagnose_synthetic_recall_stage,
)

__all__ = ['RecallIncidentRequest', 'build_memory_recall_incident',
           'write_memory_recall_incident', 'diagnose_synthetic_recall_stage']


def build_memory_recall_incident(paths: OmhPaths, request: RecallIncidentRequest) -> Incident:
    """Inspect one scoped anchor; never attach raw recall packs to an incident."""
    pack = memory.build_project_memory_recall_pack(
        paths, request.query, session_id=request.session_id,
        scope_kind=request.scope_kind, scope_ref=request.scope_ref,
        observer=request.observer, observed=request.observed,
        limit=request.limit, max_chars=request.max_chars, now=request.now,
    )
    records, unreadable = memory.scan_project_memory_records(paths)
    surfaces: dict[str, EvidenceSurface] = {
        name: {'status': 'unavailable', 'basis': basis}
        for name, basis in (
            ('live_prefetch_receipt', 'canonical_1452_contract_unavailable'),
            ('host_delivery', 'no_record_bound_observation'),
            ('model_use', 'no_record_bound_observation'),
            ('provider_availability', 'runtime_not_inspected'),
            ('provider_recall_status', 'not_supplied'),
        )
    }
    surfaces['canonical_recall_pack'] = {'status': 'observed', 'basis': 'prepared_local_selection'}
    surfaces['omh_approved_records'] = {
        'status': 'unavailable' if unreadable else 'observed',
        'basis': 'partial_unreadable_store' if unreadable else 'local_store',
    }
    if request.provider_served_count is not None:
        surfaces['provider_recall_status'] = {'status': 'not_authoritative', 'basis': 'caller_supplied_aggregate_count'}
    native = tuple(read_hermes_memory_file(
        paths.hermes_home / 'memories' / label, label=label, cap=cap,
        now=request.now.timestamp() if request.now else None,
    ) for label, _, cap in HERMES_MEMORY_FILES)
    native_error = any(item.error for item in native)
    surfaces['native_inventory'] = {
        'status': 'unavailable' if native_error else 'not_authoritative',
        'basis': 'read_error' if native_error else 'native_inventory_not_omh_reviewed',
    }
    native_match = bool(request.claim_digest) and any(
        digest(entry) == request.claim_digest for item in native for entry in item.entries)
    candidates: list[dict[str, object]] = []
    candidate_error = False
    for path in sorted((paths.memory_dir / 'candidates').glob('*.json')):
        if path.is_symlink():
            candidate_error = True
            continue
        memory._assert_under_memory_root(paths, path)
        value, error = read_json_object_result(path)
        candidate_error = candidate_error or error is not None
        if value is not None:
            candidates.append(value)
    surfaces['omh_candidates'] = {
        'status': 'unavailable' if candidate_error else 'observed',
        'basis': 'partial_unreadable_store' if candidate_error else 'local_store',
    }
    # Scope helpers are the selector's own filters. Digest lookup never exposes
    # a claim from another project; explicit IDs may explain a lens mismatch.
    def anchored(value: dict[str, object]) -> bool:
        if request.record_id:
            return request.record_id in (value.get('record_id'), value.get('candidate_id'))
        return memory._record_scope_matches(value, scope_kind=request.scope_kind, scope_ref=request.scope_ref) and (
            digest(str(value.get('summary', ''))) == request.claim_digest)

    matches = [value for value in records if anchored(value)]
    pending = [value for value in candidates if anchored(value)]
    anchor = {'requested_digest': digest(request.record_id) if request.record_id else request.claim_digest}
    reason = 'not_found'
    last = 'none'
    if unreadable or candidate_error or native_error:
        reason = 'store_unavailable'
    if native_match:
        reason = 'native_only_not_omh_reviewed'
    if pending:
        candidate = pending[0]
        reason = str(candidate.get('status', 'selection_unresolved'))
        candidate_id = str(candidate.get('candidate_id', ''))
        # Only canonical generated IDs can cross the persistence boundary.
        if re.fullmatch(r'cand_[0-9a-f]{16}', candidate_id):
            anchor['candidate_id'] = candidate_id
        last = 'candidate'
    if len(matches) > 1:
        reason = 'ambiguous_anchor'
    elif matches:
        record = matches[0]
        record_id = str(record.get('record_id', ''))
        if re.fullmatch(r'mem_[0-9a-f]{16}', record_id):
            anchor['record_id'] = record_id
        anchor['claim_digest'] = digest(str(record.get('summary', '')))
        last = 'stored'
        if not memory._record_scope_matches(record, scope_kind=request.scope_kind, scope_ref=request.scope_ref):
            reason = 'scope_mismatch'
        elif not memory._record_perspective_matches(
            record, observer=(request.observer.strip().lower() if request.observer else None),
            observed=(request.observed.strip().lower() if request.observed else None),
        ):
            reason = 'perspective_mismatch'
        else:
            reason = 'selection_unresolved'
            # Legacy pack API returns object-valued JSON; narrow at that boundary.
            excluded = pack['excluded_records']
            included = pack['included_records']
            if not isinstance(excluded, list) or not isinstance(included, list):
                raise ValueError('invalid canonical recall pack record lists')
            for item in excluded:
                if isinstance(item, dict) and item.get('record_id') == record_id:
                    reason = str(item['reason'])
            if any(isinstance(item, dict) and item.get('record_id') == record_id for item in included):
                reason, last = 'selected_live_evidence_unavailable', 'selected'
    if not pack['enabled']:
        reason = 'project_memory_disabled'
    config = {
        'home_digest': digest(str(paths.omh_home.resolve())),
        'session_digest': digest(request.session_id),
        'policy_digest': digest(json.dumps(pack['policy'], sort_keys=True)),
        'selection_digest': digest(json.dumps({
            'scope': pack['scope'], 'perspective': pack['perspective'],
            'query_digest': digest(request.query), 'limit': request.limit, 'max_chars': request.max_chars,
        }, sort_keys=True)),
    }
    result: Incident = {
        **diagnosis(reason, 'local_inspection'),
        'schema_version': 'memory_recall_incident/v1',
        'incident_id': 'incident_' + digest(json.dumps([anchor, config, reason], sort_keys=True))[:24],
        'anchor': anchor, 'configuration_identity': config, 'evidence_surfaces': surfaces,
        'last_proven_stage': last, 'delivery_observed': None, 'model_use_observed': None,
        'external_review_status': 'not_omh_reviewed', 'redaction_policy': 'metadata_only',
        'claim_boundary': 'Local diagnosis and prepared remediation only; not live rendering, delivery, model use, '
                          'dispatch, execution, review, CI, merge, or native-write evidence.',
    }
    return result


def write_memory_recall_incident(paths: OmhPaths, request: RecallIncidentRequest) -> Incident:
    """Explicitly persist only the bounded diagnosis, never caller-supplied artifacts."""
    result = build_memory_recall_incident(paths, request)
    destination = paths.memory_incidents_dir / f"{result['incident_id']}.json"
    memory._assert_under_memory_root(paths, destination)
    atomic_write_json(destination, dict(result), private=True)
    return result
