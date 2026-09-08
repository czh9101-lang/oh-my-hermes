from __future__ import annotations

import json
from pathlib import Path

from omh.browser_workflow_learning import parse_browser_workflow_trace, replay_browser_workflow_trace

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "benchmarks" / "browser-trace-replay"


def main() -> int:
    trace = parse_browser_workflow_trace(_load("trace.json"))
    positive = replay_browser_workflow_trace(trace, {"fixture_id": "positive"})
    negative = replay_browser_workflow_trace(trace, {"fixture_id": "negative"})
    if positive["status"] != "replayed" or negative["status"] != "stale":
        return 1
    print(json.dumps({"positive": positive["status"], "negative": negative["status"]}, sort_keys=True))
    return 0


def _load(name: str) -> dict[str, object]:
    value = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("benchmark fixture must be an object")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
