#!/usr/bin/env python3
"""Live cross-harness benchmark controller entry point.

AUDIENCE: agent/maintainer. Offline `fake` mode is the default and starts no
process. `probe` executes only the free local `cross_harness_benchmark/v1`
command binding. `dispatch` additionally starts one isolated Hermes child and
therefore requires both `--allow-paid-live` and `--max-paid-calls`.

Run from the repository root:

    PYTHONPATH=. python3 benchmarks/cross-harness/live/bench.py run
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parent
REPOSITORY_ROOT = BASE.parents[2]
sys.path.insert(0, str(BASE / "lib"))

from controller import ControllerError, doctor, run  # noqa: E402
from receipt import RECEIPT_SCHEMA  # noqa: E402

DEFAULT_CORPUS = REPOSITORY_ROOT / "benchmarks" / "cross-harness" / "v1" / "manifest.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cross-harness-live",
        description=(
            "Execute cross_harness_benchmark/v1 work through the approved explicit "
            "dispatch boundary and emit a v1 envelope plus a controller receipt. "
            "The v1 corpus, schemas, and trust anchors are never modified."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    doctor_parser = sub.add_parser("doctor", help="Describe the lane; executes nothing.")
    doctor_parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    run_parser = sub.add_parser("run", help="Run the selected mode and emit envelope plus receipt.")
    run_parser.add_argument("--mode", choices=("fake", "probe", "dispatch"), default="fake")
    run_parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    run_parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    run_parser.add_argument(
        "--base",
        type=Path,
        help="Existing cross_harness_benchmark_cli_input/v1 envelope supplying unobserved fixture results.",
    )
    run_parser.add_argument(
        "--baseline",
        type=Path,
        help=(
            "Prior cross_harness_live_receipt/v2 receipt of the same task set; the "
            "emitted receipt then carries the verdict transitions against it."
        ),
    )
    run_parser.add_argument("--envelope-output", type=Path)
    run_parser.add_argument("--receipt-output", type=Path)
    run_parser.add_argument("--omh-executable", default="omh")
    run_parser.add_argument("--hermes-executable", default="hermes")
    run_parser.add_argument("--model", help="Hermes model alias metadata; required for --mode dispatch.")
    run_parser.add_argument("--provider", help="Provider alias metadata; required for --mode dispatch.")
    run_parser.add_argument("--reasoning", default="medium")
    run_parser.add_argument("--timeout", type=int, default=300)
    run_parser.add_argument("--allow-paid-live", action="store_true")
    run_parser.add_argument("--max-paid-calls", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            result = doctor(args.corpus)
        else:
            result = run(
                corpus_path=args.corpus,
                mode=args.mode,
                repository_root=args.repository_root,
                base_path=args.base,
                baseline_path=args.baseline,
                omh_executable=args.omh_executable,
                hermes_executable=args.hermes_executable,
                model=args.model,
                provider=args.provider,
                reasoning=args.reasoning,
                timeout=args.timeout,
                allow_paid_live=args.allow_paid_live,
                max_paid_calls=args.max_paid_calls,
            )
            _write(args.envelope_output, result["envelope"])
            _write(args.receipt_output, result["receipt"])
    except ControllerError as error:
        _emit({"schema_version": "cross_harness_live_error/v1", "ok": False, "reason_code": str(error)})
        return 2
    _emit(result)
    return 0 if result.get("ok", True) else 1


def _write(path: Path | None, value: object) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _emit(value: object) -> None:
    print(json.dumps(value, sort_keys=True, indent=2))


__all__ = ["RECEIPT_SCHEMA", "build_parser", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
