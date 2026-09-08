#!/usr/bin/env python3
"""Measure bounded tracker normalization and ordinary chat ingress overhead."""

from __future__ import annotations

from collections.abc import Callable
import json
from math import ceil
from pathlib import Path
import platform
import sys
from time import perf_counter_ns

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from omh.system.tracker_content import normalize_tracker_content
from omh.wrapper.contract import _build_chat_interaction_payload_uncached, build_chat_interaction_payload

WARM_CALLS = 200
SAMPLE_CALLS = 2_000


def _p95_milliseconds(samples: list[int]) -> float:
    ordered = sorted(samples)
    return ordered[ceil(len(ordered) * 0.95) - 1] / 1_000_000


def _measure(call: Callable[[], object]) -> float:
    for _ in range(WARM_CALLS):
        call()
    samples: list[int] = []
    for _ in range(SAMPLE_CALLS):
        started = perf_counter_ns()
        call()
        samples.append(perf_counter_ns() - started)
    return _p95_milliseconds(samples)


def _tracker_event() -> dict[str, object]:
    return {
        "tracker_content": {
            "provider": "github",
            "event_type": "issues",
            "payload": {
                "repository": {"id": "benchmark-repository"},
                "issue": {"id": "benchmark-issue", "number": 1, "title": "benchmark", "body": "x" * (64 * 1024 - 128)},
            },
        }
    }


def _host_context() -> dict[str, object]:
    return {"authenticated": True, "fetch_status": "ok", "delivery_id": "benchmark-delivery", "replay_digest": ""}


def main() -> int:
    event = _tracker_event()
    host_context = _host_context()
    observed = normalize_tracker_content(event, host_context=host_context)
    if not isinstance(observed, dict) or observed.get("state") != "accepted":
        raise RuntimeError("benchmark tracker fixture was not accepted")
    ordinary_message = "summarize the current project status"
    ordinary_event = {"message": {"text": ordinary_message}}
    tracker_p95 = _measure(lambda: normalize_tracker_content(event, host_context=host_context))
    ordinary_baseline_p95 = _measure(
        lambda: _build_chat_interaction_payload_uncached(
            ordinary_event,
            source="generic",
            mode="auto",
            limit=3,
            min_confidence="high",
            include_message=False,
            executor_target="choose",
            source_metadata=None,
            main_agent_model="",
            target_notice=None,
            paths=None,
            skill_policy=None,
            host_project_binding_factory=None,
        )
    )
    ordinary_event_p95 = _measure(lambda: build_chat_interaction_payload(ordinary_event))
    latency_ratio = ordinary_event_p95 / ordinary_baseline_p95 if ordinary_baseline_p95 > 0 else None
    passes = tracker_p95 <= 5.0 and latency_ratio is not None and latency_ratio <= 1.10
    print(
        json.dumps(
            {
                "schema_version": "tracker_ingress_benchmark/v1",
                "environment": {
                    "platform": platform.platform(),
                    "python": platform.python_version(),
                    "machine": platform.machine(),
                },
                "calls": {"warm": WARM_CALLS, "sample": SAMPLE_CALLS},
                "tracker_64kib": {
                    "p95_ms": tracker_p95, "budget_ms": 5.0, "passes": tracker_p95 <= 5.0,
                    "serialized_input_bytes": len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()),
                },
                "ordinary_chat": {
                    "pre_tracker_seam_p95_ms": ordinary_baseline_p95,
                    "event_p95_ms": ordinary_event_p95,
                    "event_to_baseline_latency_ratio": latency_ratio,
                    "limit_ratio": 1.10,
                    "basis": "observed_p95_ratio" if latency_ratio is not None else "zero_baseline",
                    "passes": latency_ratio is not None and latency_ratio <= 1.10,
                },
            },
            sort_keys=True,
        )
    )
    return 0 if passes else 1


if __name__ == "__main__":
    raise SystemExit(main())
