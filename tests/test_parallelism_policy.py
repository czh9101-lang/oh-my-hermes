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

from omh.coding.fanout_retry import FANOUT_MAX_RETRIES
from omh.coding.parallelism_policy import (
    FANOUT_MAX_DEPTH_DEFAULT,
    FANOUT_RUN_SPAWN_CEILING_DEFAULT,
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


class SpawnGuardPolicyTests(unittest.TestCase):
    """`max_depth` and `run_spawn_ceiling` read like every other tunable here.

    They bound the same one subprocess exception the widths do, from a
    different direction, so they get the same validated-override-plus-
    disclosure treatment rather than a private constant nobody can edit.
    """

    def _write_profile(self, paths: OmhPaths, parallelism: object) -> None:
        profile = build_setup_profile()
        profile["parallelism"] = parallelism
        paths.setup_profile_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(paths.setup_profile_path, profile, private=True)

    def test_defaults_match_the_omo_spawn_guard(self) -> None:
        self.assertEqual(FANOUT_MAX_DEPTH_DEFAULT, 1)
        self.assertEqual(FANOUT_RUN_SPAWN_CEILING_DEFAULT, 60)
        policy = build_parallelism_policy()
        self.assertEqual(policy["max_depth"], 1)
        self.assertEqual(policy["run_spawn_ceiling"], 60)

    def test_stored_overrides_apply(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(tmp)
            self._write_profile(paths, {"max_depth": 2, "run_spawn_ceiling": 12})
            policy = read_parallelism_policy(paths)
            self.assertEqual(policy["max_depth"], 2)
            self.assertEqual(policy["run_spawn_ceiling"], 12)
            self.assertEqual(policy["ignored_keys"], [])

    def test_out_of_range_values_fall_back_and_are_disclosed(self) -> None:
        # A profile that asks for unbounded nesting or a five-figure spawn
        # budget is a typo, not a policy; it falls back and says so.
        with TemporaryDirectory() as tmp:
            paths = _paths(tmp)
            self._write_profile(paths, {"max_depth": 99, "run_spawn_ceiling": 0})
            policy = read_parallelism_policy(paths)
            self.assertEqual(policy["max_depth"], 1)
            self.assertEqual(policy["run_spawn_ceiling"], 60)
            self.assertIn("max_depth", policy["ignored_keys"])
            self.assertIn("run_spawn_ceiling", policy["ignored_keys"])

    def test_a_pool_wider_than_the_whole_budget_is_clamped_and_disclosed(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(tmp)
            self._write_profile(paths, {"run_spawn_ceiling": 3})
            policy = read_parallelism_policy(paths)
            self.assertEqual(policy["global_concurrency"], 3)
            self.assertEqual(policy["global_concurrency_clamped_from"], 8)
            # The per-lane clamp reads the width the pool will actually run
            # with, so the cascade lands on the same number.
            self.assertEqual(policy["default_concurrency"], 3)
            self.assertEqual(policy["default_concurrency_clamped_from"], 5)

    def test_the_resolution_carries_the_guard_into_the_dispatch_record(self) -> None:
        policy = build_parallelism_policy()
        policy["max_depth"] = 2
        policy["run_spawn_ceiling"] = 9
        policy["global_concurrency_clamped_from"] = 8
        resolution = resolve_fanout_concurrency(policy, None)
        self.assertEqual(resolution["max_depth"], 2)
        self.assertEqual(resolution["run_spawn_ceiling"], 9)
        self.assertEqual(resolution["global_concurrency_clamped_from"], 8)


class SecurityPostureTests(unittest.TestCase):
    """`OMH_SECURITY=strict` tightens the fanout pool, spawn guard, and retry
    ceiling together (`security_posture.POSTURE_MAPPING`); `default` (unset)
    reads byte-for-byte the same numbers this module's own constants name.
    """

    def test_default_posture_matches_todays_constants_exactly(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(tmp)
            policy = read_parallelism_policy(paths, env={})
            self.assertEqual(policy["default_concurrency"], PARALLELISM_DEFAULTS["default_concurrency"])
            self.assertEqual(policy["global_concurrency"], PARALLELISM_DEFAULTS["global_concurrency"])
            self.assertEqual(policy["lane_budget_default"], PARALLELISM_DEFAULTS["lane_budget_default"])
            self.assertEqual(policy["max_depth"], FANOUT_MAX_DEPTH_DEFAULT)
            self.assertEqual(policy["run_spawn_ceiling"], FANOUT_RUN_SPAWN_CEILING_DEFAULT)
            self.assertEqual(policy["max_retries"], FANOUT_MAX_RETRIES)
            self.assertEqual(policy["security_posture"], "default")
            self.assertNotIn("default_concurrency_security_clamped_from", policy)

    def test_strict_posture_tightens_every_bundled_tunable(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(tmp)
            policy = read_parallelism_policy(paths, env={"OMH_SECURITY": "strict"})
            self.assertEqual(policy["security_posture"], "strict")
            self.assertLess(policy["default_concurrency"], PARALLELISM_DEFAULTS["default_concurrency"])
            self.assertLess(policy["global_concurrency"], PARALLELISM_DEFAULTS["global_concurrency"])
            self.assertLess(policy["lane_budget_default"], PARALLELISM_DEFAULTS["lane_budget_default"])
            self.assertLess(policy["run_spawn_ceiling"], FANOUT_RUN_SPAWN_CEILING_DEFAULT)
            self.assertEqual(policy["max_retries"], 0)
            self.assertEqual(policy["max_depth"], FANOUT_MAX_DEPTH_DEFAULT)

    def test_strict_posture_clamps_a_setup_profile_override_and_discloses_it(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(tmp)
            profile = build_setup_profile()
            profile["parallelism"] = {"default_concurrency": 5, "global_concurrency": 8, "max_depth": 3}
            paths.setup_profile_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(paths.setup_profile_path, profile, private=True)
            policy = read_parallelism_policy(paths, env={"OMH_SECURITY": "strict"})
            self.assertLess(policy["default_concurrency"], 5)
            self.assertEqual(policy["default_concurrency_security_clamped_from"], 5)
            self.assertLess(policy["global_concurrency"], 8)
            self.assertEqual(policy["global_concurrency_security_clamped_from"], 8)
            # A profile that asked for deeper nesting is pinned back to the
            # floor strict pins as a hard ceiling.
            self.assertEqual(policy["max_depth"], FANOUT_MAX_DEPTH_DEFAULT)
            self.assertEqual(policy["max_depth_security_clamped_from"], 3)

    def test_an_unrecognized_posture_value_is_rejected_loudly(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(tmp)
            with self.assertRaises(ValueError) as ctx:
                read_parallelism_policy(paths, env={"OMH_SECURITY": "paranoid"})
            self.assertIn("OMH_SECURITY", str(ctx.exception))

    def test_the_resolution_carries_max_retries_and_posture(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(tmp)
            policy = read_parallelism_policy(paths, env={"OMH_SECURITY": "strict"})
            resolution = resolve_fanout_concurrency(policy, None)
            self.assertEqual(resolution["max_retries"], 0)
            self.assertEqual(resolution["security_posture"], "strict")


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

    def _fanout_dispatch_args(self, paths, repo, contract, goal):
        import subprocess

        from omh.commands.main import build_parser

        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "add", "seed.txt"], cwd=str(repo), check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
            cwd=str(repo),
            check=True,
        )
        return build_parser().parse_args(
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

    def test_strict_posture_passes_the_tightened_max_retries_to_dispatch(self) -> None:
        import contextlib
        import io
        import os
        from unittest.mock import patch

        from omh.coding.fanout import build_fanout_contract
        from omh.coding.fanout_artifacts import write_fanout_contract

        goal_text = "split the sample feature across agents"
        units = [{"unit_id": "core", "title": "Core work", "owner": "codex", "file_scope": ["src/core/"]}]
        with TemporaryDirectory() as tmp:
            paths = _paths(tmp)
            contract = write_fanout_contract(paths, build_fanout_contract(goal_text, units))
            goal = Path(tmp) / "goal.txt"
            goal.write_text(goal_text, encoding="utf-8")
            args = self._fanout_dispatch_args(paths, Path(tmp) / "repo", contract, goal)
            captured: dict[str, object] = {}

            def _capture(*call_args, **kwargs):
                captured.update(kwargs)
                return {"schema_version": "fanout_dispatch_summary/v1", "units": []}

            with patch.dict(os.environ, {"OMH_SECURITY": "strict"}):
                with patch("omh.coding.fanout_dispatch.dispatch_fanout", new=_capture):
                    with contextlib.redirect_stdout(io.StringIO()):
                        args.func(args)
            self.assertEqual(captured["max_retries"], 0)
            self.assertLess(captured["concurrency"], 5)

    def test_an_unrecognized_posture_value_is_a_clean_cli_error(self) -> None:
        import os
        from unittest.mock import patch

        from omh.coding.fanout import build_fanout_contract
        from omh.coding.fanout_artifacts import write_fanout_contract
        from omh.installer import OmhError

        goal_text = "split the sample feature across agents"
        units = [{"unit_id": "core", "title": "Core work", "owner": "codex", "file_scope": ["src/core/"]}]
        with TemporaryDirectory() as tmp:
            paths = _paths(tmp)
            contract = write_fanout_contract(paths, build_fanout_contract(goal_text, units))
            goal = Path(tmp) / "goal.txt"
            goal.write_text(goal_text, encoding="utf-8")
            args = self._fanout_dispatch_args(paths, Path(tmp) / "repo", contract, goal)
            with patch.dict(os.environ, {"OMH_SECURITY": "paranoid"}):
                with self.assertRaises(OmhError) as ctx:
                    args.func(args)
            self.assertIn("OMH_SECURITY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
