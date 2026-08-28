import importlib.util
import json
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "issue_triage.py"
WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "issue-triage.yml"
SPEC = importlib.util.spec_from_file_location("issue_triage", SCRIPT)
issue_triage = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(issue_triage)


class IssueTriageTests(unittest.TestCase):
    def test_prepare_redacts_untrusted_secrets_and_ignores_bot_output(self):
        long_number = "1" * 15
        payload = {
            "issue": {
                "number": 23,
                "title": "Failure at https://private.example/path?q=value",
                "body": f"Authorization: Bearer demo-secret-value and {long_number}",
                "state": "open",
                "user": {"login": "reporter"},
                "labels": [{"name": "bug"}],
            },
            "comments": [
                {"user": {"login": "person"}, "body": "api_key=demo-value"},
                {"user": {"login": "github-actions[bot]"}, "body": issue_triage.MARKER},
            ],
        }

        result = issue_triage.prepare_context(payload)

        encoded = json.dumps(result)
        self.assertNotIn("private.example", encoded)
        self.assertNotIn("demo-secret-value", encoded)
        self.assertNotIn(long_number, encoded)
        self.assertEqual(len(result["comments"]), 1)

    def test_render_disables_mentions_links_and_html_comments(self):
        long_number = "9" * 14
        result = issue_triage.validate_result(
            {
                "category": "bug",
                "priority": "high",
                "confidence": "medium",
                "summary": (
                    "Ask @maintainer <!-- hidden --> at https://bad.example/ "
                    f"with token=another-demo-value and {long_number}"
                ),
                "analysis": "Likely a bounded failure.",
                "missing_information": ["Provide redacted logs"],
                "recommended_next_steps": ["Confirm the installed version"],
                "needs_human": True,
                "human_reason": "Hardware state must be checked.",
            }
        )

        comment = issue_triage.render_comment(result)
        labels = issue_triage.build_labels(result)

        self.assertIn("@\u200bmaintainer", comment)
        self.assertNotIn("bad.example", comment)
        self.assertNotIn("hidden", comment)
        self.assertNotIn("another-demo-value", comment)
        self.assertNotIn(long_number, comment)
        selected = {label["name"] for label in labels["apply"]}
        self.assertIn("ai-needs-info", selected)
        self.assertIn("ai-needs-human", selected)
        self.assertIn("ai-category:bug", selected)
        self.assertIn("ai-priority:high", selected)
        self.assertNotIn("needs-info", labels["managed"])
        self.assertNotIn("needs-human", labels["managed"])

    def test_gate_allows_one_open_and_only_maintainer_command_for_reruns(self):
        opened = {"event": {"name": "issues", "action": "opened", "actor": "reporter"}}
        edited = {"event": {"name": "issues", "action": "edited", "actor": "reporter"}}
        author_comment = {
            "event": {
                "name": "issue_comment",
                "action": "created",
                "actor": "reporter",
                "comment_body": issue_triage.REANALYZE_COMMAND,
            }
        }
        maintainer_comment = {
            "event": {
                "name": "issue_comment",
                "action": "created",
                "actor": issue_triage.MAINTAINER,
                "comment_body": issue_triage.REANALYZE_COMMAND,
            }
        }

        self.assertTrue(issue_triage.gate_analysis(opened)["should_analyze"])
        self.assertFalse(issue_triage.gate_analysis(edited)["should_analyze"])
        self.assertFalse(issue_triage.gate_analysis(author_comment)["should_analyze"])
        self.assertTrue(issue_triage.gate_analysis(maintainer_comment)["should_analyze"])
        maintainer_comment["event"]["action"] = "edited"
        self.assertFalse(issue_triage.gate_analysis(maintainer_comment)["should_analyze"])

    def test_gate_counts_only_trusted_bot_attempt_markers_and_stops_at_limit(self):
        comments = [
            {
                "user": {"login": "attacker"},
                "body": issue_triage.ATTEMPT_MARKER.format(attempt=99),
            },
            {
                "user": {"login": "github-actions[bot]"},
                "body": issue_triage.ATTEMPT_MARKER.format(
                    attempt=issue_triage.MAX_ATTEMPTS
                ),
            },
        ]
        payload = {
            "event": {
                "name": "issue_comment",
                "action": "created",
                "actor": issue_triage.MAINTAINER,
                "comment_body": issue_triage.REANALYZE_COMMAND,
            },
            "comments": comments,
        }

        gate = issue_triage.gate_analysis(payload)

        self.assertFalse(gate["should_analyze"])
        self.assertEqual(gate["reason"], "attempt-limit-reached")

    def test_success_and_failure_markers_are_separate(self):
        self.assertNotEqual(issue_triage.MARKER, issue_triage.FAILURE_MARKER)
        comment = issue_triage.render_comment(
            {
                "category": "question",
                "priority": "low",
                "confidence": "low",
                "summary": "Summary",
                "analysis": "Analysis",
                "missing_information": [],
                "recommended_next_steps": [],
                "needs_human": False,
                "human_reason": "",
            },
            attempt=2,
        )
        self.assertIn(issue_triage.MARKER, comment)
        self.assertIn(issue_triage.ATTEMPT_MARKER.format(attempt=2), comment)
        self.assertNotIn(issue_triage.FAILURE_MARKER, comment)

    def test_failure_notice_does_not_depend_on_outputs_from_failed_job(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        failure_job = workflow.split("\n  report-failure:\n", 1)[1]

        self.assertIn("if: needs.analyze.result == 'failure'", failure_job)
        self.assertNotIn("needs.analyze.outputs.attempt", failure_job)
        self.assertIn("Math.max(0, ...attempts) + 1", failure_job)

    def test_invalid_enum_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid category"):
            issue_triage.validate_result(
                {
                    "category": "close-immediately",
                    "priority": "low",
                    "confidence": "low",
                    "summary": "",
                    "analysis": "",
                    "missing_information": [],
                    "recommended_next_steps": [],
                    "needs_human": False,
                    "human_reason": "",
                }
            )

    def test_fenced_json_is_accepted(self):
        raw = "```json\n" + json.dumps({"category": "question"}) + "\n```"
        self.assertEqual(issue_triage._extract_json(raw)["category"], "question")


if __name__ == "__main__":
    unittest.main()
