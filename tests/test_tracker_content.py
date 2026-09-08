from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package

load_local_package()

from omh.commands.main import main  # noqa: E402
from omh.system.tracker_content import loads_tracker_event_json, normalize_tracker_content  # noqa: E402


def _event(*, body: str = "evidence") -> dict[str, object]:
    return {
        "tracker_content": {
            "provider": "github",
            "event_type": "issues",
            "payload": {
                "repository": {"id": "repo-e\u0301"},
                "issue": {"id": "issue-1381", "number": 1381, "title": "title", "body": body},
            },
        }
    }


def _host(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "authenticated": True,
        "fetch_status": "ok",
        "delivery_id": "delivery-1381",
        "replay_digest": "",
    }
    values.update(overrides)
    return values


class TrackerContentNormalizationTests(unittest.TestCase):
    def test_delivery_digest_replay_is_exact_and_ids_remain_literal(self) -> None:
        # Given: a valid event with a decomposed immutable repository id.
        event = _event()
        accepted = normalize_tracker_content(event, host_context=_host())
        assert accepted is not None

        # When: the host reports the delivery with its stored digest.
        replayed = normalize_tracker_content(event, host_context=_host(replay_digest=accepted["canonical_digest"]))
        changed = normalize_tracker_content(_event(body="changed"), host_context=_host(replay_digest=accepted["canonical_digest"]))

        # Then: exact replay is inert, changed bytes fail closed, and ids were not normalized.
        self.assertEqual(accepted["repository_id"], "repo-e\u0301")
        self.assertFalse(accepted["truncated"])
        self.assertEqual(accepted["normalization"], "none")
        self.assertEqual(replayed["state"], "already_seen")
        self.assertEqual(changed["state"], "blocked")
        self.assertEqual(changed["block_reason"], "delivery_conflict")

    def test_host_failures_and_invalid_shapes_are_metadata_only_blocks(self) -> None:
        # Given: one valid provider payload and hostile boundary variants.
        valid = _event()
        malformed = _event()
        malformed["tracker_content"] = {"provider": "github", "event_type": "issues", "payload": {"repository": {"id": "r"}, "issue": {"id": "i", "number": 1, "body": None}}}
        oversized = _event(body="x" * (64 * 1024 + 1))
        ambiguous = dict(valid)
        ambiguous["github_event"] = valid["tracker_content"]
        cases = (
            (_host(authenticated=False), valid, "unauthenticated"),
            (_host(fetch_status="failed"), valid, "fetch_failed"),
            (_host(delivery_id=""), valid, "missing_delivery"),
            (_host(), malformed, "malformed"),
            (_host(), oversized, "oversized"),
            (_host(), ambiguous, "ambiguous"),
        )

        # When: each boundary input is normalized.
        results = [normalize_tracker_content(event, host_context=host) for host, event, _ in cases]

        # Then: every invalid state has the fixed route and no content projection.
        for result, (_, _, reason) in zip(results, cases, strict=True):
            assert result is not None
            self.assertEqual(result["state"], "blocked")
            self.assertEqual(result["block_reason"], reason)
            self.assertEqual(result["route"], "github-event-ops")
            self.assertEqual(result["content_parts"], [])

    def test_pull_request_and_issue_comment_adapters_are_explicitly_supported(self) -> None:
        # Given: the two non-issue event adapters with their declared containers.
        pull_request = _event()
        pull_request["tracker_content"]["event_type"] = "pull_request"
        pull_request["tracker_content"]["payload"]["pull_request"] = pull_request["tracker_content"]["payload"].pop("issue")
        comment = {
            "tracker_content": {
                "provider": "github",
                "event_type": "issue_comment",
                "subtype": "pull_request",
                "payload": {
                    "repository": {"id": "repo-1381"},
                    "comment": {"id": "comment-1381", "number": 1, "body": "evidence"},
                },
            }
        }

        # When: each event reaches the normalizer with host evidence.
        results = [
            normalize_tracker_content(pull_request, host_context=_host()),
            normalize_tracker_content(comment, host_context=_host()),
        ]

        # Then: both have the fixed metadata route and no authority effect.
        for result in results:
            assert result is not None
            self.assertEqual(result["state"], "accepted")
            self.assertEqual(result["route"], "github-event-ops")
            self.assertEqual(result["authority_effect"], "none")

    def test_aggregate_size_depth_and_unsupported_subtype_fail_closed(self) -> None:
        # Given: bounds that are outside the selected title/body parts.
        oversized = _event()
        oversized["tracker_content"]["ignored"] = "x" * (72 * 1024)
        nested: dict[str, object] = {"leaf": "value"}
        for _ in range(13):
            nested = {"next": nested}
        too_deep = _event()
        too_deep["tracker_content"]["ignored"] = nested
        inline = _event()
        inline["tracker_content"]["event_type"] = "pull_request_review_comment"

        # When: each provider input crosses the closed adapter boundary.
        results = [
            normalize_tracker_content(oversized, host_context=_host()),
            normalize_tracker_content(too_deep, host_context=_host()),
            normalize_tracker_content(inline, host_context=_host()),
        ]

        # Then: aggregate, depth, and undeclared inline review inputs cannot continue.
        self.assertEqual(results[0]["block_reason"], "oversized")
        self.assertEqual(results[1]["block_reason"], "malformed")
        self.assertEqual(results[2]["block_reason"], "unsupported")

    def test_json_loader_rejects_duplicate_keys_and_nonfinite_values(self) -> None:
        # Given: serialized event inputs that a Python dict alone cannot represent.
        duplicate = '{"tracker_content":{},"tracker_content":{}}'
        nonfinite = '{"tracker_content":{"provider":"github","event_type":"issues","payload":NaN}}'

        # When / Then: parsing fails at the transport boundary.
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            loads_tracker_event_json(duplicate)
        with self.assertRaisesRegex(ValueError, "non-finite JSON value"):
            loads_tracker_event_json(nonfinite)

    def test_transport_size_and_discriminator_type_fail_closed(self) -> None:
        # Given: a transport body too large to safely decode and a parsed event
        # whose provider discriminator has the wrong type.
        oversized_json = '{"tracker_content":{"provider":"github","event_type":"issues","payload":"' + ("x" * (72 * 1024)) + '"}}'
        malformed = _event()
        malformed["tracker_content"]["event_type"] = None

        # When / Then: both boundaries reject before any generic extraction.
        with self.assertRaisesRegex(ValueError, "too large"):
            loads_tracker_event_json(oversized_json)
        result = normalize_tracker_content(malformed, host_context=_host())
        assert result is not None
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["block_reason"], "malformed")


class TrackerCliIngressTests(unittest.TestCase):
    def test_cli_event_json_uses_the_tracker_boundary(self) -> None:
        # Given: a tracker event supplied through the existing CLI event ingress.
        with TemporaryDirectory() as directory:
            event_path = Path(directory) / "event.json"
            event_path.write_text(json.dumps(_event()), encoding="utf-8")
            stdout = io.StringIO()

            # When: the public interaction command reads the event.
            with redirect_stdout(stdout):
                exit_code = main(["chat", "interact", "--event-json", str(event_path), "--json"])

        # Then: missing host authentication is terminal and no generic chat route is used.
        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["route"]["selected_skill"], "github-event-ops")
        self.assertEqual(payload["tracker_content"]["block_reason"], "unauthenticated")
        self.assertNotIn("delegation", payload)

    def test_cli_route_event_json_never_uses_generic_extraction(self) -> None:
        # Given: a provider event passed to the CLI routing command.
        with TemporaryDirectory() as directory:
            event_path = Path(directory) / "event.json"
            event_path.write_text(json.dumps(_event()), encoding="utf-8")
            stdout = io.StringIO()

            # When: the public route command reads the event.
            with redirect_stdout(stdout):
                exit_code = main(["chat", "route", "--event-json", str(event_path), "--json"])

        # Then: the metadata route survives without body extraction.
        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["route"]["selected_skill"], "github-event-ops")
        self.assertEqual(payload["route"]["tracker_content"]["block_reason"], "unauthenticated")


if __name__ == "__main__":
    unittest.main()
