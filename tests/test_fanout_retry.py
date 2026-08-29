"""Retry taxonomy, replay-safety predicate, and the backoff ladder.

The three properties these hold, in the order the policy decides them:

* A transient transport failure is retryable; a real non-zero exit from a test
  or verification run is terminal and is NEVER retried. The negative case is
  the point of the whole classifier -- retrying a genuine failure burns a full
  agent-CLI run to re-derive an answer already in hand.
* A unit may only be replayed when nothing observed it doing anything. Files in
  its worktree or a result artifact both block a replay, and an unmeasurable
  worktree blocks it too: "I could not tell" must fail closed.
* The delay is `min(base * 2^(attempt-1), cap)` scaled by 75-100% jitter, with
  the random source injected so every bound is asserted exactly and no test
  sleeps.
"""

from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.coding.fanout_retry import (  # noqa: E402
    CLASS_EXECUTOR_MISSING,
    CLASS_PROVIDER_LIMIT,
    CLASS_TERMINAL_FAILURE,
    CLASS_TRANSPORT,
    CLASS_UNIT_TIMEOUT,
    FANOUT_MAX_RETRIES,
    REPLAY_SAFE,
    REPLAY_UNSAFE_SIDE_EFFECTS,
    REPLAY_UNSAFE_UNMEASURED,
    RETRY_BASE_DELAY_SECONDS,
    RETRY_JITTER_FLOOR,
    RETRY_MAX_DELAY_SECONDS,
    classify_unit_failure,
    evaluate_unit_retry,
    replay_safety,
    retry_delay_seconds,
)


def _classify(exit_code: int = 1, out: str = "", err: str = "", limit: str = ""):
    return classify_unit_failure(
        exit_code=exit_code, output_tail=out, stderr_tail=err, limit_shaped=limit
    )


class FailureTaxonomyTests(unittest.TestCase):
    def test_transport_shapes_are_retryable(self) -> None:
        for text in (
            "openai: http 503 service unavailable",
            "Error: socket hang up",
            "read ECONNRESET",
            "connection reset by peer",
            "upstream connect error or disconnect/reset before headers",
            "TypeError: fetch failed",
            "status 502 bad gateway",
            "status 504 gateway timeout",
            '{"type":"overloaded_error"}',
            "internal server error",
            "connection refused",
        ):
            verdict = _classify(err=text)
            self.assertTrue(verdict["retryable"], text)
            self.assertEqual(verdict["failure_class"], CLASS_TRANSPORT, text)
            self.assertTrue(verdict["failure_label"], text)

    def test_a_limit_shaped_failure_is_retryable_under_its_own_class(self) -> None:
        # The dispatcher already derives this label for the provider-limit
        # evidence record; the retry verdict reads that same label rather than
        # re-matching, so the two surfaces cannot drift apart.
        verdict = _classify(err="you have hit your rate limit", limit="rate_limit")
        self.assertTrue(verdict["retryable"])
        self.assertEqual(verdict["failure_class"], CLASS_PROVIDER_LIMIT)
        self.assertEqual(verdict["failure_label"], "rate_limit")

    def test_a_real_test_failure_is_terminal(self) -> None:
        # THE negative case. None of these may ever be retried, however
        # transient-looking a word in them is.
        for text in (
            "FAILED (failures=3, errors=1)",
            "AssertionError: 2 != 3",
            "ruff: 4 errors found",
            "error: could not compile the crate",
            "ERROR: line 500 of module foo",
            "reset the branch and try again",
        ):
            verdict = _classify(err=text)
            self.assertFalse(verdict["retryable"], text)
            self.assertEqual(verdict["failure_class"], CLASS_TERMINAL_FAILURE, text)

    def test_a_missing_executable_and_a_timeout_get_their_own_terminal_classes(self) -> None:
        self.assertEqual(
            _classify(exit_code=127, out="codex not found on PATH")["failure_class"],
            CLASS_EXECUTOR_MISSING,
        )
        self.assertFalse(_classify(exit_code=127)["retryable"])
        # A unit that burned its whole timeout is not a transport blip, and
        # a transport word in its tail must not make it one.
        timed_out = _classify(exit_code=124, out="unit timed out after 30s; socket hang up")
        self.assertEqual(timed_out["failure_class"], CLASS_UNIT_TIMEOUT)
        self.assertFalse(timed_out["retryable"])

    def test_a_successful_exit_classifies_as_nothing(self) -> None:
        verdict = _classify(exit_code=0, err="socket hang up")
        self.assertFalse(verdict["retryable"])
        self.assertEqual(verdict["failure_class"], "")


class ReplaySafetyPredicateTests(unittest.TestCase):
    def test_a_unit_that_changed_nothing_is_replay_safe(self) -> None:
        verdict = replay_safety({"outcome": "no_changes"})
        self.assertTrue(verdict["replay_safe"])
        self.assertEqual(verdict["replay_verdict"], REPLAY_SAFE)

    def test_observed_worktree_changes_block_a_replay(self) -> None:
        verdict = replay_safety({"outcome": "recovery_available", "paths_changed": 2})
        self.assertFalse(verdict["replay_safe"])
        self.assertEqual(verdict["replay_verdict"], REPLAY_UNSAFE_SIDE_EFFECTS)
        self.assertEqual(verdict["side_effect"], "worktree_changes")

    def test_a_result_artifact_blocks_a_replay_even_on_a_clean_worktree(self) -> None:
        verdict = replay_safety({"outcome": "no_changes"}, artifact_observed=True)
        self.assertFalse(verdict["replay_safe"])
        self.assertEqual(verdict["side_effect"], "unit_result_artifact")

    def test_an_unmeasurable_worktree_fails_closed(self) -> None:
        # "The unit left nothing" and "I could not tell" lead an operator to
        # opposite actions; only the first may be replayed.
        for recovery in (None, {}, {"outcome": "capture_failed", "reason": "git refused"}):
            verdict = replay_safety(recovery)
            self.assertFalse(verdict["replay_safe"], recovery)
            self.assertEqual(verdict["replay_verdict"], REPLAY_UNSAFE_UNMEASURED, recovery)


class BackoffTests(unittest.TestCase):
    def test_the_ladder_doubles_and_the_floor_is_the_jitter_floor(self) -> None:
        floor = retry_delay_seconds(1, rng=lambda: 0.0)
        self.assertAlmostEqual(floor, RETRY_BASE_DELAY_SECONDS * RETRY_JITTER_FLOOR)
        self.assertAlmostEqual(
            retry_delay_seconds(2, rng=lambda: 0.0),
            RETRY_BASE_DELAY_SECONDS * 2 * RETRY_JITTER_FLOOR,
        )
        self.assertAlmostEqual(
            retry_delay_seconds(3, rng=lambda: 0.0),
            RETRY_BASE_DELAY_SECONDS * 4 * RETRY_JITTER_FLOOR,
        )

    def test_full_jitter_is_the_undiscounted_delay(self) -> None:
        self.assertAlmostEqual(retry_delay_seconds(1, rng=lambda: 1.0), RETRY_BASE_DELAY_SECONDS)

    def test_every_delay_stays_inside_75_to_100_percent_of_its_step(self) -> None:
        import random as _random

        rng = _random.Random(20260830).random
        for attempt in range(1, 9):
            step = min(RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)), RETRY_MAX_DELAY_SECONDS)
            for _ in range(50):
                delay = retry_delay_seconds(attempt, rng=rng)
                self.assertGreaterEqual(delay, step * RETRY_JITTER_FLOOR - 1e-6)
                self.assertLessEqual(delay, step + 1e-6)

    def test_the_cap_bounds_the_ladder(self) -> None:
        self.assertAlmostEqual(retry_delay_seconds(50, rng=lambda: 1.0), RETRY_MAX_DELAY_SECONDS)

    def test_an_out_of_range_random_source_cannot_escape_the_bounds(self) -> None:
        # Defensive, not decorative: a caller injecting a bad rng in a test
        # must not silently produce a negative or oversized sleep.
        self.assertAlmostEqual(
            retry_delay_seconds(1, rng=lambda: -5.0), RETRY_BASE_DELAY_SECONDS * RETRY_JITTER_FLOOR
        )
        self.assertAlmostEqual(retry_delay_seconds(1, rng=lambda: 5.0), RETRY_BASE_DELAY_SECONDS)


class RetryDecisionTests(unittest.TestCase):
    def _decide(self, **overrides):
        kwargs = {
            "attempt": 1,
            "exit_code": 1,
            "output_tail": "",
            "stderr_tail": "socket hang up",
            "recovery": {"outcome": "no_changes"},
            "rng": lambda: 0.0,
        }
        kwargs.update(overrides)
        return evaluate_unit_retry(**kwargs)

    def test_transient_plus_clean_worktree_retries_with_a_delay(self) -> None:
        decision = self._decide()
        self.assertTrue(decision["retry"])
        self.assertEqual(decision["decision"], "retrying")
        self.assertEqual(decision["replay_verdict"], REPLAY_SAFE)
        self.assertAlmostEqual(
            decision["delay_seconds"], RETRY_BASE_DELAY_SECONDS * RETRY_JITTER_FLOOR
        )

    def test_transient_plus_side_effects_is_surfaced_not_retried(self) -> None:
        decision = self._decide(recovery={"outcome": "recovery_available"})
        self.assertFalse(decision["retry"])
        self.assertEqual(decision["decision"], "surfaced_for_continuation")
        self.assertEqual(decision["replay_verdict"], REPLAY_UNSAFE_SIDE_EFFECTS)

    def test_a_terminal_failure_never_reaches_the_replay_predicate(self) -> None:
        decision = self._decide(stderr_tail="FAILED (failures=1)")
        self.assertFalse(decision["retry"])
        self.assertEqual(decision["decision"], "terminal")
        # No replay verdict at all: the predicate is not consulted, so the
        # record cannot imply the worktree was measured.
        self.assertNotIn("replay_verdict", decision)

    def test_attempts_are_bounded(self) -> None:
        exhausted = self._decide(attempt=FANOUT_MAX_RETRIES + 1)
        self.assertFalse(exhausted["retry"])
        self.assertEqual(exhausted["decision"], "retries_exhausted")
        self.assertEqual(exhausted["max_retries"], FANOUT_MAX_RETRIES)
        self.assertTrue(self._decide(attempt=FANOUT_MAX_RETRIES)["retry"])


if __name__ == "__main__":
    unittest.main()
