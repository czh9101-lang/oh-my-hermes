from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.system.tracker_content import normalize_tracker_content


def _event() -> dict[str, object]:
    return {
        "provider": "github",
        "event_type": "issues",
        "payload": {
            "repository": {"id": "repo-1"},
            "issue": {"id": "issue-1", "number": 1, "title": "Issue", "body": "Evidence"},
        },
    }


_HOST = {"authenticated": True, "fetch_status": "ok", "delivery_id": "delivery-1"}


class TrackerMalformedTests(unittest.TestCase):
    def test_invalid_event_type_returns_a_blocked_envelope(self) -> None:
        for value in (None, [], {}, ["issues"], True, 1):
            with self.subTest(value=value):
                result = normalize_tracker_content(
                    {**_event(), "event_type": value}, host_context=_HOST
                )
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["event_type"], "")

    def test_ignored_provider_arrays_are_not_counted_as_content_parts(self) -> None:
        event = _event()
        event["payload"]["issue"]["labels"] = ["one", "two", "three", "four"]

        result = normalize_tracker_content(event, host_context=_HOST)

        self.assertEqual(result["state"], "accepted")
        self.assertEqual(len(result["content_parts"]), 2)

    def test_invalid_unicode_is_terminal_metadata_not_an_exception(self) -> None:
        event = _event()
        event["payload"]["issue"]["body"] = "\ud800"

        result = normalize_tracker_content(event, host_context=_HOST)

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["block_reason"], "malformed")

    def test_aggregate_metadata_size_is_bounded(self) -> None:
        event = {**_event(), "extra": ["x" * 1024] * 1024}

        result = normalize_tracker_content(event, host_context=_HOST)

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["block_reason"], "oversized")

    def test_unsupported_event_text_is_not_reflected(self) -> None:
        result = normalize_tracker_content(
            {**_event(), "event_type": "private caller content"}, host_context=_HOST
        )

        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["event_type"], "")


if __name__ == "__main__":
    unittest.main()
