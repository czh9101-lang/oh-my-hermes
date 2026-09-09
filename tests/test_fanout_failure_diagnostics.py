"""Foundation proofs only; dispatcher/status/native acceptance is separate."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tracemalloc
import unittest

from _local_package import load_local_package
from five_issue_process_fixture import process_fixture

load_local_package()

from omh.coding import fanout_failure_diagnostics as diag_api
from omh.coding import fanout_output as output


class FailureDiagnosticsFoundation(unittest.TestCase):
    def capture(self, stdout: bytes = b'', stderr: bytes = b'', *,
                protocol: str = 'plain',
                known_secrets: tuple[str, ...] = ()) -> output.FanoutOutput:
        capture = output.FanoutOutput(protocol=protocol, known_secrets=known_secrets)
        streams: tuple[tuple[diag_api.StreamName, bytes], ...] = (('stdout', stdout), ('stderr', stderr))
        for name, data in streams:
            for start in range(0, len(data), 137):
                capture.feed(name, data[start:start + 137])
            capture.finish(name)
        return capture

    def diagnostic(self, capture: output.FanoutOutput, *, phase: str = 'worker',
                   reason: str = 'nonzero', returncode: int | None = 3,
                   exit_code_source: str = 'process', owner: str = 'generic',
                   failed: bool = True) -> diag_api.FailureDiagnostic | None:
        return diag_api.build_failure_diagnostic(
            fanout_id='fanout-1', unit_id='unit-a', run_ref='run-a',
            owner=owner, attempt_id='attempt-1', worktree_ref='tree-a',
            base_sha='a' * 40, observed_revision='b' * 40, phase=phase,
            reason=reason, returncode=returncode, exit_code_source=exit_code_source,
            streams=capture.streams(), failed=failed)

    def assert_bounded(self, stream: diag_api.StreamDiagnostic) -> None:
        raw = stream['text'].encode('utf-8')
        self.assertEqual(stream['limit_bytes'], 2000)
        self.assertEqual(stream['limit_lines'], 20)
        self.assertLessEqual(len(raw), 2000)
        self.assertLessEqual(len(stream['text'].splitlines()), 20)
        self.assertEqual(stream['kept_bytes'], len(raw))
        self.assertEqual(stream['kept_lines'], len(stream['text'].splitlines()))
        self.assertEqual(stream['digest'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(stream['digest_basis'], 'sanitized_utf8')
        self.assertNotIn('spill_ref', stream)

    def test_d1_real_stderr_failure_and_cleanup(self):
        with process_fixture(['--exit-code', '3'], stderr=b'compiler failed\n') as child:
            child.event('ready')
            child.send('start')
            child.event('started')
            child.event('emitted')
            result = child.finish()
            diagnostic = self.diagnostic(self.capture(result.stdout, result.stderr),
                                         returncode=result.returncode)
            assert diagnostic is not None
            self.assertEqual(diagnostic['schema_version'], 'fanout_failure_diagnostic/v1')
            self.assertEqual((diagnostic['unit_id'], diagnostic['phase'],
                              diagnostic['returncode'], diagnostic['exit_code_source']),
                             ('unit-a', 'worker', 3, 'process'))
            stream = diagnostic['streams'][1]
            self.assertEqual((stream['stream'], stream['state']), ('stderr', 'retained'))
            self.assertTrue(stream['text'])
            self.assert_bounded(stream)
        self.assertTrue(child.cleanup['verified_absent'])
        self.assertEqual(child.cleanup['errors'], [])
        self.assertIsNotNone(child.process.returncode)
        self.assertTrue(all(not Path(p).exists() for p in child.cleanup['owned_resources']))

    def test_d2_stream_order_is_not_chronology(self):
        capture = output.FanoutOutput()
        capture.feed('stderr', b'phase B failed\n')
        capture.feed('stdout', b'phase A failed\n')
        capture.finish('stderr')
        capture.finish('stdout')
        diagnostic = self.diagnostic(capture)
        assert diagnostic is not None
        streams = diagnostic['streams']
        self.assertEqual([s['stream'] for s in streams], ['stdout', 'stderr'])
        self.assertEqual([s['original_bytes'] for s in streams], [15, 15])
        self.assertNotEqual(streams[0]['text'], streams[1]['text'])
        self.assertNotIn('chronology', json.dumps(streams))

    def test_d2_structured_fragments_never_merge_across_streams(self):
        seen: list[object] = []
        capture = output.FanoutOutput(protocol='codex', observer=lambda stream, event: seen.append(event))
        capture.feed('stdout', b'{"type":')
        capture.feed('stderr', b'"thread.started"}\n')
        capture.finish('stdout')
        capture.finish('stderr')
        self.assertEqual(seen, [])
        self.assertEqual([s['original_bytes'] for s in capture.streams()], [8, 18])
        self.assertIn('invalid_frame', capture.issues)

    def test_d3_wire_counts_utf8_lines_and_incomplete(self):
        for raw, lines in [(b'', 0), (b'\n', 1), (b'a\n', 1), (b'a\n\n', 2),
                           (b'a\r\nb', 2), ('\uac00\U0001f642\n'.encode() * 1001, 1001)]:
            with self.subTest(size=len(raw)):
                stream = self.capture(raw).streams()[0]
                self.assertEqual((stream['original_bytes'], stream['original_lines']),
                                 (len(raw), lines))
                self.assert_bounded(stream)
        capture = output.FanoutOutput()
        capture.feed('stdout', b'compiler failed\n')
        capture.finish('stdout', complete=False)
        self.assertIsNone(capture.streams()[0]['original_bytes'])
        self.assertIsNone(capture.streams()[0]['original_lines'])
        self.assertEqual(capture.streams()[1]['state'], 'not_captured')

    def test_d3_error_window_byte_bound_and_detachment(self):
        capture = self.capture(stderr=b'\xff' * 3000)
        self.assertLessEqual(len(capture.error_window('stderr').encode('utf-8')), 2000)
        for size in (2000, 2001):
            stream = self.capture(b'x' * size).streams()[0]
            self.assertEqual(stream['original_bytes'], size)
            self.assertEqual(stream['truncated'], size > 2000)
            self.assert_bounded(stream)
        streams = capture.streams()
        streams[0]['text'] = 'caller mutation'
        self.assertNotEqual(capture.streams()[0]['text'], streams[0]['text'])

    def test_d3_safe_line_limit_and_sanitized_digest(self):
        stream = self.capture(b'compiler failed\n' * 21).streams()[0]
        self.assertEqual(stream['state'], 'retained')
        self.assertEqual(stream['kept_lines'], 20)
        self.assertEqual(stream['original_lines'], 21)
        self.assertTrue(stream['truncated'])
        self.assertEqual(stream['truncation_reason'], 'line_limit')
        self.assert_bounded(stream)

    def test_d3_unterminated_frame_memory_and_recovery(self):
        seen: list[object] = []
        capture = output.FanoutOutput(protocol='codex', observer=lambda stream, event: seen.append(event['type']))
        tracemalloc.start()
        try:
            for _ in range(80):
                capture.feed('stdout', b'x' * 65536)
                self.assertLessEqual(capture.buffered_bytes, 2 * output.FRAME_LIMIT_BYTES +
                                     2 * output.FINAL_TEXT_LIMIT_BYTES + 20000)
            self.assertLess(tracemalloc.get_traced_memory()[1], 4 * 1024 * 1024)
        finally:
            tracemalloc.stop()
        capture.feed('stdout', b'\n{"type":"thread.started","thread_id":"id"}\n')
        capture.finish('stdout')
        self.assertEqual(seen, ['thread.started'])
        self.assertEqual(capture.streams()[0]['original_bytes'],
                         80 * 65536 + len(b'\n{"type":"thread.started","thread_id":"id"}\n'))
        self.assertIn('frame_limit', capture.issues)
        self.assertTrue(capture.streams()[0]['truncated'])

    def test_d4_adversarial_bodies_are_not_retained(self):
        payloads = [b'Authorization: Bearer abcdef123456', b'sk-abcdefgh12345678',
                    b'github_pat_' + b'A' * 32, b'AKIA' + b'A' * 16,
                    b'AIza' + b'A' * 24, b'-----BEGIN PRIVATE KEY-----',
                    b'PROMPT_SENTINEL_1423', b'def SOURCE_SENTINEL_1423(): pass',
                    b'{"content":"TRANSCRIPT_SENTINEL_1423"}',
                    b'compiler failed\n\x1b[2J', b'\x1b]52;c;U0VDUkVU\x07',
                    b'compiler failed\r', b'compiler failed\x00', b'compiler failed\x08',
                    '\u202eprivate'.encode(), '\u2066private'.encode()]
        for raw in payloads:
            with self.subTest(raw=raw):
                stream = self.capture(raw).streams()[0]
                self.assertIn(stream['state'], ['redacted', 'withheld'])
                self.assertTrue(stream['reason'])
                self.assertTrue(stream['text'])
                self.assertNotIn(raw.decode('utf-8'), json.dumps(stream, ensure_ascii=False))
                self.assertFalse(stream['truncated'])
                self.assert_bounded(stream)

    def test_d4_screen_after_retention_cap_and_split_secret(self):
        capture = output.FanoutOutput(known_secrets=('compiler failed',))
        for byte in b'compiler failed\n':
            capture.feed('stdout', bytes([byte]))
        capture.finish('stdout')
        self.assertEqual(capture.streams()[0]['state'], 'redacted')
        raw = b'compiler failed\n' * 140 + b'PRIVATE_PROMPT'
        stream = self.capture(raw, known_secrets=('PRIVATE_PROMPT',)).streams()[0]
        self.assertEqual(stream['state'], 'redacted')
        self.assertNotIn('compiler failed', stream['text'])
        self.assertTrue(stream['truncated'])
        unknown = self.capture(b'compiler failed\n' * 21 + b'unknown body').streams()[0]
        self.assertEqual(unknown['state'], 'withheld')

    def test_d4_protocol_is_never_diagnostic_and_success_is_absent(self):
        raw = b'{"type":"turn.failed","error":{"message":"compiler failed"}}\n'
        capture = self.capture(raw, protocol='codex')
        self.assertEqual(capture.streams()[0]['state'], 'withheld')
        self.assertNotIn('compiler failed', capture.streams()[0]['text'])
        self.assertIsNone(self.diagnostic(capture, failed=False, returncode=0))

    def test_d5_typed_exit_provenance_and_invalid_bindings(self):
        capture = self.capture()
        for code in (124, 127):
            result = self.diagnostic(capture, returncode=code)
            assert result is not None
            self.assertEqual((result['phase'], result['reason'], result['exit_code_source']),
                             ('worker', 'nonzero', 'process'))
        for phase, reason, code, source in [
                ('preflight', 'denial', None, 'not_observed'),
                ('worktree', 'internal_error', None, 'not_observed'),
                ('launch', 'missing_binary', 127, 'synthetic'),
                ('launch', 'spawn_error', 1, 'synthetic'),
                ('timeout', 'deadline', 124, 'synthetic'),
                ('unit_result', 'malformed_result', 0, 'process'),
                ('verification', 'nonzero', 7, 'process'),
                ('dispatcher', 'internal_error', None, 'not_observed')]:
            result = self.diagnostic(capture, phase=phase, reason=reason,
                                     returncode=code, exit_code_source=source)
            assert result is not None
            self.assertEqual((result['phase'], result['reason'], result['returncode'],
                              result['exit_code_source']), (phase, reason, code, source))
        for phase, reason, code, source, owner in [
                ('capacity', 'nonzero', 3, 'process', 'generic'),
                ('worker', 'quota', 3, 'process', 'generic'),
                ('worker', 'nonzero', True, 'process', 'generic'),
                ('worker', 'nonzero', None, 'process', 'generic'),
                ('worker', 'nonzero', 3, 'not_observed', 'generic'),
                ('worker', 'nonzero', 3, 'process', 'private\nbody')]:
            with self.subTest(phase=phase, reason=reason, code=code, source=source, owner=owner):
                with self.assertRaises(ValueError):
                    _ = self.diagnostic(capture, phase=phase, reason=reason, returncode=code,
                                    exit_code_source=source, owner=owner)

    def test_d6_invalid_utf8_split_valid_utf8_and_controls(self):
        for raw in [b'\xff\xfe\x80err\n', b'\xf0\x9f', b'good\n\xff']:
            stream = self.capture(raw).streams()[0]
            self.assertEqual(stream['state'], 'withheld')
            self.assertEqual(stream['reason'], 'invalid_utf8')
            self.assertEqual(stream['original_bytes'], len(raw))
        capture = output.FanoutOutput()
        for byte in '\uac00\U0001f642'.encode():
            capture.feed('stdout', bytes([byte]))
        capture.finish('stdout')
        self.assertNotEqual(capture.streams()[0]['reason'], 'invalid_utf8')
        self.assertEqual(capture.streams()[0]['original_bytes'], 7)
        with self.assertRaises(TypeError):
            capture.feed('stderr', '\ud800')

    def test_d6_json_depth_duplicates_malformed_and_event_limit(self):
        seen: list[object] = []
        capture = output.FanoutOutput(protocol='codex', observer=lambda stream, event: seen.append(event['type']))
        malformed = [b'{broken}\n', b'{"type":"a","type":"b"}\n',
                     b'{"type":"a","x":NaN}\n',
                     ('{"type":"deep","x":' + '[' * 16 + '0' + ']' * 16 + '}\n').encode()]
        for raw in malformed:
            capture.feed('stdout', raw)
        capture.feed('stdout', b'{"type":"valid"}\n' * (output.EVENT_LIMIT + 1))
        capture.finish('stdout')
        self.assertEqual(len(seen), output.EVENT_LIMIT - len(malformed))
        self.assertEqual(set(seen), {'valid'})
        self.assertIn('event_limit', capture.issues)
        self.assertIn('invalid_frame', capture.issues)
        self.assertIn('depth_limit', capture.issues)

    def test_d6_optional_reader_current_attempt_and_revalidation(self):
        module = diag_api
        original = self.diagnostic(self.capture(stderr=b'compiler failed\n'))
        assert original is not None
        binding = dict(fanout_id='fanout-1', unit_id='unit-a', attempt_id='attempt-1')
        read = module.read_failure_diagnostic
        self.assertEqual(read(original, **binding), original)
        for value in [None, {}, dict(original, schema_version='future/v2'),
                      dict(original, attempt_id='stale'), dict(original, unit_id='foreign')]:
            self.assertIsNone(read(value, **binding))
        bad = copy.deepcopy(original)
        bad['streams'][1]['text'] = 'PRIVATE_BODY'
        self.assertIsNone(read(bad, **binding))
        bad = copy.deepcopy(original)
        bad['streams'][1]['digest'] = '0' * 64
        self.assertIsNone(read(bad, **binding))
        self.assertIsNone(read(dict(original, spill_ref='/private/raw'), **binding))
        self.assertEqual(original['attempt_id'], 'attempt-1')

    def test_d6_reader_rejects_controls_and_impossible_counters(self):
        module = diag_api
        original = self.diagnostic(self.capture(stderr=b'compiler failed\n'))
        assert original is not None
        binding = dict(fanout_id='fanout-1', unit_id='unit-a', attempt_id='attempt-1')
        mutations: list[dict[str, object]] = [dict(text='compiler failed\r', kept_bytes=16, kept_lines=1,
                          digest=hashlib.sha256(b'compiler failed\r').hexdigest()),
                     dict(original_bytes=0, original_lines=0),
                     dict(truncated=False, truncation_reason=None, original_bytes=3000),
                     dict(reason=[]), dict(state={}), dict(original_bytes=True)]
        for mutation in mutations:
            changed_streams = [dict(stream) for stream in original['streams']]
            changed_streams[1].update(mutation)
            bad: dict[str, object] = dict(original, streams=changed_streams)
            with self.subTest(mutation=mutation):
                try:
                    result = module.read_failure_diagnostic(bad, **binding)
                except TypeError:
                    self.fail('optional malformed diagnostic must be unavailable, not raise')
                self.assertIsNone(result)
        for field in ('phase', 'reason', 'streams'):
            bad = dict(original)
            bad[field] = [] if field != 'streams' else [{}]
            with self.subTest(field=field):
                try:
                    result = module.read_failure_diagnostic(bad, **binding)
                except TypeError:
                    self.fail('optional malformed diagnostic must be unavailable, not raise')
                self.assertIsNone(result)

    def test_d7_lost_native_frames_invalidate_stale_final_text(self):
        valid = b'{"type":"item.completed","item":{"type":"agent_message","text":"old"}}\n'
        for tail in (b'x' * (output.FRAME_LIMIT_BYTES + 1) + b'\n',
                     b'{broken}\n', b'{"type":"item.completed","item":{"type":"agent_message","text":null}}\n'):
            capture = self.capture(valid + tail, protocol='codex')
            with self.subTest(size=len(tail)):
                self.assertEqual(capture.take_final_text(), '')
                self.assertTrue(capture.issues)
        capture = output.FanoutOutput(protocol='codex')
        capture.feed('stdout', b'{"type":"other"}\n' * (output.EVENT_LIMIT - 1))
        capture.feed('stdout', valid)
        capture.feed('stdout', b'{"type":"other"}\n')
        capture.finish('stdout')
        self.assertEqual(capture.take_final_text(), '')

    def test_d7_plain_last_complete_fence_characterization(self):
        from omh.coding.fanout_dispatch import stdout_fenced_json_blocks as _stdout_fenced_json_blocks
        raw = 'noise\n```json\n{"v":1}\n```\n```json\n{"v":2}\n```\n```json\nunfinished'
        self.assertEqual(_stdout_fenced_json_blocks(raw), ['{"v":1}', '{"v":2}'])

    def test_d7_plain_fallback_is_bounded_and_consumed(self):
        from omh.coding.fanout_dispatch import stdout_fenced_json_blocks as _stdout_fenced_json_blocks
        raw = b'noise\n```json\n{"v":1}\n```\n```json\n{"v":2}\n```\n```json\nunfinished'
        capture = self.capture(raw)
        self.assertEqual(_stdout_fenced_json_blocks(capture.take_final_text()), ['{"v":2}'])
        self.assertEqual(capture.take_final_text(), '')
        capture = self.capture(b'```json\n' + b'x' * 262145 + b'\n```\n')
        self.assertEqual(capture.take_final_text(), '')
        self.assertIn('final_text_limit', capture.issues)

    def test_d7_native_final_precedence_root_only_and_usage(self):
        for protocol, events, expected, usage in [
            ('codex', [
                {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'first'}},
                {'type': 'item.completed', 'item': {'type': 'tool', 'text': 'PRIVATE_TOOL'}},
                {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'last'}},
                {'type': 'turn.completed', 'usage': {'input_tokens': 4, 'output_tokens': 2,
                                                    'cached_input_tokens': 1, 'private': 99}}],
             'last', {'input_tokens': 4, 'output_tokens': 2, 'cached_input_tokens': 1}),
            ('claude', [
                {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'assistant'}]}},
                {'type': 'result', 'result': 'root', 'usage': {'input_tokens': 3, 'output_tokens': 5}},
                {'type': 'result', 'parent_tool_use_id': 'tool', 'result': 'PRIVATE_CHILD'},
                {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'later'}]}}],
             'root', {'input_tokens': 3, 'output_tokens': 5})]:
            capture = output.FanoutOutput(protocol=protocol)
            for event in events:
                capture.feed('stdout', json.dumps(event).encode() + b'\n')
            capture.finish('stdout')
            self.assertEqual(capture.take_final_text(), expected)
            self.assertEqual(capture.usage, usage)
            self.assertEqual(capture.take_final_text(), '')

    def test_d7_final_text_overrun_does_not_borrow_earlier_result(self):
        for protocol, events in [
            ('codex', [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}}
                       for text in ['old', 'x' * 262145]]),
            ('claude', [{'type': 'result', 'result': text} for text in ['old', 'x' * 262145]])]:
            capture = self.capture(b''.join(json.dumps(e).encode() + b'\n' for e in events), protocol=protocol)
            self.assertEqual(capture.take_final_text(), '')
            self.assertIn('final_text_limit', capture.issues)

    def test_d7_observer_is_ephemeral_and_errors_propagate(self):
        def observer(stream: diag_api.StreamName, event: dict[str, object]) -> None:
            self.assertEqual(stream, 'stdout')
            event.clear()
        capture = output.FanoutOutput(protocol='codex', observer=observer)
        capture.feed('stdout', b'{"type":"item.completed","item":{"type":"agent_message","text":"final"}}\n')
        self.assertEqual(capture.take_final_text(), 'final')
        def fail(_stream: diag_api.StreamName, _event: dict[str, object]) -> None:
            raise RuntimeError('observer_failure')
        capture = output.FanoutOutput(protocol='codex', observer=fail)
        with self.assertRaisesRegex(RuntimeError, '^observer_failure$'):
            capture.feed('stdout', b'{"type":"thread.started"}\n')

    def test_d7_qa_does_not_claim_surface_acceptance(self):
        spec = importlib.util.find_spec('five_issue_cases.diagnostics')
        self.assertIsNotNone(spec, 'missing diagnostics foundation QA adapter')
        from five_issue_cases.diagnostics import run_case
        for number in range(1, 8):
            result = run_case('D' + str(number))
            self.assertFalse(result['pass'])
            self.assertEqual(result['provenance']['scope'], 'foundation')
            self.assertTrue(result['blocked_reason'])
            self.assertTrue(result['cleanup']['verified_absent'])


if __name__ == '__main__':
    _ = unittest.main()
