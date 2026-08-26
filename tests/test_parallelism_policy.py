"""Parallelism policy: OMO-shaped defaults, profile overrides, clamps.

The owner asked for OMO's execution-concurrency defaults (per-lane 5,
global 8) installed by setup and editable in the profile. These tests pin
the defaults, the validated-override-with-disclosure read (an invalid
stored value falls back and is named, never silently wins), and the
flag-vs-policy resolution the fanout dispatch command runs.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from omh.coding.parallelism_policy import (
    build_parallelism_policy,
    read_parallelism_policy,
    resolve_fanout_concurrency,
)
from omh.profiles.setup import (
    PARALLELISM_DEFAULTS,
    build_setup_profile,
    write_setup_profile,
)
from omh.system.local_store import atomic_write_json
from omh.system.paths import OmhPaths


def _paths(tmp: str) -> OmhPaths:
    root = Path(tmp)
    return OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")


class ParallelismDefaultsTests(unittest.TestCase):
    def test_defaults_mirror_the_omo_engine_bounds(self) -> None:
        self.assertEqual(PARALLELISM_DEFAULTS["default_concurrency"], 5)
        self.assertEqual(PARALLELISM_DEFAULTS["global_concurrency"], 8)
        self.assertEqual(PARALLELISM_DEFAULTS["lane_budget_default"], 5)
        policy = build_parallelism_policy()
        self.assertEqual(policy["default_concurrency"], 5)
        self.assertEqual(policy["global_concurrency"], 8)
        self.assertEqual(policy["per_owner"], {})
        self.assertEqual(policy["ignored_keys"], [])

    def test_a_fresh_setup_profile_writes_the_editable_block(self) -> None:
        profile = build_setup_profile()
        block = profile["parallelism"]
        self.assertEqual(block["schema_version"], "parallelism_policy/v1")
        self.assertEqual(block["default_concurrency"], 5)
        self.assertEqual(block["global_concurrency"], 8)
        self.assertEqual(block["per_owner"], {})

    def test_a_missing_profile_or_block_reads_as_the_defaults(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(tmp)
            self.assertEqual(read_parallelism_policy(paths)["default_concurrency"], 5)
            write_setup_profile(paths)
            policy = read_parallelism_policy(paths)
            self.assertEqual(policy["default_concurrency"], 5)
            self.assertEqual(policy["global_concurrency"], 8)


class ParallelismOverrideTests(unittest.TestCase):
    def _write_profile(self, paths: OmhPaths, parallelism: object) -> None:
        profile = build_setup_profile()
        profile["parallelism"] = parallelism
        paths.setup_profile_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(paths.setup_profile_path, profile, private=True)

    def test_stored_overrides_apply(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(tmp)
            self._write_profile(
                paths,
                {
                    "default_concurrency": 3,
                    "global_concurrency": 12,
                    "lane_budget_default": 4,
                    "per_owner": {"codex": 2, "claude-code": 3},
                },
            )
            policy = read_parallelism_policy(paths)
            self.assertEqual(policy["default_concurrency"], 3)
            self.assertEqual(policy["global_concurrency"], 12)
            self.assertEqual(policy["lane_budget_default"], 4)
            self.assertEqual(policy["per_owner"], {"codex": 2, "claude-code": 3})
            self.assertEqual(policy["ignored_keys"], [])

    def test_invalid_values_fall_back_and_are_disclosed(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(tmp)
            self._write_profile(
                paths,
                {
                    "default_concurrency": True,
                    "global_concurrency": 0,
                    "lane_budget_default": 500,
                    "per_owner": {"codex": 0, "": 2, "claude-code": 2},
                },
            )
            policy = read_parallelism_policy(paths)
            self.assertEqual(policy["default_concurrency"], 5)
            self.assertEqual(policy["global_concurrency"], 8)
            self.assertEqual(policy["lane_budget_default"], 5)
            self.assertEqual(policy["per_owner"], {"claude-code": 2})
            for key in ("default_concurrency", "global_concurrency", "lane_budget_default"):
                self.assertIn(key, policy["ignored_keys"])
            self.assertTrue(any(item.startswith("per_owner.") for item in policy["ignored_keys"]))

    def test_a_non_mapping_block_reads_as_defaults(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(tmp)
            self._write_profile(paths, "everything, please")
            policy = read_parallelism_policy(paths)
            self.assertEqual(policy["default_concurrency"], 5)
            self.assertEqual(policy["ignored_keys"], [])

    def test_default_above_global_is_clamped_and_disclosed(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(tmp)
            self._write_profile(paths, {"default_concurrency": 10, "global_concurrency": 4})
            policy = read_parallelism_policy(paths)
            self.assertEqual(policy["default_concurrency"], 4)
            self.assertEqual(policy["default_concurrency_clamped_from"], 10)


class FanoutConcurrencyResolutionTests(unittest.TestCase):
    def test_no_flag_uses_the_policy_default(self) -> None:
        resolution = resolve_fanout_concurrency(build_parallelism_policy(), None)
        self.assertEqual(resolution["applied"], 5)
        self.assertEqual(resolution["source"], "policy_default")
        self.assertFalse(resolution["clamped"])

    def test_an_explicit_flag_wins_within_the_ceiling(self) -> None:
        resolution = resolve_fanout_concurrency(build_parallelism_policy(), 3)
        self.assertEqual(resolution["applied"], 3)
        self.assertEqual(resolution["source"], "cli_flag")

    def test_a_flag_above_the_ceiling_is_clamped_and_disclosed(self) -> None:
        resolution = resolve_fanout_concurrency(build_parallelism_policy(), 20)
        self.assertEqual(resolution["applied"], 8)
        self.assertTrue(resolution["clamped"])
        self.assertEqual(resolution["requested"], 20)

    def test_a_sub_one_flag_floors_at_one(self) -> None:
        resolution = resolve_fanout_concurrency(build_parallelism_policy(), 0)
        self.assertEqual(resolution["applied"], 1)

    def test_the_readers_disclosures_ride_into_the_resolution(self) -> None:
        # The dispatch record is the surface an operator reads after the
        # fact; a computed-then-discarded disclosure is no disclosure.
        policy = build_parallelism_policy()
        policy["ignored_keys"] = ["global_concurrency"]
        resolution = resolve_fanout_concurrency(policy, None)
        self.assertEqual(resolution["ignored_keys"], ["global_concurrency"])

    def test_a_clamped_policy_default_reports_clamped_with_its_origin(self) -> None:
        policy = build_parallelism_policy()
        policy["default_concurrency"] = 4
        policy["global_concurrency"] = 4
        policy["default_concurrency_clamped_from"] = 10
        resolution = resolve_fanout_concurrency(policy, None)
        self.assertTrue(resolution["clamped"])
        self.assertEqual(resolution["policy_clamped_from"], 10)
        self.assertEqual(resolution["applied"], 4)
        # An explicit flag is the operator's own number; the reader's clamp
        # note does not apply to it.
        flagged = resolve_fanout_concurrency(policy, 3)
        self.assertFalse(flagged["clamped"])


class CliWiringTests(unittest.TestCase):
    def test_parser_default_is_none_and_dispatch_receives_the_policy_width(self) -> None:
        # The headline behavior of this change is the CLI moving from a
        # hardcoded default of 2 to the profile-resolved width; without this
        # test, reverting the parser default breaks nothing.
        import contextlib
        import io
        import subprocess
        from unittest.mock import patch

        from omh.coding.fanout import build_fanout_contract
        from omh.coding.fanout_artifacts import write_fanout_contract
        from omh.commands.main import build_parser

        goal_text = "split the sample feature across agents"
        units = [
            {"unit_id": "core", "title": "Core work", "owner": "codex", "file_scope": ["src/core/"]},
            {"unit_id": "docs", "title": "Docs work", "owner": "claude-code", "file_scope": ["docs/"]},
        ]
        parser = build_parser()
        with TemporaryDirectory() as tmp:
            paths = _paths(tmp)
            contract = write_fanout_contract(paths, build_fanout_contract(goal_text, units))
            goal = Path(tmp) / "goal.txt"
            goal.write_text(goal_text, encoding="utf-8")
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
            (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "seed.txt"], cwd=str(repo), check=True)
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
                cwd=str(repo),
                check=True,
            )
            args = parser.parse_args(
                [
                    "--omh-home",
                    str(paths.omh_home),
                    "--hermes-home",
                    str(paths.hermes_home),
                    "coding",
                    "fanout",
                    "dispatch",
                    contract["fanout_id"],
                    "--goal-file",
                    str(goal),
                    "--repo-root",
                    str(repo),
                ]
            )
            self.assertIsNone(args.concurrency)
            captured: dict[str, object] = {}

            def _capture(*call_args, **kwargs):
                captured.update(kwargs)
                return {"schema_version": "fanout_dispatch_summary/v1", "units": []}

            with patch("omh.coding.fanout_dispatch.dispatch_fanout", new=_capture):
                with contextlib.redirect_stdout(io.StringIO()):
                    args.func(args)
            self.assertEqual(captured["concurrency"], 5)
            self.assertEqual(captured["per_owner_lanes"], {})
            self.assertEqual(captured["concurrency_policy"]["source"], "policy_default")


if __name__ == "__main__":
    unittest.main()
