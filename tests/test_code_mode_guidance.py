"""Contracts for the code-mode discipline annotation of execute_code results.

The transform_tool_result seam appends one bounded guidance block to the
first ``execute_code`` result of a session, under its own JSON key so the
host's result shape stays parseable. Everything else — other tools, non-JSON
results, already-annotated results, already-served sessions — passes through
untouched (None), because the seam replaces the result the model reads.
"""

import json
import unittest

from omh.plugin_bundle.omh.code_mode_guidance import (
    CODE_MODE_DISCIPLINE_RULES,
    CODE_MODE_GUIDANCE_KEY,
    annotate_execute_code_result,
    code_mode_guidance_text,
    _reset_delivery_state,
)
from omh.plugin_bundle.omh.hooks.result_transforms import transform_tool_result

EXECUTE_CODE_RESULT = json.dumps(
    {
        "status": "success",
        "output": "12 rows\n",
        "exit_code": 0,
        "tool_calls_made": 3,
        "duration_seconds": 1.2,
    }
)

DIFF_RESULT = (
    "--- a/plan.md\n"
    "+++ b/plan.md\n"
    "@@ -1,2 +1,2 @@\n"
    " context stays\n"
    "-short\n"
    "+the replacement line is much longer\n"
)


class GuidanceTextTest(unittest.TestCase):
    def test_every_discipline_rule_is_rendered(self):
        text = code_mode_guidance_text()
        for rule in CODE_MODE_DISCIPLINE_RULES:
            self.assertIn(rule, text)

    def test_names_the_failure_modes_it_guards(self):
        text = code_mode_guidance_text()
        self.assertIn("exit_code 0 is not verified success", text)
        self.assertIn("Never swallow failures", text)
        self.assertIn("dry run", text)
        self.assertIn("fresh", text)
        self.assertIn("Persist intermediate state to files", text)

    def test_stays_bounded(self):
        self.assertLess(len(code_mode_guidance_text()), 1200)


class AnnotateExecuteCodeResultTest(unittest.TestCase):
    def setUp(self):
        _reset_delivery_state()

    def test_first_result_of_a_session_gains_the_guidance_key(self):
        annotated = annotate_execute_code_result(
            tool_name="execute_code",
            result=EXECUTE_CODE_RESULT,
            session_id="session-a",
        )
        self.assertIsNotNone(annotated)
        parsed = json.loads(annotated)
        self.assertEqual(parsed[CODE_MODE_GUIDANCE_KEY], code_mode_guidance_text())
        # Original result fields survive untouched.
        self.assertEqual(parsed["exit_code"], 0)
        self.assertEqual(parsed["output"], "12 rows\n")

    def test_second_result_of_the_same_session_passes_through(self):
        annotate_execute_code_result(
            tool_name="execute_code",
            result=EXECUTE_CODE_RESULT,
            session_id="session-a",
        )
        self.assertIsNone(
            annotate_execute_code_result(
                tool_name="execute_code",
                result=EXECUTE_CODE_RESULT,
                session_id="session-a",
            )
        )

    def test_a_different_session_is_served_independently(self):
        annotate_execute_code_result(
            tool_name="execute_code",
            result=EXECUTE_CODE_RESULT,
            session_id="session-a",
        )
        self.assertIsNotNone(
            annotate_execute_code_result(
                tool_name="execute_code",
                result=EXECUTE_CODE_RESULT,
                session_id="session-b",
            )
        )

    def test_other_tools_pass_through(self):
        self.assertIsNone(
            annotate_execute_code_result(
                tool_name="terminal",
                result=EXECUTE_CODE_RESULT,
                session_id="session-a",
            )
        )

    def test_non_json_and_non_object_results_pass_through(self):
        for result in ("plain text output", json.dumps([1, 2]), "", None):
            with self.subTest(result=result):
                self.assertIsNone(
                    annotate_execute_code_result(
                        tool_name="execute_code",
                        result=result,
                        session_id="session-a",
                    )
                )

    def test_already_annotated_results_pass_through(self):
        parsed = json.loads(EXECUTE_CODE_RESULT)
        parsed[CODE_MODE_GUIDANCE_KEY] = "already here"
        self.assertIsNone(
            annotate_execute_code_result(
                tool_name="execute_code",
                result=json.dumps(parsed),
                session_id="session-a",
            )
        )

    def test_declined_results_do_not_consume_the_session_claim(self):
        # A non-JSON result must not burn the one delivery the session gets.
        annotate_execute_code_result(
            tool_name="execute_code",
            result="not json",
            session_id="session-a",
        )
        self.assertIsNotNone(
            annotate_execute_code_result(
                tool_name="execute_code",
                result=EXECUTE_CODE_RESULT,
                session_id="session-a",
            )
        )

    def test_guidance_never_echoes_result_content(self):
        secret_result = json.dumps(
            {"status": "success", "output": "secret-token-123", "exit_code": 0}
        )
        annotated = annotate_execute_code_result(
            tool_name="execute_code",
            result=secret_result,
            session_id="session-a",
        )
        parsed = json.loads(annotated)
        self.assertNotIn("secret-token-123", parsed[CODE_MODE_GUIDANCE_KEY])


class ComposedTransformTest(unittest.TestCase):
    def setUp(self):
        _reset_delivery_state()

    def test_execute_code_results_are_annotated(self):
        transformed = transform_tool_result(
            tool_name="execute_code",
            result=EXECUTE_CODE_RESULT,
            session_id="session-a",
        )
        self.assertIsNotNone(transformed)
        self.assertIn(CODE_MODE_GUIDANCE_KEY, json.loads(transformed))

    def test_diff_results_still_get_band_padding(self):
        padded = transform_tool_result(
            tool_name="patch",
            result=DIFF_RESULT,
            session_id="session-a",
        )
        self.assertIsNotNone(padded)
        widths = {
            len(line)
            for line in padded.splitlines()
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        }
        self.assertEqual(len(widths), 1)

    def test_irrelevant_results_pass_through(self):
        self.assertIsNone(
            transform_tool_result(
                tool_name="web_search",
                result=json.dumps({"data": {"web": []}}),
                session_id="session-a",
            )
        )


if __name__ == "__main__":
    unittest.main()
