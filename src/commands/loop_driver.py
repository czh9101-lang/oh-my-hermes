"""Explicit local driver transfer and migration command adapters."""
from __future__ import annotations

import argparse

from ..core.errors import OmhError
from ..workflows.goal_loop import _guarded_cycle_update, build_loop_status_card, read_loop_cycle
from ..workflows.loop_driver_updates import bind_driver, migrate_driver
from ..workflows.loop_observation_input import read_loop_observation_json
from .common import _paths, _print_json, add_revision_guard_arguments


def cmd_loop_driver_bind(args: argparse.Namespace) -> int:
    try:
        submitted = read_loop_observation_json(args.input)
        paths = _paths(args)
        cycle = _guarded_cycle_update(
            paths, args.loop_id, lambda current: bind_driver(current, submitted),
            operation="bind_loop_driver", expected_revision=args.expected_revision,
        )
        _print_json({"loop": cycle, "status_card": build_loop_status_card(paths, args.loop_id)})
    except (OSError, ValueError) as exc:
        raise OmhError(str(exc)) from exc
    return 0


def cmd_loop_migrate_driver(args: argparse.Namespace) -> int:
    try:
        paths = _paths(args)
        cycle = (_guarded_cycle_update(paths, args.loop_id, migrate_driver, operation="migrate_loop_driver",
                                       expected_revision=args.expected_revision)
                 if args.apply else read_loop_cycle(paths, args.loop_id))
        _print_json({"loop": cycle, "applied": args.apply,
                     "status_card": build_loop_status_card(paths, args.loop_id)})
    except (OSError, ValueError) as exc:
        raise OmhError(str(exc)) from exc
    return 0


def add_driver_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    binding = sub.add_parser("driver-bind")
    binding.add_argument("--loop", dest="loop_id", required=True)
    binding.add_argument("--input", required=True)
    add_revision_guard_arguments(binding)
    binding.set_defaults(func=cmd_loop_driver_bind)
    migration = sub.add_parser("migrate-driver")
    migration.add_argument("--loop", dest="loop_id", required=True)
    migration.add_argument("--apply", action="store_true")
    add_revision_guard_arguments(migration)
    migration.set_defaults(func=cmd_loop_migrate_driver)
