from __future__ import annotations

from copy import deepcopy
import unittest

from _local_package import load_local_package

load_local_package()
from omh.workflows.lifecycle_growth_contracts import (
    build_analysis_cancellation,
    build_analysis_status,
    evaluate_lifecycle_growth,
    prepare_lifecycle_growth,
    readout_lifecycle_growth,
    route_lifecycle_analysis_request,
    select_latest_analysis_run,
    validate_lifecycle_growth_artifact,
)
from test_lifecycle_growth_contracts import LifecycleGrowthContractTests
from test_lifecycle_growth_readiness import LifecycleGrowthReadinessTests


# Well past the twenty-four hours the reviewed upstream used as a cutoff, so a
# fixture that says "old" says it in the units the record actually carries.
BEYOND_ONE_DAY_MINUTES = 3 * 24 * 60


def _status(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "run_state": "completed",
        "run_ref": "analysis_run_activation_q3_01",
        "observed_at": "2026-09-08T09:00:00Z",
        "elapsed_minutes": 42,
        "service_expectation_minutes": 120,
        "evidence_refs": ("evidence_analysis_run_completed",),
    }
    values.update(overrides)
    return build_analysis_status(**values)


def _launch_artifacts() -> dict[str, dict[str, object]]:
    return LifecycleGrowthContractTests()._launch_artifacts()


def _readout(**overrides: object) -> dict[str, object]:
    return LifecycleGrowthContractTests()._readout(**overrides)


def _experiment() -> dict[str, object]:
    return LifecycleGrowthReadinessTests()._experiment()


def _cancellation(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "request_state": "prepared",
        "run_ref": "analysis_run_activation_q3_01",
        "cancel_scope": "single_run",
        "acknowledgement_state": "not_observed",
        "result_state": "not_observed",
        "evidence_refs": (),
    }
    values.update(overrides)
    return build_analysis_cancellation(**values)


class AnalysisRunStateTests(unittest.TestCase):
    def test_given_queued_run_older_than_a_day_when_read_out_then_state_survives_as_a_delay(self) -> None:
        status = _status(run_state="queued", elapsed_minutes=BEYOND_ONE_DAY_MINUTES, service_expectation_minutes=120, evidence_refs=("evidence_analysis_queued",))
        result = readout_lifecycle_growth(_readout(analysis_status=dict(_status_input(status))))

        self.assertEqual(status["run_state"], "queued")
        self.assertEqual(status["delay_state"], "delayed")
        self.assertEqual(result["analysis_run_state"], "queued")
        self.assertEqual(result["analysis_delay_state"], "delayed")
        self.assertEqual(result["analysis_observed_at"], "2026-09-08T09:00:00Z")
        self.assertNotIn(result["analysis_run_state"], ("failed", "canceled", "completed", "not_started"))
        self.assertEqual(result["disposition"], "review")

    def test_given_no_supplied_expectation_when_in_flight_then_age_alone_never_delays_or_settles(self) -> None:
        unmeasured = _status(run_state="running", elapsed_minutes=BEYOND_ONE_DAY_MINUTES, service_expectation_minutes=0, evidence_refs=("evidence_analysis_running",))
        generous = _status(run_state="running", elapsed_minutes=BEYOND_ONE_DAY_MINUTES, service_expectation_minutes=BEYOND_ONE_DAY_MINUTES * 2, evidence_refs=("evidence_analysis_running",))

        self.assertEqual((unmeasured["run_state"], unmeasured["delay_state"]), ("running", "unknown"))
        self.assertEqual((generous["run_state"], generous["delay_state"]), ("running", "within_expectation"))

    def test_given_settled_or_unstarted_runs_when_built_then_delay_is_not_applicable(self) -> None:
        for state in ("completed", "failed", "canceled", "unknown"):
            with self.subTest(state=state):
                self.assertEqual(_status(run_state=state, elapsed_minutes=BEYOND_ONE_DAY_MINUTES)["delay_state"], "not_applicable")
        not_started = build_analysis_status(run_state="not_started", observed_at="2026-09-08T09:00:00Z")
        self.assertEqual((not_started["run_ref"], not_started["evidence_refs"], not_started["delay_state"]), ("", [], "not_applicable"))

    def test_given_each_run_state_when_read_out_then_dispositions_stay_distinct_from_data_health(self) -> None:
        for state, expected in (
            ("not_started", "insufficient_data"),
            ("queued", "review"),
            ("running", "review"),
            ("completed", "insufficient_data"),
            ("failed", "insufficient_data"),
            ("canceled", "insufficient_data"),
            ("unknown", "insufficient_data"),
        ):
            with self.subTest(state=state):
                result = readout_lifecycle_growth(_readout(analysis_status=_status_input_for(state)))
                self.assertEqual(result["disposition"], expected)
                self.assertEqual(result["analysis_run_state"], state)

    def test_given_a_completed_run_over_stale_data_when_read_out_then_freshness_still_decides(self) -> None:
        stale = readout_lifecycle_growth(_readout(data_freshness_state="stale"))
        broken = readout_lifecycle_growth(_readout(instrumentation_state="broken"))

        self.assertEqual((stale["analysis_run_state"], stale["disposition"]), ("completed", "insufficient_data"))
        self.assertEqual((broken["analysis_run_state"], broken["disposition"]), ("completed", "review"))

    def test_given_an_experiment_past_minimum_runtime_when_analysis_is_queued_then_runtimes_stay_separate(self) -> None:
        result = evaluate_lifecycle_growth(_experiment(), _readout(analysis_status=_status_input_for("queued")))

        self.assertEqual(result["runtime_days_observed"], 14)
        self.assertNotIn("minimum runtime has not elapsed", result["artifact_errors"])
        self.assertEqual(result["analysis_run_state"], "queued")
        self.assertEqual(result["disposition"], "insufficient_data")
        from _lifecycle_configuration import sequence
        self.assertIn("configuration_identity_missing", sequence(result["evidence_reason_codes"]))

    def test_given_a_guardrail_breach_when_analysis_is_in_flight_then_rollback_still_outranks_it(self) -> None:
        result = readout_lifecycle_growth(_readout(rollback_state="triggered", analysis_status=_status_input_for("running")))

        self.assertEqual(result["disposition"], "rollback")

    def test_given_a_malformed_analysis_status_when_validated_then_errors_not_exceptions_are_returned(self) -> None:
        forged_delay = deepcopy(_readout(analysis_status=_status_input_for("queued")))
        forged_delay["analysis_status"]["delay_state"] = "within_expectation"
        unbacked = deepcopy(_readout())
        unbacked["analysis_status"]["evidence_refs"] = []
        phantom = deepcopy(_readout())
        phantom["analysis_status"]["run_state"] = "not_started"

        self.assertTrue(any("delay_state" in error for error in validate_lifecycle_growth_artifact(forged_delay)))
        self.assertTrue(any("evidence_refs" in error for error in validate_lifecycle_growth_artifact(unbacked)))
        self.assertTrue(any("must be empty when no analysis has started" in error for error in validate_lifecycle_growth_artifact(phantom)))

    def test_given_a_run_reference_on_an_unstarted_analysis_when_built_then_it_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            build_analysis_status(run_state="not_started", run_ref="analysis_run_activation_q3_01")
        with self.assertRaises(ValueError):
            build_analysis_status(run_state="queued", run_ref="analysis_run_activation_q3_01", observed_at="not-a-timestamp", evidence_refs=("evidence_analysis_queued",))


class AnalysisRunSelectionTests(unittest.TestCase):
    def _runs(self) -> tuple[dict[str, object], ...]:
        return (
            _status(run_state="completed", run_ref="analysis_run_01", observed_at="2026-09-08T12:00:00Z"),
            _status(run_state="queued", run_ref="analysis_run_02", observed_at="2026-09-06T09:00:00Z", elapsed_minutes=BEYOND_ONE_DAY_MINUTES, evidence_refs=("evidence_analysis_queued",)),
            _status(run_state="running", run_ref="analysis_run_03", observed_at="2026-09-07T09:00:00Z", elapsed_minutes=90, evidence_refs=("evidence_analysis_running",)),
            _status(run_state="failed", run_ref="analysis_run_04", observed_at="2026-09-08T18:00:00Z", evidence_refs=("evidence_analysis_failed",)),
        )

    def test_given_several_runs_when_selected_then_the_newest_in_flight_run_wins_over_newer_settled_ones(self) -> None:
        selection = select_latest_analysis_run(self._runs())

        self.assertEqual(selection["selection_state"], "in_flight")
        self.assertEqual(selection["selected_run_ref"], "analysis_run_03")
        self.assertEqual(selection["selected_run_state"], "running")
        self.assertEqual(selection["selected_observed_at"], "2026-09-07T09:00:00Z")
        self.assertEqual(selection["selected_evidence_refs"], ["evidence_analysis_running"])
        self.assertEqual(selection["in_flight_count"], 2)
        self.assertEqual(selection["reason_codes"], [])

    def test_given_only_settled_or_no_runs_when_selected_then_the_state_says_so(self) -> None:
        settled = select_latest_analysis_run((self._runs()[0], self._runs()[3]))
        empty = select_latest_analysis_run(())
        unstarted = select_latest_analysis_run((build_analysis_status(run_state="not_started", observed_at="2026-09-08T09:00:00Z"),))

        self.assertEqual((settled["selection_state"], settled["selected_run_ref"]), ("settled", "analysis_run_04"))
        self.assertEqual((empty["selection_state"], empty["selected_run_ref"]), ("none", ""))
        self.assertEqual(unstarted["selection_state"], "none")

    def test_given_an_invalid_or_oversized_record_set_when_selected_then_reasons_replace_a_silent_pick(self) -> None:
        broken = dict(self._runs()[1])
        broken["run_state"] = "in_progress"
        mixed = select_latest_analysis_run((self._runs()[0], broken))
        oversized = select_latest_analysis_run(tuple(self._runs()[0] for _ in range(9)))

        self.assertEqual(mixed["reason_codes"], ["analysis run record 1 is invalid"])
        self.assertEqual(mixed["selected_run_ref"], "analysis_run_01")
        self.assertTrue(oversized["reason_codes"])
        self.assertEqual(oversized["selection_state"], "none")

    def test_given_an_in_flight_run_when_another_is_requested_then_it_holds_until_reconciled(self) -> None:
        pending = route_lifecycle_analysis_request(self._runs(), reconciliation_state="pending")
        reconciled = route_lifecycle_analysis_request(self._runs(), reconciliation_state="reconciled")
        settled = route_lifecycle_analysis_request((self._runs()[0],), reconciliation_state="not_requested")

        self.assertEqual(pending["verdict"], "HOLD")
        self.assertEqual(pending["duplicate_risk_state"], "in_flight_run_present")
        self.assertIn("the latest analysis run is still in flight; reconcile it before preparing another", pending["reason_codes"])
        self.assertEqual(reconciled["verdict"], "READY")
        self.assertEqual(reconciled["duplicate_risk_state"], "in_flight_run_present")
        self.assertEqual((settled["verdict"], settled["duplicate_risk_state"]), ("READY", "none"))

    def test_given_an_unknown_reconciliation_state_when_requested_then_it_holds(self) -> None:
        result = route_lifecycle_analysis_request((self._runs()[0],), reconciliation_state="already_handled")

        self.assertEqual(result["verdict"], "HOLD")
        self.assertEqual(result["reconciliation_state"], "")


class AnalysisCancellationTests(unittest.TestCase):
    def test_given_each_cancellation_stage_when_built_then_prepared_acknowledged_and_terminal_stay_apart(self) -> None:
        prepared = _cancellation()
        accepted = _cancellation(acknowledgement_state="observed_accepted", evidence_refs=("evidence_cancel_accepted",))
        terminal = _cancellation(acknowledgement_state="observed_accepted", result_state="observed_canceled", evidence_refs=("evidence_cancel_accepted", "evidence_run_canceled"))

        self.assertEqual(prepared["handoff_status"], "prepared_not_observed")
        self.assertEqual((prepared["acknowledgement_state"], prepared["result_state"]), ("not_observed", "not_observed"))
        self.assertEqual(accepted["handoff_status"], "prepared_not_observed")
        self.assertEqual((accepted["acknowledgement_state"], accepted["result_state"]), ("observed_accepted", "not_observed"))
        self.assertEqual(terminal["handoff_status"], "prepared_not_observed")
        self.assertEqual(terminal["result_state"], "observed_canceled")

    def test_given_an_unbacked_or_unrequested_cancellation_when_built_then_it_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            _cancellation(result_state="observed_canceled", evidence_refs=("evidence_run_canceled",))
        with self.assertRaises(ValueError):
            _cancellation(acknowledgement_state="observed_rejected", result_state="observed_canceled", evidence_refs=("evidence_cancel_rejected",))
        with self.assertRaises(ValueError):
            _cancellation(acknowledgement_state="observed_accepted", evidence_refs=())
        with self.assertRaises(ValueError):
            _cancellation(request_state="not_requested", run_ref="analysis_run_activation_q3_01", cancel_scope="")

    def test_given_a_canceled_run_state_when_prepared_then_only_an_observed_result_backs_it(self) -> None:
        unbacked = _launch_artifacts()
        unbacked["readout"] = _readout(analysis_status=_status_input_for("canceled"))
        unbacked["handoff"]["analysis_cancellation"] = _cancellation()
        backed = _launch_artifacts()
        backed["readout"] = _readout(analysis_status=_status_input_for("canceled"))
        backed["handoff"]["analysis_cancellation"] = _cancellation(
            run_ref="analysis_run_activation_q3_01",
            acknowledgement_state="observed_accepted",
            result_state="observed_canceled",
            evidence_refs=("evidence_cancel_accepted", "evidence_run_canceled"),
        )

        self.assertIn("a canceled analysis state requires an observed cancellation result", prepare_lifecycle_growth(unbacked)["hold_reasons"])
        self.assertNotIn("a canceled analysis state requires an observed cancellation result", prepare_lifecycle_growth(backed)["hold_reasons"])

    def test_given_a_cancellation_naming_another_run_when_prepared_then_readiness_holds(self) -> None:
        artifacts = _launch_artifacts()
        artifacts["readout"] = _readout(analysis_status=_status_input_for("running"))
        artifacts["handoff"]["analysis_cancellation"] = _cancellation(run_ref="analysis_run_other")

        hold_reasons = prepare_lifecycle_growth(artifacts)["hold_reasons"]

        self.assertIn("the prepared cancellation targets a different run than the observed analysis", hold_reasons)
        self.assertIn("the latest analysis run is still in flight; reconcile it before preparing another", hold_reasons)

    def test_given_an_observed_cancellation_over_an_in_flight_run_when_prepared_then_the_contradiction_holds(self) -> None:
        artifacts = _launch_artifacts()
        artifacts["readout"] = _readout(analysis_status=_status_input_for("running"))
        artifacts["handoff"]["analysis_cancellation"] = _cancellation(
            run_ref="analysis_run_activation_q3_01",
            acknowledgement_state="observed_accepted",
            result_state="observed_canceled",
            evidence_refs=("evidence_cancel_accepted", "evidence_run_canceled"),
        )

        self.assertIn("an observed cancellation result contradicts an in-flight analysis state", prepare_lifecycle_growth(artifacts)["hold_reasons"])

    def test_given_a_forged_handoff_status_when_validated_then_the_prepared_boundary_is_reported(self) -> None:
        artifacts = _launch_artifacts()
        artifacts["handoff"]["analysis_cancellation"] = deepcopy(_cancellation())
        artifacts["handoff"]["analysis_cancellation"]["handoff_status"] = "observed"

        errors = validate_lifecycle_growth_artifact(artifacts["handoff"])

        self.assertIn("analysis_cancellation.handoff_status must be prepared_not_observed", errors)


class AnalysisReadinessTests(unittest.TestCase):
    def test_given_an_in_flight_analysis_when_prepared_then_no_second_run_is_prepared(self) -> None:
        for state in ("queued", "running"):
            with self.subTest(state=state):
                artifacts = _launch_artifacts()
                artifacts["readout"] = _readout(analysis_status=_status_input_for(state))
                readiness = prepare_lifecycle_growth(artifacts)

                self.assertEqual(readiness["verdict"], "HOLD")
                self.assertFalse(readiness["launch_ready"])
                self.assertIn("the latest analysis run is still in flight; reconcile it before preparing another", readiness["hold_reasons"])

    def test_given_a_completed_analysis_on_a_shipping_readout_when_prepared_then_launch_stays_ready(self) -> None:
        from test_lifecycle_growth_exposure import exposure_inputs
        artifacts = _launch_artifacts()
        from _lifecycle_configuration import bind
        artifacts.update(bind(exposure_inputs()))

        readiness = prepare_lifecycle_growth(artifacts)

        self.assertEqual(readiness["verdict"], "READY")
        self.assertEqual(readiness["hold_reasons"], [])

    def test_given_the_existing_launch_contract_when_prepared_then_older_gates_still_apply(self) -> None:
        artifacts = _launch_artifacts()
        artifacts["experiment"]["holdout_state"] = "unknown"

        self.assertEqual(prepare_lifecycle_growth(artifacts)["verdict"], "HOLD")


def _status_input(record: dict[str, object]) -> dict[str, object]:
    return {
        "run_state": record["run_state"],
        "run_ref": record["run_ref"],
        "observed_at": record["observed_at"],
        "elapsed_minutes": record["elapsed_minutes"],
        "service_expectation_minutes": record["service_expectation_minutes"],
        "evidence_refs": tuple(record["evidence_refs"]),
    }


def _status_input_for(state: str) -> dict[str, object]:
    if state == "not_started":
        return {
            "run_state": state,
            "run_ref": "",
            "observed_at": "2026-09-08T09:00:00Z",
            "elapsed_minutes": 0,
            "service_expectation_minutes": 0,
            "evidence_refs": (),
        }
    return {
        "run_state": state,
        "run_ref": "analysis_run_activation_q3_01",
        "observed_at": "2026-09-08T09:00:00Z",
        "elapsed_minutes": BEYOND_ONE_DAY_MINUTES if state in ("queued", "running") else 42,
        "service_expectation_minutes": 120,
        "evidence_refs": ("evidence_analysis_run",) if state != "unknown" else (),
    }


if __name__ == "__main__":
    unittest.main()
