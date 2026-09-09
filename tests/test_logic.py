"""Tests for the logic that does not need the API.

These cover the parts that decide what gets sent and what counts as a problem:
reference resolution, payload shaping, the local validation rules, and the
verification. The HTTP client itself is exercised against the live API by
`onboard.py doctor`, which is why it is not mocked here.

Run: py -m unittest discover -s tests
"""

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from paulsjob import matching, pipeline  # noqa: E402
from paulsjob.client import MAX_ATTEMPTS, PaulsJobClient  # noqa: E402
from paulsjob.cli import _assignment_findings, _definition_named  # noqa: E402
from paulsjob.errors import ConfigError, ServerError, ValidationError, from_status  # noqa: E402
from paulsjob.jobs import _description, build_payload, job_id_of  # noqa: E402
from paulsjob.reference import ReferenceData  # noqa: E402
from paulsjob.templates import (  # noqa: E402
    _normalise_rules,
    _rules_differ,
    _template_updates,
    validate_definition,
)


class FakeReference(ReferenceData):
    """Reference data with fixed contents, so no network is needed."""

    def __init__(self):
        super().__init__(client=None)
        self._sets = {
            "job_categories": [{"ID": "IT", "Name": "IT"}],
            "career_levels": [
                {"ID": "SeniorLevel", "Name": "Oberstufe"},
                {"ID": "EntryLevel", "Name": "Einstiegsniveau"},
            ],
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

    def test_a_renamed_agent_is_reported_with_what_was_found(self):
        # The message has to name the live agent: "expected X" alone leaves the
        # reader guessing whether it was renamed or replaced.
        self.state["steps"][1]["_agents"][0]["Name"] = "Anderer Agent"
        findings = pipeline.verify(self.state, self.definition)
        self.assertEqual(findings[0]["check"], "agent_name")
        self.assertEqual(findings[0]["severity"], pipeline.CRITICAL)
        self.assertIn("Anderer Agent", findings[0]["detail"])

    def test_changed_agent_type_is_a_warning(self):
        self.state["steps"][1]["_agents"][0]["AgentType"] = "reactive"
        findings = pipeline.verify(self.state, self.definition)
        self.assertEqual(findings[0]["check"], "agent_type")
        self.assertEqual(findings[0]["severity"], pipeline.WARNING)

    def test_changed_human_in_loop_is_a_warning(self):
        # This one decides whether a human ever sees the candidate, so it is a
        # deviation worth a conversation even though the agent is present.
        self.state["steps"][1]["_agents"][0]["HumanInLoop"] = {"Setting": "always_off"}
        findings = pipeline.verify(self.state, self.definition)
        self.assertEqual(findings[0]["check"], "agent_human_in_loop")
        self.assertEqual(findings[0]["severity"], pipeline.WARNING)

    def test_an_agent_on_a_step_that_should_have_none_is_critical(self):
        # The mirror of agent_missing: an agent nobody agreed to is talking to
        # candidates. Invisible before this check existed.
        self.state["steps"][0]["_agents"] = [{"Name": "Heimlich", "IsActive": True}]
        findings = pipeline.verify(self.state, self.definition)
        self.assertEqual(findings[0]["check"], "agent_extra")
        self.assertEqual(findings[0]["severity"], pipeline.CRITICAL)
        self.assertIn("Heimlich", findings[0]["detail"])

    def test_a_second_agent_beside_the_agreed_one_is_a_warning(self):
        self.state["steps"][1]["_agents"].append({"Name": "Zweiter", "IsActive": True})
        findings = pipeline.verify(self.state, self.definition)
        self.assertEqual([f["check"] for f in findings], ["agent_extra"])
        self.assertEqual(findings[0]["severity"], pipeline.WARNING)
        self.assertIn("Zweiter", findings[0]["detail"])

    def test_extra_step_is_informational_only(self):
        # An extra step is a column someone added; an extra agent talks to
        # candidates. Only the second one blocks go-live.
        self.state["steps"].append({"Name": "Extra", "Category": {"ID": "New"}, "_agents": []})
        findings = pipeline.verify(self.state, self.definition)
        self.assertEqual([f["check"] for f in findings], ["step_extra"])
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


class MatchRules(unittest.TestCase):
    """The `match:` block a customer writes, turned into what the API matches on."""

    def setUp(self):
        self.reference = FakeReference()

    def build(self, match):
        return matching.build_filter_rules(match, self.reference)

    def test_words_are_resolved_to_ids(self):
        # 'Oberstufe' is what the customer writes; SeniorLevel is what the API
        # will accept - it answers 400 'invalid filter rules' for the word.
        rules = self.build({"career_level": "Oberstufe"})
        self.assertEqual(rules["Must"], [{"Key": "job_career_level_id", "Operator": "is", "Value": "SeniorLevel"}])

    def test_a_list_becomes_an_in_rule(self):
        # "any of these", and each value is resolved on its way in.
        rules = self.build({"career_level": ["Oberstufe", "Einstiegsniveau"]})
        self.assertEqual(rules["Must"][0]["Operator"], "in")
        self.assertEqual(rules["Must"][0]["Value"], ["SeniorLevel", "EntryLevel"])

    def test_fields_are_anded(self):
        rules = self.build({"category": "IT", "career_level": "Oberstufe"})
        self.assertEqual([r["Key"] for r in rules["Must"]], ["job_career_level_id", "job_category_id"])

    def test_no_match_block_means_no_rules(self):
        self.assertIsNone(self.build(None))

    def test_unknown_field_names_the_available_ones(self):
        with self.assertRaises(ConfigError) as caught:
            self.build({"seniority": "Oberstufe"})
        self.assertIn("career_level", str(caught.exception))

    def test_unknown_value_lists_the_valid_options(self):
        with self.assertRaises(ConfigError) as caught:
            self.build({"career_level": "Halbgott"})
        self.assertIn("Oberstufe", str(caught.exception))

    def test_contains_takes_a_single_value(self):
        with self.assertRaises(ConfigError):
            self.build({"title_contains": ["Engineer", "Developer"]})


class LocalMatching(unittest.TestCase):
    """Predicting the match locally, which is what makes a silent fallback visible."""

    def setUp(self):
        reference = FakeReference()
        self.senior = {
            "name": "Tech",
            "_filter_rules": matching.build_filter_rules({"career_level": "Oberstufe", "category": "IT"}, reference),
        }
        self.contains = {
            "name": "Titled",
            "_filter_rules": matching.build_filter_rules({"title_contains": "backend engineer"}, reference),
        }
        self.unrestricted = {"name": "Catch-all", "_filter_rules": None}

    def test_all_must_rules_have_to_hold(self):
        self.assertTrue(matching.matches(self.senior["_filter_rules"], {"JobCareerLevelID": "SeniorLevel", "JobCategoryID": "IT"}))
        self.assertFalse(matching.matches(self.senior["_filter_rules"], {"JobCareerLevelID": "SeniorLevel", "JobCategoryID": "Sales"}))

    def test_missing_field_does_not_match(self):
        self.assertFalse(matching.matches(self.senior["_filter_rules"], {"JobCategoryID": "IT"}))

    def test_contains_is_word_wise_and_case_insensitive(self):
        rules = self.contains["_filter_rules"]
        self.assertTrue(matching.matches(rules, {"JobPositionTitle": "Senior Backend Engineer (m/w/d)"}))
        self.assertFalse(matching.matches(rules, {"JobPositionTitle": "Senior Frontend Developer"}))

    def test_a_template_without_rules_is_never_predicted(self):
        # It is reachable as the company default, but nothing routes to it, so
        # promising it would be wrong.
        self.assertEqual(matching.predict([self.unrestricted], {"JobCategoryID": "IT"}), [])

    def test_predict_returns_every_definition_that_fits(self):
        payload = {"JobCareerLevelID": "SeniorLevel", "JobCategoryID": "IT", "JobPositionTitle": "Backend Engineer"}
        self.assertEqual(matching.predict([self.senior, self.contains], payload), ["Tech", "Titled"])


class RuleComparison(unittest.TestCase):
    """Deciding whether a stored template needs its rules rewritten."""

    def test_order_and_scalar_shape_do_not_count_as_a_difference(self):
        stored = {"Must": [{"Key": "b", "Operator": "is", "Value": "2"}, {"Key": "a", "Operator": "in", "Value": ["y", "x"]}]}
        wanted = {"Must": [{"Key": "a", "Operator": "in", "Value": ["x", "y"]}, {"Key": "b", "Operator": "is", "Value": "2"}]}
        self.assertFalse(_rules_differ(stored, wanted))

    def test_a_changed_value_is_a_difference(self):
        stored = {"Must": [{"Key": "a", "Operator": "is", "Value": "1"}]}
        wanted = {"Must": [{"Key": "a", "Operator": "is", "Value": "2"}]}
        self.assertTrue(_rules_differ(stored, wanted))

    def test_absent_rules_normalise_to_empty(self):
        self.assertEqual(_normalise_rules(None), [])
        self.assertFalse(_rules_differ(None, None))


class AssignmentGrading(unittest.TestCase):
    """What the run makes of the template the API actually assigned."""

    def setUp(self):
        self.definitions = [{"name": "Tech Recruiting DE"}, {"name": "Werkstudenten DE"}]

    def checks(self, assigned, expected):
        return [f["check"] for f in _assignment_findings(self.definitions, assigned, expected)]

    def test_the_expected_template_is_silent(self):
        self.assertEqual(self.checks("Tech Recruiting DE", ["Tech Recruiting DE"]), [])

    def test_a_wrong_template_is_critical(self):
        findings = _assignment_findings(self.definitions, "Werkstudenten DE", ["Tech Recruiting DE"])
        self.assertEqual(findings[0]["check"], "template_mismatch")
        self.assertEqual(findings[0]["severity"], pipeline.CRITICAL)

    def test_no_template_at_all_is_critical(self):
        findings = _assignment_findings(self.definitions, None, ["Tech Recruiting DE"])
        self.assertEqual(findings[0]["check"], "template_assigned")
        self.assertEqual(findings[0]["severity"], pipeline.CRITICAL)

    def test_an_unmatched_job_falling_back_is_a_warning(self):
        findings = _assignment_findings(self.definitions, "Tech Recruiting DE", [])
        self.assertEqual(findings[0]["check"], "template_fallback")
        self.assertEqual(findings[0]["severity"], pipeline.WARNING)

    def test_overlapping_rules_are_noted(self):
        findings = _assignment_findings(self.definitions, "Tech Recruiting DE", ["Tech Recruiting DE", "Werkstudenten DE"])
        self.assertEqual(findings[0]["check"], "template_overlap")
        self.assertEqual(findings[0]["severity"], pipeline.INFO)

    def test_a_template_we_have_no_definition_for_cannot_be_verified(self):
        checks = self.checks("Something Else", [])
        self.assertIn("template_unknown", checks)

    def test_definition_lookup_ignores_case_and_spacing(self):
        self.assertIsNotNone(_definition_named(self.definitions, "  tech recruiting de "))


class TemplateReconciliation(unittest.TestCase):
    """What an existing template gets rewritten for, and what it does not."""

    def setUp(self):
        self.definition = {"name": "Tech Recruiting DE", "description": "Standard", "is_default": False}
        self.rules = {"Must": [{"Key": "job_category_id", "Operator": "is", "Value": "IT"}]}
        self.stored = {"FilterRulesElastic": self.rules, "Description": "Standard", "IsDefault": False}

    def test_a_template_that_agrees_is_left_alone(self):
        self.assertEqual(_template_updates(self.stored, self.definition, self.rules), [])

    def test_changed_match_rules_are_pushed(self):
        wanted = {"Must": [{"Key": "job_category_id", "Operator": "is", "Value": "Sales"}]}
        updates = _template_updates(self.stored, self.definition, wanted)
        self.assertEqual(len(updates), 1)
        self.assertIn("match rules", updates[0])

    def test_handing_over_the_default_is_pushed(self):
        # The reason this matters: a definition that gives up `is_default` has
        # to be able to hand it over, or the old default keeps collecting every
        # unmatched job while the YAML claims otherwise.
        stored = dict(self.stored, IsDefault=True)
        updates = _template_updates(stored, self.definition, self.rules)
        self.assertEqual(updates, ["is_default -> False"])

    def test_claiming_the_default_is_pushed(self):
        definition = dict(self.definition, is_default=True)
        self.assertEqual(_template_updates(self.stored, definition, self.rules), ["is_default -> True"])

    def test_description_drift_is_pushed_but_whitespace_is_not(self):
        self.assertEqual(_template_updates(dict(self.stored, Description="Alt"), self.definition, self.rules),
                         ["description"])
        self.assertEqual(_template_updates(dict(self.stored, Description="  Standard\n"), self.definition, self.rules),
                         [])

    def test_a_missing_stored_description_matches_a_definition_without_one(self):
        definition = {"name": "X", "is_default": False}
        self.assertEqual(_template_updates({"FilterRulesElastic": self.rules}, definition, self.rules), [])


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = {"data": {"ok": True}} if payload is None else payload
        self.headers = headers or {}
        self.content = b"{}"

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._payload

    @property
    def text(self):
        return str(self._payload)


class FakeSession:
    """Stands in for requests.Session, handing back a scripted list of responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "json": json})
        return self.responses.pop(0)


class Retries(unittest.TestCase):
    """The backoff path. The API never rate-limited me, so this is where it runs.

    The spec documents no 429 and no Retry-After at all, but Cloudflare fronts
    the deployment, so the retry logic exists on the assumption that silence is
    not a guarantee. Untested, it would be a guess; here it is at least a
    checked one.
    """

    def setUp(self):
        # The client logs each backoff at warning level, which is right in a run
        # and noise in a test run.
        logging.getLogger("paulsjob").setLevel(logging.ERROR)

    def tearDown(self):
        logging.getLogger("paulsjob").setLevel(logging.NOTSET)

    def client(self, *responses):
        client = PaulsJobClient(api_key="test-key", base_url="https://example.invalid")
        client.session = FakeSession(*responses)
        return client

    def test_a_429_is_retried_and_then_succeeds(self):
        client = self.client(FakeResponse(429, headers={"Retry-After": "2"}), FakeResponse(200))
        with mock.patch("paulsjob.client.time.sleep") as sleep:
            self.assertEqual(client.get("/thing"), {"ok": True})
        sleep.assert_called_once_with(2.0)          # the server's number, not ours
        self.assertEqual(len(client.session.calls), 2)

    def test_a_retry_keeps_the_same_request_id(self):
        # This is what stops a retried write from being counted as a second
        # request by the API.
        client = self.client(FakeResponse(500), FakeResponse(200))
        with mock.patch("paulsjob.client.time.sleep"):
            client.post("/thing", json_body={"a": 1})
        first, second = (call["headers"]["paul-request-id"] for call in client.session.calls)
        self.assertEqual(first, second)

    def test_backoff_grows_when_no_retry_after_is_given(self):
        client = self.client(FakeResponse(500), FakeResponse(503), FakeResponse(200))
        with mock.patch("paulsjob.client.time.sleep") as sleep:
            client.get("/thing")
        delays = [call.args[0] for call in sleep.call_args_list]
        self.assertEqual(len(delays), 2)
        self.assertLess(delays[0], delays[1])

    def test_a_validation_error_is_not_retried(self):
        # Sending the same bad payload again would only waste the customer's
        # time, so 4xx that is not 429 must fail immediately.
        client = self.client(FakeResponse(422, payload={"message": "nope"}))
        with mock.patch("paulsjob.client.time.sleep") as sleep:
            with self.assertRaises(ValidationError):
                client.get("/thing")
        sleep.assert_not_called()
        self.assertEqual(len(client.session.calls), 1)

    def test_retries_are_bounded(self):
        client = self.client(*[FakeResponse(500) for _ in range(MAX_ATTEMPTS)])
        with mock.patch("paulsjob.client.time.sleep"):
            with self.assertRaises(ServerError):
                client.get("/thing")
        self.assertEqual(len(client.session.calls), MAX_ATTEMPTS)


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
