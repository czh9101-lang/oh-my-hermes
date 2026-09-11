"""A help page that outgrows the probe budget must not switch the lane off.

`negotiate_session_capability` reads a CLI's `--help` to see whether the
structured-session flags exist. The read was capped at the same 16 KiB every
other child capture uses, and a truncated read was treated as an unanswerable
probe -- so when `claude --help` grew past the cap (21,401 bytes observed
2026-09-11, with `--verbose` at byte 17,991), the negotiation returned no
protocol, the dispatch spawned without `--output-format stream-json`, and the
unit produced no token counts and no session id for the rest of its life. The
only trace was an empty token column, which reads like a unit that spent
nothing rather than like a capability that was never negotiated.

These pin the three parts of the repair: the budget is document-sized, a
truncated read still answers the question when every flag was found in what
WAS read, and an absent protocol states why.
"""

from __future__ import annotations

import sys
import unittest

from _local_package import load_local_package

load_local_package()

from omh.coding.executor_readiness import negotiate_session_capability
from omh.coding.fanout_executor_sessions import (
    SESSION_HELP_PROBE_BYTES,
    bounded_session_probe,
)
from omh.coding._hermes_child_process import MAX_CAPTURE_BYTES


class HelpProbeBudgetTests(unittest.TestCase):
    def test_the_help_budget_clears_a_help_page_that_outgrew_the_shared_cap(self) -> None:
        # 21,401 bytes was the observed page; the budget is not set to clear it
        # by a hair, because the next release grows it again.
        self.assertGreater(SESSION_HELP_PROBE_BYTES, MAX_CAPTURE_BYTES)
        self.assertGreater(SESSION_HELP_PROBE_BYTES, 21_401 * 4)

    def test_a_larger_budget_reads_output_the_default_would_truncate(self) -> None:
        program = 'print("x" * 20000)'
        data, reason = bounded_session_probe([sys.executable, "-c", program])
        self.assertIsNone(data)
        self.assertEqual(reason, "probe_output_limited")
        data, reason = bounded_session_probe(
            [sys.executable, "-c", program], limit_bytes=SESSION_HELP_PROBE_BYTES
        )
        self.assertEqual(reason, "observed")
        self.assertEqual(len(data or b""), 20_001)

    def test_keep_partial_hands_back_what_was_read_without_calling_it_complete(self) -> None:
        # The reason still says the read was cut off: the caller decides what a
        # prefix can answer, the probe never upgrades it to `observed`.
        data, reason = bounded_session_probe(
            [sys.executable, "-c", 'print("y" * 20000)'], keep_partial=True
        )
        self.assertEqual(reason, "probe_output_limited")
        self.assertEqual(len(data or b""), MAX_CAPTURE_BYTES)

    def test_every_other_caller_still_gets_nothing_from_a_truncated_read(self) -> None:
        data, reason = bounded_session_probe([sys.executable, "-c", 'print("z" * 20000)'])
        self.assertIsNone(data)
        self.assertEqual(reason, "probe_output_limited")


def _fake_cli(version: str, help_text: str) -> str:
    """A python one-liner that answers --version and --help like a CLI."""
    return (
        "import sys;"
        f"v={version!r};h={help_text!r};"
        "sys.stdout.write(v if '--version' in sys.argv else h)"
    )


class NegotiationOutcomeTests(unittest.TestCase):
    def _negotiate(self, version: str, help_text: str):
        import os
        import tempfile
        from pathlib import Path

        directory = tempfile.mkdtemp()
        script = Path(directory) / "fake-cli"
        script.write_text(
            "#!/bin/sh\nexec " + sys.executable + ' -c "$OMH_FAKE_CLI_PROGRAM" "$@"\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
        env = dict(os.environ, OMH_FAKE_CLI_PROGRAM=_fake_cli(version, help_text))
        return negotiate_session_capability("claude-code", str(script), env=env)

    def test_a_help_page_past_the_old_cap_still_negotiates_the_protocol(self) -> None:
        flags = "--output-format stream-json --resume"
        # `--verbose` past 16 KiB is the exact shape that switched the lane off.
        help_text = flags + "\n" + ("filler line\n" * 2_000) + "--verbose\n"
        self.assertGreater(help_text.find("--verbose"), MAX_CAPTURE_BYTES)
        capability = self._negotiate("2.1.268 (Claude Code)", help_text)
        self.assertEqual(capability.protocol, "claude_stream_json")
        self.assertEqual(capability.reason, "")

    def test_a_genuinely_absent_flag_refuses_and_names_itself(self) -> None:
        capability = self._negotiate(
            "2.1.268 (Claude Code)", "--output-format stream-json --resume\n"
        )
        self.assertIsNone(capability.protocol)
        self.assertIn("flags_absent", capability.reason)
        self.assertIn("--verbose", capability.reason)
        self.assertIn("help_complete", capability.reason)

    def test_an_unrecognized_version_refuses_and_says_so(self) -> None:
        capability = self._negotiate(
            "not-a-version", "--output-format stream-json --verbose --resume\n"
        )
        self.assertIsNone(capability.protocol)
        self.assertEqual(capability.reason, "version_output_unrecognized")


if __name__ == "__main__":
    unittest.main()
