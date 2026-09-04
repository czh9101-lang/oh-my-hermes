"""Focused tests for the github-issue-intake workflow (issue #1303).

Covers the canonical installable workflow end to end: catalog definition,
EN/KO routing with negative controls, the wrapper card contract, the
privacy-safe ``github_issue_intake/v1`` artifact lifecycle (prepared vs
observed), authority boundaries, and capability/awareness exposure.
"""

from __future__ import annotations

from dataclasses import replace
import inspect
import unittest

from omh.capabilities.families import capability_family_projection
from omh.plugin_bundle.omh import awareness as awareness_module
from omh.routing import chat as chat_module
from omh.skills.catalog import builtin_definitions, harness_definition, skill_exposure_payload
from omh.wrapper.contract import build_chat_interaction_payload


def _definition(name: str):
    for definition in builtin_definitions():
        if definition.name == name:
            return definition
    raise AssertionError(f"catalog definition {name} is missing")


def _intake_module():
    # Lazy import so catalog/routing failures stay distinguishable from a
    # missing artifact module while the lane is RED.
    from omh.wrapper import github_issue_intake

    return github_issue_intake


def _prepared_artifact():
    intake = _intake_module()
    # Given a classified public-chat report with an explicit target repository
    artifact = intake.prepare_issue_intake(
        source_boundary="public_support_chat",
        classification="bug",
        user_visible_problem="omh setup fails on Windows.",
        source_summary="Public support-chat report of a Windows setup failure.",
        desired_outcome="Setup completes on Windows or a documented workaround exists.",
        scope_included=("omh setup failure on Windows",),
        scope_excluded=("code changes", "release work"),
        observed_evidence=("reporter-supplied setup failure description",),
        inference=("possible PATH handling cause",),
        repository="rlaope/oh-my-hermes",
        title="omh setup fails on Windows",
        body="## Problem\nomh setup fails on Windows.\n\n## Evidence\nReporter-supplied description.",
        labels=("bug", "setup"),
        template_fields=(
            ("summary", "omh setup fails on Windows"),
            ("user_goal", "Finish omh setup on Windows."),
            ("reproduce", "1. Run omh setup.\n2. Observe the failure."),
            ("expected", "Setup completes."),
            ("affected_surfaces", "Hermes chat or wrapper"),
            ("observed_evidence", "Observed setup failure."),
            ("acceptance_criteria", "Setup completes on Windows."),
            ("environment", "Windows 11; Python 3.13"),
        ),
    )
    return intake, artifact


def _searched_artifact():
    intake, artifact = _prepared_artifact()
    # Given a completed duplicate search with no matches
    return intake, intake.record_duplicate_search(artifact, ())


def _confirmed_artifact():
    intake, artifact = _searched_artifact()
    # Given an explicit reporter confirmation after the direction check
    return intake, intake.confirm_creation(artifact)


def _dispatched_artifact():
    intake, artifact = _confirmed_artifact()
    handoff = intake.connector_handoff(artifact)
    return intake, handoff.artifact, handoff.request


class CatalogDefinitionTests(unittest.TestCase):
    def test_definition_exists_with_canonical_identity(self) -> None:
        # Given/When: the catalog is loaded
        definition = _definition("github-issue-intake")
        # Then: the workflow keeps the canonical name, category, and exposure
        self.assertEqual(definition.category, "github-ops")
        self.assertEqual(definition.phase, "issue-intake")
        exposure = skill_exposure_payload("github-issue-intake")
        self.assertEqual(exposure["exposure"], "workflow_skill")
        self.assertTrue(exposure["install_visibility"])

    def test_bounded_interview_is_exactly_three_bilingual_questions(self) -> None:
        # Given/When: the catalog definition
        definition = _definition("github-issue-intake")
        # Then: the interview is structurally capped at three decision questions
        self.assertEqual(len(definition.expert_questions), 3)
        topics = {question.required_input for question in definition.expert_questions}
        self.assertEqual(topics, {"desired outcome", "scope boundary", "missing evidence"})
        for question in definition.expert_questions:
            with self.subTest(topic=question.required_input):
                self.assertTrue(question.en.strip())
                self.assertTrue(question.ko.strip())
                self.assertNotEqual(question.en, question.ko)

    def test_safety_rules_encode_authority_and_prepared_vs_observed(self) -> None:
        # Given/When: the catalog definition
        definition = _definition("github-issue-intake")
        rules = " ".join(definition.safety_rules).lower()
        # Then: confirmation, scoped mutation, security redirect, and no-core-call rules exist
        self.assertIn("confirmation", rules)
        self.assertIn("create_issue", " ".join(definition.safety_rules))
        self.assertIn("security", rules)
        self.assertIn("never calls github", rules)
        self.assertIn("read-back", rules)

    def test_expected_outputs_and_lane_boundaries(self) -> None:
        # Given/When: the catalog definition
        definition = _definition("github-issue-intake")
        # Then: the artifact schema and lane separators are declared
        self.assertIn("github_issue_intake/v1", definition.expected_outputs)
        boundaries = " ".join(definition.do_not_use_when)
        self.assertIn("feedback-triage", boundaries)
        self.assertIn("github-event-ops", boundaries)

    def test_harness_separates_prepared_from_observed(self) -> None:
        # Given/When: the harness registry
        harness = harness_definition("github-issue-intake")
        # Then: the artifact event and observed-only ladder step exist
        self.assertIn("github_issue_intake/v1", harness.artifact_events)
        self.assertTrue(any(step.endswith("_when_available") for step in harness.evidence_ladder))
        guards = " ".join(harness.overclaim_guards).lower()
        self.assertIn("not issue creation", guards)


class RoutingTests(unittest.TestCase):
    def _route(self, message: str) -> dict:
        return chat_module.route_chat_message(message, source="discord")

    def test_english_explicit_filing_routes_to_intake(self) -> None:
        # Given: an explicit public-chat filing request
        # When: the chat router decides
        decision = self._route("please file this as an issue: omh setup fails on Windows")
        # Then: the intake lane wins
        self.assertEqual(decision["selected_skill"], "github-issue-intake")

    def test_korean_explicit_filing_routes_to_intake(self) -> None:
        # Given: a Korean explicit filing request
        # When: the chat router decides
        decision = self._route("이 버그를 깃허브 이슈로 올려줘")
        # Then: the intake lane wins
        self.assertEqual(decision["selected_skill"], "github-issue-intake")

    def test_existing_issue_event_stays_in_event_ops(self) -> None:
        # Given: an event for an already-open issue with failing CI
        # When: the chat router decides
        decision = self._route("issue opened with failing ci")
        # Then: the event-ops lane keeps it (negative control)
        self.assertEqual(decision["selected_skill"], "github-event-ops")

    def test_classification_only_report_stays_in_feedback_triage(self) -> None:
        # Given: a classification-only feedback request
        # When: the chat router decides
        decision = self._route("cluster these customer bug reports")
        # Then: the feedback-triage lane keeps it (negative control)
        self.assertEqual(decision["selected_skill"], "feedback-triage")

    def test_issue_to_pr_stays_in_event_ops(self) -> None:
        # Given: an issue-to-PR request for an existing issue
        # When: the chat router decides
        decision = self._route("turn this issue into a pr")
        # Then: the event-ops lane keeps it (negative control)
        self.assertEqual(decision["selected_skill"], "github-event-ops")

    def test_coding_request_bundled_with_filing_stays_out_of_coding_lane(self) -> None:
        # Given: a filing request with an unauthorized coding demand attached
        # When: the chat router decides
        decision = self._route(
            "please file this as an issue: the export button throws, and while you are at it just fix it and open a PR"
        )
        # Then: routing stays in intake; no coding lane starts from a public report
        self.assertEqual(decision["selected_skill"], "github-issue-intake")


class WrapperCardTests(unittest.TestCase):
    def test_card_uses_intake_schema_and_confirmation_gate(self) -> None:
        # Given: an explicit filing request
        # When: the wrapper builds the chat interaction payload
        payload = build_chat_interaction_payload(
            "please file this as an issue: omh setup fails on Windows",
            source="discord",
        )
        # Then: the card carries the intake schema and a disabled create action
        self.assertEqual(payload["route"]["selected_skill"], "github-issue-intake")
        response = payload["chat_response"]
        self.assertEqual(response["state"]["artifact_schema"], "github_issue_intake/v1")
        actions = {str(action["id"]): action for action in response["actions"]}
        self.assertIn("confirm_issue_creation", actions)
        self.assertFalse(actions["confirm_issue_creation"]["enabled"])
        self.assertIn("requires", actions["confirm_issue_creation"]["payload"])

    def test_card_lists_github_mutation_as_not_observed(self) -> None:
        # Given/When: the intake card payload
        payload = build_chat_interaction_payload(
            "please file this as an issue: omh setup fails on Windows",
            source="discord",
        )
        response = payload["chat_response"]
        # Then: GitHub mutation and issue creation stay in the not-evidence list
        not_observed = response["state"]["workflow_explanation"]["not_evidence_yet"]
        self.assertIn("GitHub mutation", not_observed)
        self.assertIn("issue creation", not_observed)
        self.assertIn("GitHub mutation", response["claim_boundary"])

    def test_card_omits_raw_message(self) -> None:
        # Given: a filing request with a sensitive marker
        marker = "raw-transcript-marker-1303"
        # When: the wrapper builds the payload
        payload = build_chat_interaction_payload(
            f"please file this as an issue: {marker}",
            source="discord",
        )
        # Then: the raw message never leaks into the card state
        self.assertNotIn(marker, str(payload["chat_response"]["state"]))


class DirectionCheckTests(unittest.TestCase):
    def test_direction_check_contains_required_structure(self) -> None:
        # Given: a prepared artifact with a completed duplicate search
        intake, artifact = _searched_artifact()
        # When: the structured direction check is presented
        check = intake.direction_check(artifact)
        # Then: it carries type, user-visible problem, source summary, smallest
        # desired outcome, included/excluded scope, observed-vs-inferred
        # evidence, and duplicate status
        self.assertEqual(check.report_type, "bug")
        self.assertEqual(check.user_visible_problem, "omh setup fails on Windows.")
        self.assertEqual(check.source_summary, "Public support-chat report of a Windows setup failure.")
        self.assertEqual(check.smallest_desired_outcome, artifact.desired_outcome)
        self.assertEqual(check.scope_included, artifact.scope_included)
        self.assertEqual(check.scope_excluded, artifact.scope_excluded)
        self.assertEqual(check.observed_evidence, artifact.observed_evidence)
        self.assertEqual(check.inference, artifact.inference)
        self.assertEqual(check.duplicate_status, "none_found")

    def test_direction_check_reflects_duplicate_search_results(self) -> None:
        # Given: a prepared artifact before any duplicate search
        intake, artifact = _prepared_artifact()
        # When/Then: the direction check reports the search as not run
        self.assertEqual(intake.direction_check(artifact).duplicate_status, "not_searched")
        # And: a confirmed duplicate is reflected once the search is recorded
        artifact = intake.record_duplicate_search(
            artifact, ("rlaope/oh-my-hermes#1200",), confirmed_duplicate=True
        )
        self.assertEqual(intake.direction_check(artifact).duplicate_status, "confirmed_duplicate")

    def test_prepared_artifact_with_complete_facts_is_confirmation_ready(self) -> None:
        # Given: the standard prepared artifact
        intake, artifact = _prepared_artifact()
        # When/Then: its direction check is complete before confirmation
        self.assertTrue(intake.direction_check_complete(artifact))


class ConfirmationGateTests(unittest.TestCase):
    def test_confirmation_fails_when_direction_check_incomplete(self) -> None:
        # Given: a duplicate-searched artifact that is still unclear
        intake = _intake_module()
        artifact = intake.prepare_issue_intake(
            source_boundary="public_support_chat",
            classification="unclear",
            user_visible_problem="",
            source_summary="Vague public support-chat report.",
            desired_outcome="",
            scope_included=(),
            scope_excluded=(),
            observed_evidence=(),
            inference=(),
            repository="rlaope/oh-my-hermes",
            title="something is broken",
            body="",
            labels=(),
        )
        artifact = intake.record_duplicate_search(artifact, ())
        # When/Then: confirmation is refused until the direction check is complete
        self.assertFalse(intake.direction_check_complete(artifact))
        with self.assertRaises(intake.IssueIntakeError):
            intake.confirm_creation(artifact)
        # And: the same refusal applies to a maintainer file-now
        with self.assertRaises(intake.IssueIntakeError):
            intake.confirm_creation(artifact, maintainer_file_now=True)

    def test_confirmation_fails_without_user_visible_problem(self) -> None:
        # Given: a classified, duplicate-searched artifact missing the
        # user-visible problem statement
        intake = _intake_module()
        artifact = intake.prepare_issue_intake(
            source_boundary="public_support_chat",
            classification="bug",
            user_visible_problem="",
            source_summary="Public support-chat report of a Windows setup failure.",
            desired_outcome="Setup completes on Windows.",
            scope_included=("omh setup failure on Windows",),
            scope_excluded=("code changes",),
            observed_evidence=("reporter-supplied setup failure description",),
            inference=(),
            repository="rlaope/oh-my-hermes",
            title="omh setup fails on Windows",
            body="## Problem\nomh setup fails on Windows.",
            labels=("bug",),
        )
        artifact = intake.record_duplicate_search(artifact, ())
        # When/Then: confirmation is refused
        with self.assertRaises(intake.IssueIntakeError):
            intake.confirm_creation(artifact)

    def test_blocker_prevents_confirmation(self) -> None:
        # Given: a duplicate-searched artifact blocked by a security redirect
        intake, artifact = _searched_artifact()
        artifact = intake.record_connector_blocker(artifact, "security_redirect")
        # When/Then: confirmation is refused while any blocker is active
        with self.assertRaises(intake.IssueIntakeError):
            intake.confirm_creation(artifact)

    def test_blocker_prevents_connector_handoff(self) -> None:
        # Given: a confirmed artifact that later records a blocker
        intake, artifact = _confirmed_artifact()
        for blocker in ("connector_unavailable", "credentials_missing", "security_redirect", "missing_evidence"):
            with self.subTest(blocker=blocker):
                blocked = intake.record_connector_blocker(artifact, blocker)
                # When/Then: handoff is refused while the blocker is active
                with self.assertRaises(intake.IssueIntakeError):
                    intake.connector_handoff(blocked)

    def test_observed_result_cannot_clear_blocker(self) -> None:
        # Given: a confirmed artifact blocked by a missing-evidence stop
        intake, artifact = _confirmed_artifact()
        artifact = intake.record_connector_blocker(artifact, "missing_evidence")
        observed = intake.ObservedIssueResult(
            repository="rlaope/oh-my-hermes",
            author="omh-bot",
            title="omh setup fails on Windows",
            body="## Problem\nomh setup fails on Windows.\n\n## Evidence\nReporter-supplied description.",
            labels=("bug", "setup"),
            url="https://github.com/rlaope/oh-my-hermes/issues/1303",
        )
        # When/Then: recording an observed result is refused and the blocker stands
        with self.assertRaises(intake.IssueIntakeError):
            intake.record_observed_result(artifact, observed)
        self.assertEqual(artifact.blocker, "missing_evidence")
        self.assertIsNone(artifact.observed_result)


class ReadBackLabelTests(unittest.TestCase):
    def _observed_with_labels(self, intake, request, labels: tuple[str, ...]):
        return intake.ObservedIssueResult(
            repository=request.repository,
            author="omh-bot",
            title=request.title,
            body=request.body,
            labels=labels,
            url="https://github.com/rlaope/oh-my-hermes/issues/1303",
            idempotency_key=request.idempotency_key,
        )

    def test_reordered_labels_still_verify(self) -> None:
        # Given: a confirmed artifact whose read-back returns reordered labels
        intake, artifact, request = _dispatched_artifact()
        artifact = intake.record_observed_result(
            artifact, self._observed_with_labels(intake, request, ("needs-triage", "bug"))
        )
        # When: the read-back is verified
        verification = intake.verify_read_back(artifact)
        # Then: label order does not matter
        self.assertTrue(verification.verified)
        self.assertNotIn("labels", verification.mismatches)

    def test_different_label_set_fails_verification(self) -> None:
        # Given: a confirmed artifact whose read-back drops one label
        intake, artifact, request = _dispatched_artifact()
        artifact = intake.record_observed_result(
            artifact, self._observed_with_labels(intake, request, ("bug",))
        )
        # When: the read-back is verified
        verification = intake.verify_read_back(artifact)
        # Then: the label set must match exactly
        self.assertFalse(verification.verified)
        self.assertIn("labels", verification.mismatches)

    def test_extra_label_fails_verification(self) -> None:
        # Given: a confirmed artifact whose read-back adds a label
        intake, artifact, request = _dispatched_artifact()
        artifact = intake.record_observed_result(
            artifact, self._observed_with_labels(intake, request, ("bug", "needs-triage", "extra"))
        )
        # When/Then: the extra label is an exact-set mismatch
        self.assertFalse(intake.verify_read_back(artifact).verified)


class ArtifactLifecycleTests(unittest.TestCase):
    def test_prepared_package_records_intent_without_claiming_creation(self) -> None:
        # Given/When: a prepared intake artifact
        intake, artifact = _prepared_artifact()
        # Then: intent, authority, and approval are recorded; nothing is observed
        self.assertEqual(artifact.schema, "github_issue_intake/v1")
        self.assertEqual(artifact.intended_mutation, "create_issue")
        self.assertEqual(artifact.repository, "rlaope/oh-my-hermes")
        self.assertEqual(artifact.approval_authority, "public_reporter_scoped")
        self.assertEqual(artifact.confirmation_state, "pending")
        self.assertIsNone(artifact.observed_result)
        self.assertFalse(intake.creation_verified(artifact))
        # And: the outcome copy refuses to claim filing
        self.assertIn("No issue was filed", intake.outcome_summary(artifact))

    def test_artifact_fields_stay_inside_privacy_allowlist(self) -> None:
        # Given/When: a prepared artifact serialized for the wrapper
        intake, artifact = _prepared_artifact()
        data = artifact.to_dict()
        # Then: only allow-listed metadata keys exist - no transcripts, prompts,
        # credentials, or private logs
        self.assertLessEqual(set(data), intake.ARTIFACT_FIELD_ALLOWLIST)
        for key in data:
            with self.subTest(key=key):
                self.assertNotIn("transcript", key)
                self.assertNotIn("prompt", key)
                self.assertNotIn("credential", key)
                self.assertNotIn("token", key)

    def test_bounded_interview_skips_established_facts_and_caps_at_three(self) -> None:
        # Given: an interview where the desired outcome is already established
        intake = _intake_module()
        # When: the remaining decision questions are computed
        questions = intake.bounded_interview(
            established=("desired outcome",),
            desired_outcome="Setup completes on Windows.",
        )
        # Then: at most three questions remain and the established one is skipped
        self.assertLessEqual(len(questions), 3)
        self.assertNotIn("desired outcome", questions)
        self.assertIn("scope boundary", questions)
        self.assertIn("missing evidence", questions)

    def test_missing_evidence_stops_with_specific_request(self) -> None:
        # Given: an unclear report with no observed evidence
        intake = _intake_module()
        artifact = intake.prepare_issue_intake(
            source_boundary="public_support_chat",
            classification="unclear",
            user_visible_problem="",
            source_summary="Vague public support-chat report.",
            desired_outcome="",
            scope_included=(),
            scope_excluded=(),
            observed_evidence=(),
            inference=(),
            repository="rlaope/oh-my-hermes",
            title="something is broken",
            body="",
            labels=(),
        )
        # When: the workflow asks for what is missing
        request = intake.missing_evidence_request(artifact)
        # Then: it names specific evidence instead of filing a vague issue
        self.assertIn("desired outcome", request)
        self.assertIn("evidence", request)

    def test_duplicate_match_is_recorded_and_blocks_handoff(self) -> None:
        # Given: a prepared artifact
        intake, artifact = _prepared_artifact()
        # When: a duplicate search confirms an existing issue
        artifact = intake.record_duplicate_search(artifact, ("rlaope/oh-my-hermes#1200",), confirmed_duplicate=True)
        # Then: the duplicate status is recorded and creation is refused
        self.assertEqual(artifact.duplicate_status, "confirmed_duplicate")
        self.assertEqual(artifact.duplicate_refs, ("rlaope/oh-my-hermes#1200",))
        with self.assertRaises(intake.IssueIntakeError):
            intake.confirm_creation(artifact)

    def test_creation_requires_confirmation_and_duplicate_search(self) -> None:
        # Given: a prepared artifact with no duplicate search and no confirmation
        intake, artifact = _prepared_artifact()
        # When/Then: confirmation before duplicate search is refused
        with self.assertRaises(intake.IssueIntakeError):
            intake.confirm_creation(artifact)
        # And: handoff before confirmation is refused even after the search
        searched = intake.record_duplicate_search(artifact, ())
        with self.assertRaises(intake.IssueIntakeError):
            intake.connector_handoff(searched)

    def test_maintainer_file_now_skips_interview_but_not_search_or_readback(self) -> None:
        # Given: a prepared artifact and a maintainer "file now" instruction
        intake, artifact = _prepared_artifact()
        artifact = intake.record_duplicate_search(artifact, ())
        artifact = intake.confirm_creation(
            artifact,
            maintainer_observation=intake.AuthenticatedMaintainerObservation(
                actor_id="maintainer:1303", evidence_ref="wrapper-auth:1303"
            ),
        )
        # Then: the confirmation state records the maintainer bypass
        self.assertEqual(artifact.confirmation_state, "maintainer_file_now")
        # And: the duplicate search was still required (it ran above) and the
        # read-back still gates any success claim
        self.assertFalse(intake.creation_verified(artifact))
        self.assertIn("No issue was filed", intake.outcome_summary(artifact))

    def test_connector_unavailable_returns_package_plus_blocker(self) -> None:
        # Given: a confirmed artifact but no connector or credentials
        intake, artifact = _confirmed_artifact()
        artifact = intake.record_connector_blocker(artifact, "connector_unavailable")
        # Then: the complete package survives with an explicit blocker and no filing claim
        self.assertEqual(artifact.blocker, "connector_unavailable")
        self.assertIsNotNone(artifact.connector_request)
        self.assertEqual(artifact.connector_request.repository, "rlaope/oh-my-hermes")
        self.assertIsNone(artifact.observed_result)
        self.assertFalse(intake.creation_verified(artifact))
        summary = intake.outcome_summary(artifact)
        self.assertIn("connector_unavailable", summary)
        self.assertIn("No issue was filed", summary)

    def test_read_back_verifies_observed_result(self) -> None:
        # Given: a dispatched artifact and a connector-observed creation result
        intake, artifact, request = _dispatched_artifact()
        artifact = intake.record_observed_result(
            artifact,
            intake.ObservedIssueResult(
                repository=request.repository,
                author="omh-bot",
                title=request.title,
                body=request.body,
                labels=request.labels,
                url="https://github.com/rlaope/oh-my-hermes/issues/1303",
                idempotency_key=request.idempotency_key,
            ),
        )
        # When: the read-back is verified
        verification = intake.verify_read_back(artifact)
        # Then: every read-back field matches and creation is verified
        self.assertTrue(verification.verified)
        self.assertEqual(verification.mismatches, ())
        self.assertEqual(set(verification.checked_fields), set(intake.READ_BACK_FIELDS))
        self.assertTrue(intake.creation_verified(artifact))
        self.assertIn("1303", intake.outcome_summary(artifact))

    def test_read_back_mismatch_fails_verification(self) -> None:
        # Given: a dispatched artifact and an observed result with a wrong title and labels
        intake, artifact, request = _dispatched_artifact()
        artifact = intake.record_observed_result(
            artifact,
            intake.ObservedIssueResult(
                repository=request.repository,
                author="omh-bot",
                title="wrong title",
                body=request.body,
                labels=("bug",),
                url="https://github.com/rlaope/oh-my-hermes/issues/1303",
                idempotency_key=request.idempotency_key,
            ),
        )
        # When: the read-back is verified
        verification = intake.verify_read_back(artifact)
        # Then: the mismatched fields are named and creation stays unverified
        self.assertFalse(verification.verified)
        self.assertIn("title", verification.mismatches)
        self.assertIn("labels", verification.mismatches)
        self.assertFalse(intake.creation_verified(artifact))

    def test_public_reporter_authorizes_only_scoped_issue_creation(self) -> None:
        # Given: the authority model
        intake = _intake_module()
        # When/Then: a public reporter can authorize exactly one scoped create_issue
        self.assertTrue(intake.mutation_authorized("create_issue", authority="public_reporter_scoped"))
        # And: every other mutation is refused for both public and maintainer
        # authority inside this workflow (they belong to their own lanes)
        for mutation in (
            "edit_code",
            "change_settings",
            "create_branch",
            "commit",
            "open_pr",
            "merge",
            "deploy",
            "start_coding_executor",
        ):
            with self.subTest(mutation=mutation):
                self.assertFalse(intake.mutation_authorized(mutation, authority="public_reporter_scoped"))
                self.assertFalse(intake.mutation_authorized(mutation, authority="maintainer"))

    def test_security_reports_redirect_to_private_path(self) -> None:
        # Given: a security-sensitive report
        intake = _intake_module()
        # When/Then: the workflow flags the private security-reporting redirect
        self.assertTrue(intake.security_redirect_required(("vulnerability",)))
        self.assertTrue(intake.security_redirect_required(("exploit",)))
        self.assertFalse(intake.security_redirect_required(("crash on startup",)))

    def test_core_module_makes_no_network_calls(self) -> None:
        # Given/When: the artifact module source
        intake = _intake_module()
        source = inspect.getsource(intake)
        # Then: core OMH contains no HTTP/GitHub provider call surface
        for forbidden in ("urllib", "requests", "httpx", "socket", "urlopen", "urlretrieve"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


class CapabilityAndAwarenessTests(unittest.TestCase):
    def test_capability_family_membership(self) -> None:
        # Given/When: the capability family projection
        projection = capability_family_projection()
        families = {family["id"]: family for family in projection["families"]}
        # Then: the workflow joins the operate-and-observe family
        self.assertIn("github-issue-intake", families["operate_and_observe"]["primary_workflows"])
        self.assertEqual(projection["workflow_to_family"]["github-issue-intake"], "operate_and_observe")

    def test_awareness_context_card_mapping(self) -> None:
        # Given/When: the awareness lane mapping and context cards
        lane_by_workflow = awareness_module._WORKFLOW_CONTEXT_CARD_BY_WORKFLOW
        cards = awareness_module.WORKFLOW_CONTEXT_CARDS
        # Then: the workflow is mapped and listed with prepared-only copy
        self.assertEqual(lane_by_workflow["github-issue-intake"], "automation_and_status")
        card = next(card for card in cards if card["id"] == "automation_and_status")
        self.assertIn("github-issue-intake", card["representative_workflows"])

    def test_awareness_next_action_is_prepare_only(self) -> None:
        # Given/When: the direct workflow next-action map
        next_actions = awareness_module._DIRECT_WORKFLOW_NEXT_ACTIONS
        # Then: the intake lane prepares; it never executes a GitHub mutation
        self.assertEqual(next_actions["github-issue-intake"], "prepare_github_issue_intake")


class ReviewerRegressionTests(unittest.TestCase):
    def test_security_in_any_content_creates_irreversible_redirect(self) -> None:
        # Given: a security disclosure hidden in inferred report content
        intake, artifact = _prepared_artifact()
        # When: the intake is prepared with security-sensitive inference
        secured = intake.prepare_issue_intake(
            source_boundary=artifact.source_boundary,
            classification=artifact.classification,
            user_visible_problem=artifact.user_visible_problem,
            source_summary=artifact.source_summary,
            desired_outcome=artifact.desired_outcome,
            scope_included=artifact.scope_included,
            scope_excluded=artifact.scope_excluded,
            observed_evidence=artifact.observed_evidence,
            inference=("exploit permits account takeover",),
            repository=artifact.repository,
            title=artifact.connector_request.title,
            body=artifact.connector_request.body,
            labels=artifact.connector_request.labels,
        )
        # Then: the public handoff is permanently redirected before confirmation
        self.assertEqual(secured.blocker, "security_redirect")
        with self.assertRaises(intake.IssueIntakeError):
            intake.connector_handoff(secured)

    def test_security_in_every_untrusted_content_position_is_irreversible(self) -> None:
        # Given: security disclosure text placed in each intake content boundary
        intake = _intake_module()
        cases = (
            ("report", "security vulnerability in setup", "omh setup fails on Windows", "A public setup failure report.", ("Reporter observed setup failure.",), ()),
            ("title", "omh setup fails on Windows.", "security vulnerability", "A public setup failure report.", ("Reporter observed setup failure.",), ()),
            ("body", "omh setup fails on Windows.", "omh setup fails on Windows", "exploit details", ("Reporter observed setup failure.",), ()),
            ("evidence", "omh setup fails on Windows.", "omh setup fails on Windows", "A public setup failure report.", ("CVE reproduction evidence",), ()),
            ("inference", "omh setup fails on Windows.", "omh setup fails on Windows", "A public setup failure report.", ("Reporter observed setup failure.",), ("exploit may expose data",)),
        )
        for position, report, title, body, evidence, inference in cases:
            with self.subTest(position=position):
                artifact = intake.prepare_issue_intake(
                    source_boundary="public_support_chat",
                    classification="bug",
                    user_visible_problem=report,
                    source_summary="Public support-chat report.",
                    desired_outcome="Setup completes on Windows.",
                    scope_included=("omh setup",),
                    scope_excluded=("code changes",),
                    observed_evidence=evidence,
                    inference=inference,
                    repository="rlaope/oh-my-hermes",
                    title=title,
                    body=body,
                    labels=("bug",),
                )
                # When/Then: the redirect cannot authorize a public handoff
                self.assertEqual(artifact.blocker, "security_redirect")
                with self.assertRaises(intake.IssueIntakeError):
                    intake.connector_handoff(artifact)

    def test_maintainer_transition_requires_authenticated_wrapper_observation(self) -> None:
        # Given: a complete public package with duplicate search evidence
        intake, artifact = _searched_artifact()
        # When/Then: a boolean file-now request cannot elevate public authority
        with self.assertRaises(intake.IssueIntakeError):
            intake.confirm_creation(artifact, maintainer_file_now=True)
        observed = intake.AuthenticatedMaintainerObservation(
            actor_id="maintainer:42",
            evidence_ref="wrapper-auth:request-42",
        )
        transitioned = intake.confirm_creation(artifact, maintainer_observation=observed)
        self.assertEqual(transitioned.confirmation_state, "maintainer_file_now")

    def test_template_builder_refuses_missing_required_fields_and_uses_template_labels(self) -> None:
        # Given: a bug form missing its required environment field
        intake = _intake_module()
        fields = (
            ("summary", "Windows setup fails"),
            ("user_goal", "Finish setup"),
            ("reproduce", "1. Run omh setup"),
            ("expected", "Setup succeeds"),
            ("affected_surfaces", "Hermes chat or wrapper"),
            ("observed_evidence", "Observed exit status 1"),
            ("acceptance_criteria", "Setup completes"),
        )
        # When/Then: public and maintainer preparation both reject the incomplete form
        for authority in ("public_reporter_scoped", "maintainer"):
            with self.subTest(authority=authority), self.assertRaises(intake.IssueIntakeError):
                intake.build_template_request("bug", "rlaope/oh-my-hermes", fields, authority=authority)
        complete = fields + (("environment", "Windows 11; Python 3.13"),)
        request = intake.build_template_request("bug", "rlaope/oh-my-hermes", complete)
        self.assertEqual(request.labels, ("bug", "needs-triage"))
        self.assertIn("## Summary", request.body)
        self.assertIn("## Complete user goal", request.body)

    def test_documentation_and_setup_classifications_use_the_nearest_checked_form(self) -> None:
        intake = _intake_module()
        bug_fields = (
            ("summary", "Setup fails"),
            ("user_goal", "Finish setup"),
            ("reproduce", "1. Run omh setup"),
            ("expected", "Setup succeeds"),
            ("affected_surfaces", "Hermes chat or wrapper"),
            ("observed_evidence", "Observed exit status 1"),
            ("acceptance_criteria", "Setup completes"),
            ("environment", "Windows 11"),
        )
        feature_fields = (
            ("problem", "The setup guide omits one required step."),
            ("user_goal", "Complete setup from the guide."),
            ("affected_surfaces", "Hermes chat or wrapper"),
            ("proposal", "Document the missing step."),
            ("validation", "A fresh setup follows the guide successfully."),
        )

        setup = intake.build_template_request(
            "setup_support", "rlaope/oh-my-hermes", bug_fields
        )
        docs = intake.build_template_request(
            "documentation_gap", "rlaope/oh-my-hermes", feature_fields
        )

        self.assertTrue(intake.template_request_authorized(setup, "setup_support"))
        self.assertEqual(setup.labels, ("bug", "needs-triage"))
        self.assertTrue(setup.title.startswith("[Bug]:"))
        self.assertTrue(intake.template_request_authorized(docs, "documentation_gap"))
        self.assertEqual(docs.labels, ("enhancement", "needs-triage"))
        self.assertTrue(docs.title.startswith("[Feature]:"))

    def test_idempotency_key_is_not_the_guessable_template_content_digest(self) -> None:
        intake = _intake_module()
        fields = (
            ("problem", "A short predictable problem."),
            ("user_goal", "A short predictable goal."),
            ("affected_surfaces", "Hermes chat or wrapper"),
            ("proposal", "Add the requested behavior."),
            ("validation", "The behavior is observed."),
        )

        first = intake.build_template_request(
            "feature_request", "rlaope/oh-my-hermes", fields
        )
        second = intake.build_template_request(
            "feature_request", "rlaope/oh-my-hermes", fields
        )

        self.assertNotEqual(first.idempotency_key, second.idempotency_key)
        self.assertTrue(intake.template_request_authorized(first, "feature_request"))
        self.assertTrue(intake.template_request_authorized(second, "feature_request"))
        self.assertFalse(
            intake.template_request_authorized(
                replace(first, idempotency_key="0" * 64),
                "feature_request",
            )
        )

    def test_projection_is_metadata_only_and_connector_readback_binds_request(self) -> None:
        # Given: a confirmed template-compliant package
        intake, artifact = _confirmed_artifact()
        handoff = intake.connector_handoff(artifact)
        request = handoff.request
        # When: persisted projection is created
        projection = artifact.to_dict()
        # Then: raw request contents are absent while the request key is retained
        serialized = str(projection)
        self.assertNotIn(request.title, serialized)
        self.assertNotIn(request.body, serialized)
        self.assertEqual(projection["connector_request"]["idempotency_key"], request.idempotency_key)
        self.assertEqual(handoff.artifact.handoff_state, "dispatched")
        wrong = intake.ObservedIssueResult(
            repository=request.repository,
            author="omh-bot",
            title=request.title,
            body=request.body,
            labels=request.labels,
            url="https://github.com/rlaope/oh-my-hermes/issues/1303",
            idempotency_key="different-request",
        )
        with self.assertRaises(intake.IssueIntakeError):
            intake.record_observed_result(handoff.artifact, wrong)

    def test_arbitrary_complete_content_stays_prepared_and_cannot_handoff(self) -> None:
        # Given: plain title, body, and custom labels outside a checked-in form
        intake = _intake_module()
        artifact = intake.prepare_issue_intake(
            source_boundary="public_support_chat",
            classification="bug",
            user_visible_problem="omh setup fails on Windows.",
            source_summary="Public support-chat report of a Windows setup failure.",
            desired_outcome="Setup completes on Windows.",
            scope_included=("omh setup failure on Windows",),
            scope_excluded=("code changes",),
            observed_evidence=("Reporter observed setup failure.",),
            inference=(),
            repository="rlaope/oh-my-hermes",
            title="Plain title",
            body="Custom body outside the required issue form.",
            labels=("custom-label",),
        )
        artifact = intake.record_duplicate_search(artifact, ())
        # When/Then: arbitrary complete content cannot become a connector write
        self.assertIsNone(artifact.connector_request)
        with self.assertRaises(intake.IssueIntakeError):
            intake.confirm_creation(artifact)
        with self.assertRaises(intake.IssueIntakeError):
            intake.connector_handoff(artifact)

    def test_handoff_advances_to_terminal_state_and_observed_result_consumes_it(self) -> None:
        # Given: one confirmed template-compliant request
        intake, artifact = _confirmed_artifact()
        # When: the connector receives its single authorized handoff
        handoff = intake.connector_handoff(artifact)
        replay = intake.connector_handoff(artifact)
        observed = intake.ObservedIssueResult(
            repository=handoff.request.repository,
            author="omh-bot",
            title=handoff.request.title,
            body=handoff.request.body,
            labels=handoff.request.labels,
            url="https://github.com/rlaope/oh-my-hermes/issues/1303",
            idempotency_key=handoff.request.idempotency_key,
        )
        recorded = intake.record_observed_result(handoff.artifact, observed)
        # Then: dispatched and observed artifacts reject every further handoff/read-back
        self.assertEqual(handoff.request.idempotency_key, replay.request.idempotency_key)
        self.assertEqual(handoff.artifact.handoff_state, "dispatched")
        self.assertEqual(recorded.handoff_state, "observed")
        with self.assertRaises(intake.IssueIntakeError):
            intake.connector_handoff(handoff.artifact)
        with self.assertRaises(intake.IssueIntakeError):
            intake.connector_handoff(recorded)
        with self.assertRaises(intake.IssueIntakeError):
            intake.record_observed_result(recorded, observed)


if __name__ == "__main__":
    unittest.main()
