from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from importlib import import_module
from typing import TypedDict
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()
from omh.paths import resolve_paths
from omh.routing.chat import route_chat_message
from omh.workflows import memory


class CaptureOptions(TypedDict, total=False):
    stale_after_days: int
    ttl_days: int
    observed: str


class MemoryRecallIncidentTests(unittest.TestCase):
    def __init__(self, methodName='runTest') -> None:
        super().__init__(methodName)
        self.root: Path | None = None

    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))

    @property
    def paths(self):
        assert self.root is not None
        return resolve_paths(self.root / 'qa-profile', self.root / 'hermes')

    def api(self):
        # The repository loader supplies the worktree package dynamically.
        return import_module('omh.workflows.memory_recall_incident')

    def approved(self, summary='Prefer structured response output'):
        candidate = memory.capture_project_memory_candidate(self.paths, summary)['candidate']
        assert isinstance(candidate, dict)
        record = memory.approve_project_memory_candidate(self.paths, candidate['candidate_id'])['record']
        assert isinstance(record, dict)
        return record

    def test_recall_complaint_routing(self) -> None:
        # Given explicit complaints, including punctuation normalized by the router.
        for message in ('Why was my saved response preference not used?',
                        'Why was my saved preference not used?', 'You forgot my preference.',
                        'Memory was not used!', 'You did not use my preference.'):
            with self.subTest(message=message):
                # When
                route = route_chat_message(message)
                # Then
                self.assertEqual(route['selected_skill'], 'memory-sync')
                recommendations = route['recommendations']
                assert isinstance(recommendations, list)
                self.assertIn('operator_surface_fast_path:memory_recall_incident', recommendations[0]['matched'])
        for message in ('Apologize for forgetting.', 'Debug the agent loop.',
                        'Remember this project: I prefer json.',
                        'Translate "memory was not used" into French.'):
            with self.subTest(control=message):
                route = route_chat_message(message)
                self.assertNotEqual(route.get('selected_skill'), 'memory-sync')

    def test_all_recall_stages(self) -> None:
        # Given synthetic reason codes for every stage, not a live receipt schema.
        api = self.api()
        cases = {
            'not_found': ('not_found', 'not_found'),
            'pending_review': ('pending_or_rejected', 'review_candidate'),
            'rejected': ('pending_or_rejected', 'review_candidate'),
            'superseded': ('invalid_or_superseded', 'review_correction'),
            'payload_digest_mismatch': ('invalid_or_superseded', 'review_correction'),
            'scope_mismatch': ('scope_or_perspective_mismatch', 'review_scope_lens'),
            'perspective_mismatch': ('scope_or_perspective_mismatch', 'review_scope_lens'),
            'review_due': ('stale_expired_or_archived', 'review_freshness'),
            'expired_standard': ('stale_expired_or_archived', 'review_freshness'),
            'archived_tier': ('stale_expired_or_archived', 'review_freshness'),
            'no_query_overlap': ('relevance_attention_or_budget_exclusion', 'review_recall_selection'),
            'attention_cut': ('relevance_attention_or_budget_exclusion', 'review_recall_selection'),
            'over_budget': ('relevance_attention_or_budget_exclusion', 'review_recall_selection'),
            'selected_not_rendered': ('selected_not_rendered', 'inspect_provider_rendering'),
            'rendered_delivery_not_observed': ('rendered_delivery_not_observed', 'inspect_host_delivery'),
            'delivered_model_use_unknown': ('delivered_model_use_unknown', 'leave_model_use_unresolved'),
            'used': ('used', 'no_change'),
        }
        for reason, (stage, _) in cases.items():
            with self.subTest(reason=reason):
                # When
                result = api.diagnose_synthetic_recall_stage(reason)
                # Then
                self.assertEqual(result['stage'], stage)
                self.assertEqual(result['evidence_basis'], 'synthetic')
        # Given a real local store; canonical selector owns these reasons.
        record = self.approved()
        for request, expected in (
            (api.RecallIncidentRequest(record_id=record['record_id']), 'unresolved'),
            (api.RecallIncidentRequest(record_id=record['record_id'], query='volcano'), 'relevance_attention_or_budget_exclusion'),
            (api.RecallIncidentRequest(record_id=record['record_id'], max_chars=1), 'relevance_attention_or_budget_exclusion'),
            (api.RecallIncidentRequest(record_id=record['record_id'], scope_ref='another-project'), 'scope_or_perspective_mismatch'),
        ):
            with self.subTest(request=request):
                result = api.build_memory_recall_incident(self.paths, request)
                self.assertEqual(result['stage'], expected)

        fixtures: tuple[tuple[str, CaptureOptions, str | None, str], ...] = (
            ('pending', {}, None, 'pending_review'),
            ('rejected', {}, None, 'rejected'),
            ('stale', {'stale_after_days': 1}, None, 'stale_review_required'),
            ('expired', {'ttl_days': 1}, None, 'expired_standard'),
            ('archived', {}, 'archive', 'archived_tier'),
            ('perspective', {'observed': 'codex'}, None, 'perspective_mismatch'),
            ('retired', {'ttl_days': 1}, None, 'retired'),
        )
        for name, capture_options, tier, reason in fixtures:
            with self.subTest(local_fixture=name), patch.object(memory, 'utc_now', return_value='2000-12-31T00:00:00Z'):
                # Given public candidate/approval/attention operations, no mocked selector.
                candidate = memory.capture_project_memory_candidate(self.paths, name + ' preference', **capture_options)['candidate']
                assert isinstance(candidate, dict)
                anchor = candidate['candidate_id']
                if name == 'rejected':
                    memory.reject_project_memory_candidate(self.paths, anchor)
                elif name != 'pending':
                    local_record = memory.approve_project_memory_candidate(self.paths, anchor)['record']
                    assert isinstance(local_record, dict)
                    anchor = local_record['record_id']
                    if tier:
                        memory.apply_memory_attention_change(self.paths, anchor, tier=tier)
                    if name == 'retired':
                        memory.apply_memory_retirement(self.paths, now=datetime(2001, 1, 1, tzinfo=timezone.utc))
                request = api.RecallIncidentRequest(record_id=anchor, observed='claude',
                    now=datetime(2001, 1, 1, tzinfo=timezone.utc))
                # When
                result = api.build_memory_recall_incident(self.paths, request)
                # Then
                self.assertEqual(result['reason_code'], reason)
                self.assertNotEqual(result['stage'], 'unresolved')

    def test_receipt_absence_is_not_nondelivery(self) -> None:
        # Given selected memory but no record-bound live evidence.
        api = self.api()
        record = self.approved()
        for count in (None, 0, 7):
            with self.subTest(count=count):
                request = api.RecallIncidentRequest(record_id=record['record_id'], provider_served_count=count)
                # When
                result = api.build_memory_recall_incident(self.paths, request)
                # Then
                self.assertEqual(result['stage'], 'unresolved')
                self.assertEqual(result['last_proven_stage'], 'selected')
                self.assertEqual(result['evidence_surfaces']['canonical_recall_pack']['status'], 'observed')
                self.assertEqual(result['evidence_surfaces']['live_prefetch_receipt']['status'], 'unavailable')
                self.assertEqual(result['evidence_surfaces']['host_delivery']['status'], 'unavailable')
                self.assertEqual(result['evidence_surfaces']['provider_recall_status']['status'],
                                 'unavailable' if count is None else 'not_authoritative')
                self.assertIsNone(result['delivery_observed'])

    def test_incident_privacy_and_scope(self) -> None:
        # Given private content and a foreign home with an unrelated record.
        api = self.api()
        record = self.approved('private-memory-sentinel')
        assert self.root is not None
        foreign = resolve_paths(self.root / 'foreign', self.root / 'foreign-hermes')
        candidate = memory.capture_project_memory_candidate(foreign, 'foreign-summary-sentinel')['candidate']
        assert isinstance(candidate, dict)
        foreign_record = memory.approve_project_memory_candidate(foreign, candidate['candidate_id'])['record']
        assert isinstance(foreign_record, dict)
        request = api.RecallIncidentRequest(record_id=record['record_id'], query='raw-query-sentinel',
                                           session_id='raw-session-sentinel')
        # When
        result = api.write_memory_recall_incident(self.paths, request)
        # Then
        persisted = list(self.paths.memory_incidents_dir.glob('*.json'))
        self.assertEqual(len(persisted), 1)
        text = persisted[0].read_text()
        self.assertEqual(json.loads(text), result)
        for sentinel in ('private-memory-sentinel', 'raw-query-sentinel', 'raw-session-sentinel',
                         'foreign-summary-sentinel', foreign_record['record_id']):
            self.assertNotIn(sentinel, text)
        for anchor in ('../../foreign/record', 'password=private-credential-sentinel'):
            with self.assertRaises(ValueError):
                api.RecallIncidentRequest(record_id=anchor)
        self.assertEqual(api.build_memory_recall_incident(self.paths, api.RecallIncidentRequest(
            record_id=foreign_record['record_id']))['stage'], 'not_found')

    def test_prepared_stage_specific_remediation(self) -> None:
        # Given distinct causes and a reviewed local record.
        api = self.api()
        expected = {'not_found': 'capture_for_review', 'pending_review': 'review_candidate',
                    'superseded': 'review_correction', 'scope_mismatch': 'review_scope_lens',
                    'review_due': 'review_freshness', 'over_budget': 'review_recall_selection',
                    'selected_not_rendered': 'inspect_provider_rendering',
                    'rendered_delivery_not_observed': 'inspect_host_delivery',
                    'delivered_model_use_unknown': 'leave_model_use_unresolved', 'used': 'no_change'}
        record = self.approved()
        before = {str(p): p.read_bytes() for p in self.paths.memory_dir.rglob('*') if p.is_file()}
        # When
        results = [api.diagnose_synthetic_recall_stage(reason) for reason in expected]
        local = api.build_memory_recall_incident(self.paths, api.RecallIncidentRequest(record_id=record['record_id']))
        # Then
        for result, action in zip(results, expected.values(), strict=True):
            self.assertEqual(result['remediation']['action'], action)
            self.assertEqual(result['remediation']['state'], 'prepared_not_applied')
            self.assertEqual(result['remediation']['requires'], ['memory_curation_review/v1 approval', 'native_write_approval'])
            self.assertFalse(result['authorizes_mutation'])
        self.assertFalse(local['authorizes_mutation'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.paths.memory_dir.rglob('*') if p.is_file()})

    def test_provider_fault_domain_boundary(self) -> None:
        # Given explicit synthetic provider/host failures vs aggregate local status.
        api = self.api()
        for reason, domain in (('selected_not_rendered', 'provider'),
                               ('rendered_delivery_not_observed', 'unresolved'),
                               ('delivered_model_use_unknown', 'unresolved')):
            # When
            result = api.diagnose_synthetic_recall_stage(reason)
            # Then
            self.assertEqual(result['fault_domain'], domain)
            self.assertFalse(result['omh_correction_proposed'])
        record = self.approved()
        result = api.build_memory_recall_incident(self.paths, api.RecallIncidentRequest(
            record_id=record['record_id'], provider_served_count=0))
        self.assertEqual(result['fault_domain'], 'unresolved')
        self.assertFalse(result['omh_correction_proposed'])

    def test_candidate_digest_and_perspective_when_locally_available(self) -> None:
        # Given a pending claim and a perspective-scoped approved claim.
        api = self.api()
        summary = 'pending preference'
        candidate = memory.capture_project_memory_candidate(self.paths, summary)['candidate']
        assert isinstance(candidate, dict)
        request = api.RecallIncidentRequest(claim_digest=hashlib.sha256(summary.encode()).hexdigest())
        # When
        pending = api.build_memory_recall_incident(self.paths, request)
        # Then
        self.assertEqual(pending['reason_code'], 'pending_review')
        self.assertEqual(pending['anchor']['candidate_id'], candidate['candidate_id'])

    def test_corrupt_store_when_expected_claim_cannot_be_checked(self) -> None:
        # Given an unreadable store entry, absence is not provable.
        api = self.api()
        directory = self.paths.memory_dir / 'records'
        directory.mkdir(parents=True)
        (directory / 'mem_0000000000000000.json').write_text('{')
        # When
        result = api.build_memory_recall_incident(self.paths, api.RecallIncidentRequest(
            claim_digest='a' * 64))
        # Then
        self.assertEqual(result['stage'], 'unresolved')
        self.assertEqual(result['evidence_surfaces']['omh_approved_records']['status'], 'unavailable')
