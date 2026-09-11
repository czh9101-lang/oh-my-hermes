from __future__ import annotations

from collections.abc import Callable
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypeAlias
import unittest

from _cli_harness import run_cli


Json: TypeAlias = str | int | float | bool | None | list["Json"] | dict[str, "Json"]
decode: Callable[[str], Json] = json.loads


def mapping(value: Json) -> dict[str, Json]:
    assert isinstance(value, dict)
    return value


def text(value: Json) -> str:
    assert isinstance(value, str)
    return value


class GlobalScopeCliTests(unittest.TestCase):
    def test_reviewed_user_global_records_roundtrip_without_foreign_scope(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            homes = ["--omh-home", str(root / "omh"), "--hermes-home", str(root / "hermes")]

            def command(arguments: list[str]) -> dict[str, Json]:
                status, stdout, stderr = run_cli([*homes, "memory", *arguments])
                self.assertEqual(status, 0, stderr or stdout)
                return mapping(decode(stdout))

            def approved(scope: str, reference: str) -> str:
                captured = command([
                    "capture", "Release checklist requires tests",
                    "--scope-kind", scope, "--scope-ref", reference,
                ])
                candidate = mapping(captured["candidate"])
                self.assertEqual(mapping(candidate["scope"])["kind"], scope)
                candidate_id = text(candidate["candidate_id"])
                review = command(["review", "--candidate", candidate_id])
                cards = review["cards"]
                assert isinstance(cards, list)
                revision = text(mapping(cards[0])["review_revision"])
                result = command([
                    "approve", candidate_id, "--candidate-revision", revision,
                ])
                return text(mapping(result["record"])["record_id"])

            global_id = approved("user-global", "default")
            project_id = approved("project", "foreign-project")

            recalled = command([
                "recall", "release", "--scope-kind", "user-global",
                "--scope-ref", "default",
            ])
            included = recalled["included_records"]
            assert isinstance(included, list)
            self.assertEqual([mapping(item)["record_id"] for item in included], [global_id])
            self.assertNotIn(project_id, json.dumps(recalled))

            inspection = command(["recall", "release"])
            inspected = inspection["included_records"]
            assert isinstance(inspected, list)
            self.assertEqual(
                {text(mapping(item)["record_id"]) for item in inspected},
                {global_id, project_id},
            )

    def test_unknown_scope_still_fails_at_cli_boundary(self) -> None:
        with self.assertRaises((SystemExit, argparse.ArgumentError)) as raised:
            run_cli([
                "memory", "capture", "Synthetic claim", "--scope-kind", "all-users",
            ])
        error = raised.exception
        if isinstance(error, SystemExit):
            self.assertEqual(error.code, 2)
        else:
            self.assertEqual(error.argument_name, "--scope-kind")


if __name__ == "__main__":
    unittest.main()
