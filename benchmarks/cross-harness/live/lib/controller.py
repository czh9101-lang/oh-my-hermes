"""Live cross-harness benchmark controller: execute, then submit only what it saw.

The controller executes the `cross_harness_benchmark/v1` command binding and, in
`dispatch` mode, one isolated Hermes child through the approved
`omh coding hermes-child dispatch --confirm-dispatch` boundary. It then emits a
`cross_harness_benchmark_cli_input/v1` envelope whose fixture results carry only
observed values, plus a `cross_harness_live_receipt/v1` receipt binding that
envelope to the observations and their efficiency facts.

The v1 corpus, schemas, evaluator, and trust anchors are immutable and untouched.
The controller is a producer of ordinary v1 submissions: every envelope it emits
is scored by the same parser, evaluator, and scorer as a mailed-in file, and the
scorer still returns `evidence_authenticity: "unverified_submission"`. Provenance
lives only in the separate receipt.

Fixtures the controller cannot observe are simply absent from the submission, so
the v1 evaluator reports them `unsupported` (a coverage gap, never a pass). A
`--base` envelope may supply those results; the receipt then records them as
`carried_from_base` and downgrades the authenticity tier accordingly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time
from typing import Any, Final
import uuid

from omh.coding.routing_observation import validate_routing_observation
from omh.quality.cross_harness_benchmark import (
    CommandBinding,
    Corpus,
    Fixture,
    evaluate_submission,
    parse_corpus,
)
from omh.quality.cross_harness_benchmark_values import JsonValue, corpus_digest

from receipt import (
    CLAIM_BOUNDARY,
    DOCTOR_SCHEMA,
    RECEIPT_SCHEMA,
    RUN_SCHEMA,
    aggregate_efficiency,
    authenticity_tier,
    envelope_digest,
    validate_receipt,
)


INPUT_SCHEMA: Final = "cross_harness_benchmark_cli_input/v1"
SUBMISSION_SCHEMA: Final = "cross_harness_benchmark_submission/v1"

#: Fixtures whose predicate values the executed command binding itself supplies.
COMMAND_FIXTURES: Final = ("evidence-command-binding",)
#: Fixtures whose predicate values one observed Hermes child dispatch supplies.
DISPATCH_FIXTURES: Final = (
    "evidence-runtime-observation",
    "ultrawork-child-propagation",
    "ultrawork-observed-runtime",
)
#: The bounded task sent to the child on stdin only. Never persisted.
DISPATCH_TASK: Final = (
    "Cross-harness benchmark liveness probe. Do not modify any file. "
    "Reply with the single word READY and stop."
)
MAX_TIMEOUT_SECONDS: Final = 3600


class ControllerError(ValueError):
    """A refusal or contract failure raised before any effect is claimed."""


@dataclass(frozen=True, slots=True)
class Observation:
    """One controller-observed execution, with efficiency kept beside quality."""

    observation_id: str
    kind: str
    cwd_class: str
    argv_digest: str
    status: str
    observed_exit: int | None = None
    observed_semantic_result: str | None = None
    failure_code: str | None = None
    duration_ms: int | None = None
    tokens: int | None = None
    cost_usd: float | None = None
    tools: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "completed"

    def payload(self) -> dict[str, JsonValue]:
        return {
            "observation_id": self.observation_id,
            "kind": self.kind,
            "cwd_class": self.cwd_class,
            "argv_digest": self.argv_digest,
            "status": self.status,
            "observed_exit": self.observed_exit,
            "observed_semantic_result": self.observed_semantic_result,
            "failure_code": self.failure_code,
            "duration_ms": self.duration_ms,
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
            "tools": self.tools,
        }


def doctor(corpus_path: Path) -> dict[str, JsonValue]:
    """Describe the lane without executing anything."""
    corpus, _ = load_corpus(corpus_path)
    return {
        "schema_version": DOCTOR_SCHEMA,
        "ok": True,
        "corpus_id": corpus.corpus_id,
        "corpus_digest": corpus.digest,
        "default_mode": "fake",
        "modes": ["fake", "probe", "dispatch"],
        "observable_fixture_ids": sorted((*COMMAND_FIXTURES, *DISPATCH_FIXTURES)),
        "command_binding_ids": sorted(item.command_id for item in corpus.commands),
        "dispatch_boundary": "omh coding hermes-child dispatch --confirm-dispatch",
        "observation_schema": "routing_observation/v1",
        "receipt_schema": RECEIPT_SCHEMA,
        "envelope_schema": INPUT_SCHEMA,
        "v1_corpus_mutated": False,
        "claim_boundary": CLAIM_BOUNDARY,
    }


def load_corpus(path: Path) -> tuple[Corpus, dict[str, JsonValue]]:
    """Load the frozen corpus and re-parse it through the production parser."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ControllerError("corpus_unavailable") from error
    except json.JSONDecodeError as error:
        raise ControllerError("corpus_invalid_json") from error
    if not isinstance(raw, dict):
        raise ControllerError("corpus_must_be_object")
    return parse_corpus(raw), raw


def load_base_results(path: Path, corpus: Corpus) -> dict[str, JsonValue]:
    """Read carried fixture results from an existing CLI-input envelope."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ControllerError("base_unavailable") from error
    except json.JSONDecodeError as error:
        raise ControllerError("base_invalid_json") from error
    if not isinstance(raw, dict) or raw.get("schema_version") != INPUT_SCHEMA:
        raise ControllerError("base_not_cli_input")
    submission = raw.get("submission")
    if not isinstance(submission, dict):
        raise ControllerError("base_missing_submission")
    if submission.get("corpus_digest") != corpus.digest:
        raise ControllerError("base_corpus_mismatch")
    results = submission.get("results")
    if not isinstance(results, list):
        raise ControllerError("base_missing_results")
    carried: dict[str, JsonValue] = {}
    for item in results:
        if not isinstance(item, dict) or not isinstance(item.get("fixture_id"), str):
            raise ControllerError("base_result_invalid")
        carried[str(item["fixture_id"])] = item
    return carried


def run(
    *,
    corpus_path: Path,
    mode: str = "fake",
    repository_root: Path,
    base_path: Path | None = None,
    omh_executable: str = "omh",
    hermes_executable: str = "hermes",
    model: str | None = None,
    provider: str | None = None,
    reasoning: str = "medium",
    timeout: int = 300,
    allow_paid_live: bool = False,
    max_paid_calls: int = 0,
) -> dict[str, JsonValue]:
    """Execute the selected mode and return the envelope with its receipt."""
    if mode not in {"fake", "probe", "dispatch"}:
        raise ControllerError("unknown_mode")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise ControllerError("invalid_timeout")
    scheduled_paid_calls = 1 if mode == "dispatch" else 0
    if scheduled_paid_calls:
        if not allow_paid_live:
            raise ControllerError("paid_live_not_allowed")
        if max_paid_calls < scheduled_paid_calls:
            raise ControllerError("paid_call_budget_exceeded")
        if not model or not provider:
            raise ControllerError("dispatch_requires_model_and_provider")
    elif allow_paid_live:
        raise ControllerError("paid_live_flag_without_dispatch")

    corpus, corpus_raw = load_corpus(corpus_path)
    command = _single_command(corpus)
    carried = load_base_results(base_path, corpus) if base_path is not None else {}
    observations: list[Observation] = []

    if mode != "fake":
        with TemporaryDirectory(prefix="omh-cross-harness-live-") as root_text:
            root = Path(root_text)
            home = root / "home"
            home.mkdir()
            observations.append(
                execute_command_binding(
                    command,
                    repository_root=repository_root,
                    home_root=home,
                    timeout=timeout,
                )
            )
            if mode == "dispatch":
                workspace = root / "workspace"
                workspace.mkdir()
                observations.append(
                    execute_child_dispatch(
                        omh_executable=omh_executable,
                        hermes_executable=hermes_executable,
                        workspace=workspace,
                        home_root=home,
                        model=str(model),
                        provider=str(provider),
                        reasoning=reasoning,
                        timeout=timeout,
                        confirmed=allow_paid_live,
                    )
                )

    results, bindings = _fixture_results(corpus, mode, observations, carried)
    envelope = _envelope(corpus, corpus_raw, results)
    evaluate_submission(envelope["submission"], corpus)  # self-check via the real evaluator
    receipt = _receipt(
        mode=mode,
        corpus=corpus,
        command=command,
        envelope=envelope,
        observations=observations,
        bindings=bindings,
    )
    reasons = validate_receipt(receipt)
    if reasons:
        raise ControllerError("invalid_receipt:" + ",".join(reasons))
    return {
        "schema_version": RUN_SCHEMA,
        "ok": all(item.succeeded for item in observations),
        "mode": mode,
        "paid_calls_launched": scheduled_paid_calls if observations else 0,
        "envelope": envelope,
        "receipt": receipt,
    }


def execute_command_binding(
    command: CommandBinding,
    *,
    repository_root: Path,
    home_root: Path,
    timeout: int,
) -> Observation:
    """Run the corpus command binding verbatim in its declared working directory."""
    argv = list(command.argv)
    observation = {
        "observation_id": "obs-command-binding",
        "kind": "command_binding",
        "cwd_class": command.cwd_class,
        "argv_digest": corpus_digest(argv),
    }
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=repository_root,
            env=_environment(home_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return Observation(**observation, status="timed_out", failure_code="timeout", duration_ms=_elapsed_ms(started))
    except OSError:
        return Observation(**observation, status="failed", failure_code="command_launch_failed")
    duration_ms = _elapsed_ms(started)
    semantic = _semantic_result(completed.returncode, completed.stdout, command)
    return Observation(
        **observation,
        status="completed" if completed.returncode == command.expected_exit else "failed",
        observed_exit=completed.returncode,
        observed_semantic_result=semantic,
        failure_code=None if completed.returncode == command.expected_exit else "nonzero_exit",
        duration_ms=duration_ms,
    )


def child_dispatch_argv(
    omh_executable: str,
    *,
    hermes_executable: str,
    workspace: Path,
    model: str,
    provider: str,
    reasoning: str,
    parent_run_id: str,
    run_id: str,
    timeout: int,
) -> list[str]:
    """Build the approved explicit dispatch argv. The prompt never appears here."""
    return [
        omh_executable,
        "coding",
        "hermes-child",
        "dispatch",
        "--confirm-dispatch",
        "--model",
        model,
        "--provider",
        provider,
        "--reasoning",
        reasoning,
        "--parent-run-id",
        parent_run_id,
        "--run-id",
        run_id,
        "--hermes",
        hermes_executable,
        "--cwd",
        str(workspace),
        "--timeout",
        str(timeout),
        "--json",
    ]


def execute_child_dispatch(
    *,
    omh_executable: str,
    hermes_executable: str,
    workspace: Path,
    home_root: Path,
    model: str,
    provider: str,
    reasoning: str,
    timeout: int,
    confirmed: bool = False,
) -> Observation:
    """Dispatch one isolated Hermes child through the approved explicit boundary."""
    if not confirmed:
        raise ControllerError("paid_live_not_allowed")
    argv = child_dispatch_argv(
        omh_executable,
        hermes_executable=hermes_executable,
        workspace=workspace,
        model=model,
        provider=provider,
        reasoning=reasoning,
        parent_run_id=f"xh-live-parent-{uuid.uuid4().hex}",
        run_id=f"xh-live-child-{uuid.uuid4().hex}",
        timeout=timeout,
    )
    observation = {
        "observation_id": "obs-hermes-child-dispatch",
        "kind": "hermes_child_dispatch",
        "cwd_class": "isolated_temporary",
        # The workspace path is a local absolute path and is deliberately not
        # part of the digested argv, which stays metadata-only.
        "argv_digest": corpus_digest([item for item in argv if not item.startswith("/")]),
    }
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            input=DISPATCH_TASK,
            cwd=workspace,
            env=_environment(home_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout + 30,
        )
    except subprocess.TimeoutExpired:
        return Observation(**observation, status="timed_out", failure_code="timeout", duration_ms=_elapsed_ms(started))
    except OSError:
        return Observation(**observation, status="failed", failure_code="command_launch_failed")
    duration_ms = _elapsed_ms(started)
    if completed.returncode:
        return Observation(
            **observation,
            status="failed",
            observed_exit=completed.returncode,
            failure_code="nonzero_exit",
            duration_ms=duration_ms,
        )
    metrics = _routing_observation_metrics(completed.stdout)
    if metrics is None:
        return Observation(
            **observation,
            status="failed",
            observed_exit=completed.returncode,
            failure_code="observation_invalid",
            duration_ms=duration_ms,
        )
    return Observation(
        **observation,
        status="completed",
        observed_exit=completed.returncode,
        observed_semantic_result=str(metrics["status"]),
        duration_ms=duration_ms,
        tokens=metrics["tokens"],
        cost_usd=metrics["cost_usd"],
        tools=metrics["tools"],
    )


def _routing_observation_metrics(stdout: str) -> dict[str, Any] | None:
    """Read only the allowlisted scalars from a validated routing observation."""
    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict) or validate_routing_observation(raw):
        return None
    return {
        "claim": "observed" if raw.get("claim") == "observed" else "prepared_not_observed",
        "status": str(raw.get("status") or "failed"),
        "tokens": raw["tokens"] if type(raw.get("tokens")) is int else None,
        "tools": raw["tools"] if type(raw.get("tools")) is int else None,
        "cost_usd": raw["cost_usd"]
        if isinstance(raw.get("cost_usd"), (int, float)) and not isinstance(raw.get("cost_usd"), bool)
        else None,
    }


def _semantic_result(exit_code: int, stdout: str, command: CommandBinding) -> str:
    """Derive the observed semantic result; stdout itself is never retained."""
    if exit_code != command.expected_exit:
        return "unvalidated"
    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError:
        return "unvalidated"
    if isinstance(raw, dict) and raw.get("ok") is True and raw.get("errors") == []:
        return command.expected_semantic_result
    return "unvalidated"


def _fixture_results(
    corpus: Corpus,
    mode: str,
    observations: Sequence[Observation],
    carried: Mapping[str, JsonValue],
) -> tuple[list[JsonValue], list[dict[str, JsonValue]]]:
    by_kind = {item.kind: item for item in observations}
    command_observation = by_kind.get("command_binding")
    dispatch_observation = by_kind.get("hermes_child_dispatch")
    results: list[JsonValue] = []
    bindings: list[dict[str, JsonValue]] = []
    for fixture in corpus.fixtures:
        if mode == "fake":
            if fixture.id not in (*COMMAND_FIXTURES, *DISPATCH_FIXTURES):
                built = None
            else:
                built = _simulated_result(fixture, corpus)
            provenance, bound = "fake_adapter", []
        elif fixture.id in COMMAND_FIXTURES and command_observation is not None:
            built = _observed_result(fixture, corpus, command_observation, command_observation)
            provenance, bound = "controller_observed", [command_observation.observation_id]
        elif fixture.id in DISPATCH_FIXTURES and dispatch_observation is not None and command_observation is not None:
            built = _observed_result(fixture, corpus, command_observation, dispatch_observation)
            provenance = "controller_observed"
            bound = [command_observation.observation_id, dispatch_observation.observation_id]
        else:
            built, provenance, bound = None, "carried_from_base", []
        if built is None:
            built = carried.get(fixture.id)
            provenance, bound = "carried_from_base", []
        if built is None:
            continue
        results.append(built)
        bindings.append({"fixture_id": fixture.id, "provenance": provenance, "observation_ids": list(bound)})
    return results, bindings


def _simulated_result(fixture: Fixture, corpus: Corpus) -> dict[str, JsonValue]:
    """Offline simulation: satisfies the predicate but never claims observation.

    `prepared` evidence is below every fixture's required class, so a fake run
    can never produce a passing fixture, a level, or a certification.
    """
    actual, facts = _predicate_values(fixture)
    return _result(
        fixture,
        corpus,
        actual=actual,
        facts=facts,
        evidence_class="prepared",
        runtime_observation="prepared_not_observed",
        command_exit=corpus.commands[0].expected_exit,
        command_semantic=corpus.commands[0].expected_semantic_result,
        child_result="pass",
    )


def _observed_result(
    fixture: Fixture,
    corpus: Corpus,
    command_observation: Observation,
    source: Observation,
) -> dict[str, JsonValue]:
    actual, facts = _observed_values(fixture, source)
    return _result(
        fixture,
        corpus,
        actual=actual,
        facts=facts,
        evidence_class="runtime",
        runtime_observation="observed",
        command_exit=command_observation.observed_exit if command_observation.observed_exit is not None else -1,
        command_semantic=command_observation.observed_semantic_result or "unvalidated",
        child_result="pass" if source.succeeded else "fail",
    )


def _observed_values(fixture: Fixture, source: Observation) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    """Map one observation onto the fixture's predicate keys, observed values only."""
    if fixture.id == "evidence-command-binding":
        return {"semantic_result": source.observed_semantic_result or "unvalidated"}, {}
    if fixture.id == "evidence-runtime-observation":
        return {}, {"observation_state": "observed" if source.succeeded else "prepared_not_observed"}
    if fixture.id == "ultrawork-child-propagation":
        return {"parent_exit": source.observed_exit if source.observed_exit is not None else -1}, {}
    if fixture.id == "ultrawork-observed-runtime":
        return {}, {"dispatch_state": source.observed_semantic_result or "failed"}
    raise ControllerError("fixture_not_observable")


def _predicate_values(fixture: Fixture) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    actual: dict[str, JsonValue] = {}
    facts: dict[str, JsonValue] = {}
    for predicate in fixture.predicates:
        target = actual if predicate.scope == "actual_machine" else facts
        target[predicate.key] = predicate.value
    return actual, facts


def _result(
    fixture: Fixture,
    corpus: Corpus,
    *,
    actual: dict[str, JsonValue],
    facts: dict[str, JsonValue],
    evidence_class: str,
    runtime_observation: str,
    command_exit: int,
    command_semantic: str,
    child_result: str,
) -> dict[str, JsonValue]:
    source = next(item for item in corpus.sources if item.source_id == fixture.source_id)
    command = next(item for item in corpus.commands if item.command_id == fixture.command_binding_id)
    source_base: dict[str, JsonValue] = {
        "source_id": source.source_id,
        "commit": source.commit,
        "license": source.license,
        "path_metadata": source.path_metadata,
    }
    command_base: dict[str, JsonValue] = {
        "command_id": command.command_id,
        "harness": command.harness,
        "argv": list(command.argv),
        "cwd_class": command.cwd_class,
        "source_id": command.source_id,
        "source_commit": command.source_commit,
        "expected_exit": command.expected_exit,
        "expected_semantic_result": command.expected_semantic_result,
    }
    return {
        "fixture_id": fixture.id,
        "adapter_id": fixture.adapter_id,
        "capability_id": fixture.capability_id,
        "evidence_class": evidence_class,
        "runtime_observation": runtime_observation,
        "actual_machine": dict(actual),
        "facts": dict(facts),
        "source_binding": {**source_base, "source_digest": corpus_digest(source_base)},
        "command_evidence": {
            **command_base,
            "binding_digest": corpus_digest(command_base),
            "observed_exit": command_exit,
            "observed_semantic_result": command_semantic,
        },
        "child_results": [{"id": "primary", "result": child_result}],
    }


def _envelope(corpus: Corpus, corpus_raw: Mapping[str, JsonValue], results: list[JsonValue]) -> dict[str, JsonValue]:
    return {
        "schema_version": INPUT_SCHEMA,
        "corpus": dict(corpus_raw),
        "submission": {
            "schema_version": SUBMISSION_SCHEMA,
            "corpus_digest": corpus.digest,
            "harness_id": corpus.commands[0].harness,
            "results": results,
        },
    }


def _receipt(
    *,
    mode: str,
    corpus: Corpus,
    command: CommandBinding,
    envelope: Mapping[str, JsonValue],
    observations: Sequence[Observation],
    bindings: Sequence[Mapping[str, JsonValue]],
) -> dict[str, JsonValue]:
    def selected(provenance: str) -> list[JsonValue]:
        return [str(item["fixture_id"]) for item in bindings if item["provenance"] == provenance]

    observed = selected("controller_observed")
    carried = selected("carried_from_base")
    simulated = selected("fake_adapter")
    submitted = {str(item["fixture_id"]) for item in bindings}
    unsupported = [item.id for item in corpus.fixtures if item.id not in submitted]
    payloads = [item.payload() for item in observations]
    return {
        "schema_version": RECEIPT_SCHEMA,
        "mode": mode,
        "harness_id": command.harness,
        "corpus_digest": corpus.digest,
        "envelope_digest": envelope_digest(envelope),
        "evidence_authenticity": authenticity_tier(
            mode=mode, observed_count=len(observed), carried_count=len(carried)
        ),
        "controller_observed_fixture_ids": observed,
        "carried_fixture_ids": carried,
        "simulated_fixture_ids": simulated,
        "unsupported_fixture_ids": unsupported,
        "fixture_bindings": [dict(item) for item in bindings],
        "observations": payloads,
        "efficiency": aggregate_efficiency(payloads),
        "claim_boundary": CLAIM_BOUNDARY,
    }


def _single_command(corpus: Corpus) -> CommandBinding:
    if len(corpus.commands) != 1:
        raise ControllerError("unsupported_command_binding_count")
    return corpus.commands[0]


def _environment(home_root: Path) -> dict[str, str]:
    """Inherit PATH and credentials but redirect every runtime home to a temp root."""
    environment = dict(os.environ)
    environment["OMH_HOME"] = str(home_root / "omh")
    environment["HERMES_HOME"] = str(home_root / "hermes")
    return environment


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))
