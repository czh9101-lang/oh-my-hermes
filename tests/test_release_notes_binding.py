"""Closed note metadata remains part of immutable evidence, including resume."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import TypedDict

from omh.maintenance.release_identity import build_input_manifest, probe_source_identity, verify_release_evidence_bundle
from omh.maintenance.release_notes import NotesMetadata, notes_metadata, validate_notes_metadata
from omh.maintenance.changelog import ChangelogError

DIGEST = 'sha256:' + 'a' * 64


class BoundBundle(TypedDict):
    schema_version: str
    version: str
    source_identity: dict[str, object]
    release_notes: NotesMetadata
    publication_ready: bool


def bundle_for(notes: Path) -> BoundBundle:
    metadata = notes_metadata(notes, '2.0.4')
    identity = probe_source_identity(None, archive_digest=DIGEST)
    identity['input_manifest'] = build_input_manifest(source_identity=identity, release_notes=metadata)
    return {'schema_version': 'omh_release_evidence_bundle/v2', 'version': '2.0.4',
            'source_identity': identity, 'release_notes': metadata, 'publication_ready': True}


class ReleaseNotesBindingTests(unittest.TestCase):
    def test_matches_when_notes_and_manifest_are_unchanged(self):
        # Given
        with TemporaryDirectory() as tmp:
            notes = Path(tmp) / 'notes.md'
            notes.write_bytes(b'- fixture\n')
            bundle = bundle_for(notes)
            # When
            result = verify_release_evidence_bundle(bundle, archive_digest=DIGEST, notes_file=notes)
            # Then
            self.assertEqual(result['verification'], 'matching')
            self.assertEqual(result['notes_binding'], 'matching')
            self.assertTrue(result['publication_ready'])

    def test_refuses_when_bound_artifact_is_missing(self):
        # Given
        with TemporaryDirectory() as tmp:
            notes = Path(tmp) / 'notes.md'
            notes.write_bytes(b'- fixture\n')
            bundle = bundle_for(notes)
            # When
            result = verify_release_evidence_bundle(bundle, archive_digest=DIGEST)
            # Then
            self.assertEqual(result['verification'], 'unverifiable')

    def test_reports_stale_when_note_content_changes(self):
        # Given
        with TemporaryDirectory() as tmp:
            notes = Path(tmp) / 'notes.md'
            notes.write_bytes(b'- fixture\n')
            bundle = bundle_for(notes)
            notes.write_bytes(b'- changed\n')
            # When
            result = verify_release_evidence_bundle(bundle, archive_digest=DIGEST, notes_file=notes)
            # Then
            self.assertEqual(result['verification'], 'stale')

    def test_refuses_when_notes_manifest_digest_is_forged(self):
        # Given
        with TemporaryDirectory() as tmp:
            notes = Path(tmp) / 'notes.md'
            notes.write_bytes(b'- fixture\n')
            bundle = bundle_for(notes)
            manifest = bundle['source_identity']['input_manifest']
            assert isinstance(manifest, dict)
            manifest['digest'] = DIGEST
            # When
            result = verify_release_evidence_bundle(bundle, archive_digest=DIGEST, notes_file=notes)
            # Then
            self.assertEqual(result['verification'], 'unverifiable')

    def test_legacy_is_inspectable_when_notes_binding_is_absent(self):
        # Given
        identity = probe_source_identity(None, archive_digest=DIGEST)
        identity['input_manifest'] = build_input_manifest(source_identity=identity)
        bundle = {'schema_version': 'omh_release_evidence_bundle/v2', 'version': '2.0.4', 'source_identity': identity}
        # When
        result = verify_release_evidence_bundle(bundle, archive_digest=DIGEST)
        # Then
        self.assertEqual(result['verification'], 'matching')
        self.assertEqual(result['notes_binding'], 'not_recorded')
        self.assertFalse(result['publication_ready'])

    def test_rejects_metadata_when_shape_or_path_is_invalid(self):
        # Given
        with TemporaryDirectory() as tmp:
            notes = Path(tmp) / 'notes.md'
            notes.write_bytes(b'- fixture\n')
            valid = notes_metadata(notes, '2.0.4')
            cases = [dict(valid, path='../notes.md'), dict(valid, path='/notes.md'),
                     dict(valid, path='a\\notes.md'), dict(valid, extra=True),
                     dict(valid, byte_length=True), dict(valid, sha256='x'), dict(valid, version='../2.0.4')]
            for value in cases:
                with self.subTest(value=value):
                    # When / Then
                    with self.assertRaises(ChangelogError):
                        validate_notes_metadata(value)
