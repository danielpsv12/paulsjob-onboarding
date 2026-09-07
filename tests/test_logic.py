"""Tests for the logic that does not need the API.

These cover the parts that decide what gets sent and what counts as a problem:
reference resolution, payload shaping, the local validation rules, and the
verification. The HTTP client itself is exercised against the live API by
`onboard.py doctor`, which is why it is not mocked here.

Run: py -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from paulsjob import pipeline  # noqa: E402
from paulsjob.errors import ConfigError, from_status  # noqa: E402
from paulsjob.jobs import _description, build_payload, job_id_of  # noqa: E402
from paulsjob.reference import ReferenceData  # noqa: E402
from paulsjob.templates import validate_definition  # noqa: E402


class FakeReference(ReferenceData):
    """Reference data with fixed contents, so no network is needed."""

    def __init__(self):
        super().__init__(client=None)
        self._sets = {
            "job_categories": [{"ID": "IT", "Name": "IT"}],
            "career_levels": [{"ID": "SeniorLevel", "Name": "Oberstufe"}],
            "employment_types": [{"ID": "10", "Name": "Werkstudent"}],
            "working_hours": [
                {"ID": "FullTime", "Name": "Vollzeit"},
                {"ID": "PartTime", "Name": "Teilzeit"},
            ],
            "step_categories": [
                {"ID": "New", "Name": "New", "NameMap": {"de": "Neu"}},
                {"ID": "PreScreening", "Name": "Pre-Screening", "NameMap": {"de": "Vorauswahl"}},
                {"ID": "AIVoiceInterview", "Name": "AI Voice Interview", "NameMap": {"de": "KI-Sprachinterview"}},
            ],
        }


class ReferenceResolution(unittest.TestCase):
    def setUp(self):
        self.reference = FakeReference()

    def test_resolves_by_id(self):
        self.assertEqual(self.reference.resolve("working_hours", "FullTime"), "FullTime")

    def test_resolves_by_name(self):
        self.assertEqual(self.reference.resolve("working_hours", "Vollzeit"), "FullTime")

    def test_resolves_case_insensitively(self):
        self.assertEqual(self.reference.resolve("working_hours", "vollzeit"), "FullTime")

    def test_resolves_localised_step_category(self):
        # Both the ID and the German label must reach the same category.
        self.assertEqual(self.reference.resolve("step_categories", "Vorauswahl"), "PreScreening")
        self.assertEqual(self.reference.resolve("step_categories", "PreScreening"), "PreScreening")

    def test_unknown_value_lists_the_valid_options(self):
        with self.assertRaises(ConfigError) as caught:
            self.reference.resolve("working_hours", "Vollzeit (40h)")
        message = str(caught.exception)
        self.assertIn("not a valid value", message)
        self.assertIn("FullTime", message)
        self.assertIn("PartTime", message)


class DescriptionShaping(unittest.TestCase):
    def test_plain_text_becomes_job_requirements(self):
        # The API takes an object, not a string - a customer export has a string.
        self.assertEqual(_description("Wir suchen..."), {"JobRequirements": "Wir suchen..."})

    def test_structured_block_maps_all_three_fields(self):
        result = _description(
            {"requirements": "R", "ideal_candidate": "I", "about_company": "A"}
        )
        self.assertEqual(result, {"JobRequirements": "R", "IdealCandidateProfile": "I", "AboutCompany": "A"})

    def test_missing_requirements_is_rejected(self):
        with self.assertRaises(ConfigError):
            _description({"about_company": "A"})


class JobPayload(unittest.TestCase):
    def setUp(self):
        self.reference = FakeReference()
        self.job = {
            "external_id": "BSP-1",
            "title": "Backend Engineer",
            "description": "Anforderungen",
            "career_level": "Oberstufe",
            "working_hours": "Vollzeit",
        }

    def test_resolves_words_to_ids(self):
        payload = build_payload(self.job, self.reference, person_slug="slug-1")
        self.assertEqual(payload["JobCareerLevelID"], "SeniorLevel")
        self.assertEqual(payload["WorkingHoursID"], "FullTime")

    def test_sets_job_owner_because_an_api_key_has_no_creator(self):
        payload = build_payload(self.job, self.reference, person_slug="slug-1")
        self.assertEqual(payload["HiringManagers"], [{"PersonSlug": "slug-1", "IsJobOwner": True}])

    def test_indexing_is_off_unless_asked_for(self):
        # Indexing defaults to true in the API and sends the job to the AI
        # indexer immediately; a demo run should not do that silently.
        self.assertFalse(build_payload(self.job, self.reference)["Indexing"])
        self.job["indexing"] = True
        self.assertTrue(build_payload(self.job, self.reference)["Indexing"])

    def test_missing_required_field_is_named(self):
        del self.job["title"]
        with self.assertRaises(ConfigError) as caught:
            build_payload(self.job, self.reference)
        self.assertIn("title", str(caught.exception))


class JobIdSelection(unittest.TestCase):
    def test_prefers_paulsjob_job_id(self):
        # JobPositionID is a per-company counter and produces 404 in a URL.
        record = {"PaulsjobJobID": 182681, "JobPositionID": 1, "JobExternalID": "BSP-1"}
        self.assertEqual(job_id_of(record), 182681)

    def test_falls_back_when_absent(self):
        self.assertEqual(job_id_of({"JobPositionID": 7}), 7)
        self.assertIsNone(job_id_of({}))


class DefinitionValidation(unittest.TestCase):
    def setUp(self):
        self.reference = FakeReference()
        self.definition = {
            "name": "T",
            "steps": [{"name": "Vorauswahl", "category": "PreScreening", "agent": {
                "name": "A", "type": "proactive", "human_in_loop": "always_off",
                "instructions": "tu etwas",
            }}],
        }

    def test_accepts_a_valid_definition(self):
        validate_definition(self.definition, self.reference)

    def test_conditional_requires_a_condition(self):
        self.definition["steps"][0]["agent"]["human_in_loop"] = "conditional"
        with self.assertRaises(ConfigError) as caught:
            validate_definition(self.definition, self.reference)
        self.assertIn("requires a 'condition'", str(caught.exception))

    def test_condition_without_conditional_is_rejected(self):
        # The API answers 422 for this; we catch it before anything is created.
        self.definition["steps"][0]["agent"]["condition"] = "wenn X"
        with self.assertRaises(ConfigError) as caught:
            validate_definition(self.definition, self.reference)
        self.assertIn("only allowed when human_in_loop is 'conditional'", str(caught.exception))

    def test_ai_voice_interview_requires_its_additional_fields(self):
        self.definition["steps"][0]["category"] = "AIVoiceInterview"
        with self.assertRaises(ConfigError) as caught:
            validate_definition(self.definition, self.reference)
        self.assertIn("GreetingInstructions", str(caught.exception))


class Verification(unittest.TestCase):
    def setUp(self):
        self.definition = {
            "steps": [
                {"name": "Neu", "category": "New"},
                {
                    "name": "Vorauswahl",
                    "category": "PreScreening",
                    "agent": {
                        "name": "Vorauswahl-Agent",
                        "type": "proactive",
                        "human_in_loop": "conditional",
                        "instructions": "prüfe die Kriterien",
                    },
                },
            ]
        }
        self.state = {
            "template_status": {"AllowPipelineTemplateOverride": False},
            "steps": [
                {"Name": "Neu", "Category": {"ID": "New"}, "_agents": []},
                {
                    "Name": "Vorauswahl",
                    "Category": {"ID": "PreScreening"},
                    "_agents": [
                        {
                            "Name": "Vorauswahl-Agent",
                            "AgentType": "proactive",
                            "IsActive": True,
                            "HumanInLoop": {"Setting": "conditional"},
                            "Instructions": {"SystemPrompt": "prüfe die Kriterien"},
                        }
                    ],
                },
            ],
        }

    def test_matching_pipeline_has_no_findings(self):
        self.assertEqual(pipeline.verify(self.state, self.definition), [])

    def test_missing_step_is_critical(self):
        self.state["steps"] = self.state["steps"][:1]
        findings = pipeline.verify(self.state, self.definition)
        self.assertIn("step_missing", [f["check"] for f in findings])
        self.assertEqual(pipeline.worst_severity(findings), pipeline.CRITICAL)

    def test_missing_agent_is_critical(self):
        self.state["steps"][1]["_agents"] = []
        findings = pipeline.verify(self.state, self.definition)
        self.assertIn("agent_missing", [f["check"] for f in findings])

    def test_inactive_agent_is_critical(self):
        self.state["steps"][1]["_agents"][0]["IsActive"] = False
        findings = pipeline.verify(self.state, self.definition)
        self.assertIn("agent_inactive", [f["check"] for f in findings])

    def test_changed_prompt_is_a_warning(self):
        self.state["steps"][1]["_agents"][0]["Instructions"]["SystemPrompt"] = "etwas anderes"
        findings = pipeline.verify(self.state, self.definition)
        prompt_findings = [f for f in findings if f["check"] == "agent_prompt"]
        self.assertEqual(len(prompt_findings), 1)
        self.assertEqual(prompt_findings[0]["severity"], pipeline.WARNING)

    def test_hidden_step_is_reported(self):
        self.state["steps"][1]["IsHidden"] = True
        findings = pipeline.verify(self.state, self.definition)
        self.assertIn("step_hidden", [f["check"] for f in findings])

    def test_api_drift_flag_is_surfaced(self):
        self.state["steps"][1]["HasChangedFromTemplate"] = True
        findings = pipeline.verify(self.state, self.definition)
        self.assertIn("step_drifted", [f["check"] for f in findings])

    def test_extra_step_is_informational_only(self):
        self.state["steps"].append({"Name": "Extra", "Category": {"ID": "New"}, "_agents": []})
        findings = pipeline.verify(self.state, self.definition)
        self.assertEqual([f["severity"] for f in findings], [pipeline.INFO])

    def test_unmanaged_template_is_a_warning(self):
        self.state["template_status"]["AllowPipelineTemplateOverride"] = True
        findings = pipeline.verify(self.state, self.definition)
        self.assertIn("template_unmanaged", [f["check"] for f in findings])

    def test_empty_pipeline_short_circuits(self):
        self.state["steps"] = []
        findings = pipeline.verify(self.state, self.definition)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["check"], "steps_exist")


class ErrorMapping(unittest.TestCase):
    def test_status_codes_map_to_distinct_types(self):
        # 401 and 403 must not collapse into one: one means the key is wrong,
        # the other means the key is fine but may not do this.
        self.assertEqual(type(from_status(401, "m", "/p", "GET")).__name__, "AuthError")
        self.assertEqual(type(from_status(403, "m", "/p", "GET")).__name__, "PermissionError_")
        self.assertEqual(type(from_status(404, "m", "/p", "GET")).__name__, "NotFoundError")
        self.assertEqual(type(from_status(422, "m", "/p", "GET")).__name__, "ValidationError")
        self.assertEqual(type(from_status(500, "m", "/p", "GET")).__name__, "ServerError")

    def test_message_carries_the_request_id_for_tracing(self):
        error = from_status(422, "nope", "/p", "POST", request_id="abc-123")
        self.assertIn("abc-123", str(error))


if __name__ == "__main__":
    unittest.main()
