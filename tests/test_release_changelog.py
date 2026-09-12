"""Changelog extraction through the existing release command boundary."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from functools import partial
import subprocess
import sys

run_cli = partial(subprocess.run, capture_output=True, text=True, timeout=30)


class ReleaseChangelogTests(unittest.TestCase):
    def test_extracts_exact_markdown_when_version_is_stamped(self):
        # Given
        body = '- fixture $HOME "quotes"\n\n```md\n## Unreleased\n```\n'
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            changelog = root / 'CHANGELOG.md'
            original = '# Changelog\n\n## Unreleased\n\n## 2.0.4 - 2026-09-13\n\n' + body
            changelog.write_text(original, encoding='utf-8')
            notes = root / 'notes.md'
            # When
            result = run_cli([sys.executable, '-P', '-m', 'omh.cli', 'release', 'notes', '--version', '2.0.4', '--repo-root', str(root), '--notes-file', str(notes), '--json'])
            # Then
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(notes.read_bytes(), body.encode())
            self.assertEqual(changelog.read_text(), original)

    def test_pure_parser_preserves_interior_when_markdown_is_special(self):
        from omh.maintenance.changelog import extract_notes
        # Given
        body = '### Changed\n\n- $HOME "quotes" \\ escapes\n\n~~~md\n## 2.0.4 - 1999-01-01\n~~~\n\n```md\n## Unreleased\n```\n\n- caf\u00e9 \u2603  \n'
        raw = ('## Unreleased\n\n## 2.0.4 - 2026-09-13\n\n' + body + '\n## 2.0.3 - 2026-09-12\n\n- old\n').encode()
        # When
        result = extract_notes(raw, '2.0.4')
        # Then
        self.assertEqual(result, body.encode())

    def test_pure_parser_normalizes_crlf_when_artifact_is_extracted(self):
        from omh.maintenance.changelog import extract_notes
        # Given
        raw = b'## 2.0.4 - 2026-09-13\r\n\r\n- first\r\n\r\n- second  \r\n\r\n'
        # When
        result = extract_notes(raw, '2.0.4')
        # Then
        self.assertEqual(result, b'- first\n\n- second  \n')

    def test_preserves_unicode_separators_when_they_are_not_markdown_newlines(self):
        from omh.maintenance.changelog import extract_notes
        # Given
        body = 'prefix\u2028## 2.0.3 - 2026-09-12\n\n- still target content\n'
        raw = ('## 2.0.4 - 2026-09-13\n\n' + body).encode()
        # When
        result = extract_notes(raw, '2.0.4')
        # Then
        self.assertEqual(result, body.encode())

    def test_refuses_stamp_when_output_would_exceed_parser_bound(self):
        from datetime import date
        from omh.maintenance.changelog import ChangelogError, MAX_CHANGELOG_BYTES, stamp_changelog
        # Given
        tail = b'\n## Unreleased\n\n- fixture\n'
        raw = b'x' * (MAX_CHANGELOG_BYTES - len(tail)) + tail
        # When / Then
        with self.assertRaises(ChangelogError):
            stamp_changelog(raw, '2.0.4', date(2026, 9, 13))

    def test_refuses_invalid_section_when_source_is_malformed(self):
        from omh.maintenance.changelog import ChangelogError, extract_notes
        # Given
        cases = [b'## Unreleased\n\n- pending\n', b'## 2.0.4 - 2026-09-13\n\n',
                 b'## 2.0.4 - 2026-09-13\n\n- a\n## 2.0.4 - 2026-09-14\n\n- b\n',
                 b'## 2.0.4 - 2026-02-30\n\n- a\n', b'\xff', b'x' * (2 * 1024 * 1024 + 1),
                 b'## 2.0.4 - 2026-09-13\n\n' + b'a' * (256 * 1024 + 1)]
        for raw in cases:
            with self.subTest(size=len(raw)):
                # When / Then
                with self.assertRaises(ChangelogError):
                    extract_notes(raw, '2.0.4')

    def test_refuses_bad_version_when_target_is_untrusted(self):
        from omh.maintenance.changelog import ChangelogError, extract_notes
        # Given
        for version in ('', '../2.0.4', '2.0.4\n', '2.04.0', '2.0.4-beta'):
            with self.subTest(version=version):
                # When / Then
                with self.assertRaises(ChangelogError):
                    extract_notes(b'## 2.0.4 - 2026-09-13\n\n- a\n', version)
