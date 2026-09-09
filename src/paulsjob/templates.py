"""Pipeline templates: make sure the pipelines a job could need actually exist.

The task says "match it with an appropriate pipeline template". The platform can
do that matching itself - a template carries `FilterRulesElastic` and the job is
initialised with `AutoInit` - but in a fresh account there is nothing to match
against: no company templates, an empty platform library, and no company default
(that endpoint answers 404). So the templates have to exist, and carry their
rules, before any job can be matched to one.

This module reads template definitions from YAML and creates each only if a
template of that name is absent, which keeps repeated runs from piling up
duplicates. An existing template whose match rules have drifted from the
definition is corrected, because the rules are what the API matches on: a stale
rule silently sends the next job to the wrong pipeline.
"""

import logging

from . import matching
from .errors import ConfigError, NotFoundError, ValidationError

log = logging.getLogger("paulsjob")

TEMPLATES_PATH = "/recruiting/job-step-templates/pipelines"
# A template belongs to a version group, which is a separate object with its own
# lifetime: deleting the template does not delete the group, and the dashboard
# lists groups, so an undeleted one shows up as a duplicate of the same name.
VERSION_GROUPS_PATH = "/recruiting/job-step-templates/pipeline-version-groups"
DEFAULT_TEMPLATE_PATH = "/recruiting/job-step-templates/pipelines/default"

VALID_AGENT_TYPES = {"proactive", "reactive", "reactive_and_proactive"}
VALID_HIL_SETTINGS = {"always_on", "always_off", "conditional"}

DRY_RUN_TEMPLATE_ID = "dry-run-template-id"
DRY_RUN_STEP_ID = "dry-run-step-id"

# Categories whose agents need extra fields the API only validates on write.
# Kept here so the definition can be checked before anything is created.
REQUIRED_ADDITIONAL_FIELDS = {
    "AIVoiceInterview": ("GreetingInstructions", "EndOfCallProcedure"),
}


def find_template_by_name(client, name):
    """Return the template with this name, or None. Walks every page."""
    needle = name.strip().casefold()
    for template in client.paginate_cursor(TEMPLATES_PATH, "PipelineTemplates"):
        if (template.get("Name") or "").strip().casefold() == needle:
            return template
    return None


def get_company_default(client):
    """Return the company default template, or None when none is configured.

    A fresh account answers 404 here. That is an expected state, not a failure -
    it is precisely why AutoInit cannot work until a template exists.
    """
    try:
        return client.get(DEFAULT_TEMPLATE_PATH)
    except NotFoundError:
        return None


def _template_body(definition, filter_rules):
    """The create/update payload for a template, without its steps."""
    body = {"Name": definition["name"], "IsDefault": bool(definition.get("is_default", False))}
    if definition.get("description"):
        body["Description"] = definition["description"]
    if definition.get("pipeline_without_ai"):
        # Costs no credits, but only permits New/TeamDiscussion/CodeExecution/Rejected.
        body["PipelineWithoutAI"] = True
    if filter_rules is not None:
        body["FilterRulesElastic"] = filter_rules
    return body


def _rules_differ(stored, wanted):
    """Compare what the template carries with what the definition asks for."""
    return _normalise_rules(stored) != _normalise_rules(wanted)


def _template_updates(stored, definition, filter_rules):
    """What the live template disagrees with the definition about.

    Match rules are the important one - they decide which jobs arrive - but
    `is_default` matters just as much and in the opposite direction: it decides
    which jobs arrive when *nothing* matched. A definition that gives up the
    default has to be able to hand it over, or the old default keeps quietly
    collecting every unmatched job.

    PipelineWithoutAI is deliberately not reconciled: switching it on is
    refused with 422 while the template still holds steps of other categories,
    so pushing it blindly would break the run rather than fix the template.
    """
    updates = []
    if _rules_differ(stored.get("FilterRulesElastic"), filter_rules):
        updates.append(f"match rules -> {_describe_rules(filter_rules)}")
    wanted_default = bool(definition.get("is_default", False))
    if bool(stored.get("IsDefault")) != wanted_default:
        updates.append(f"is_default -> {wanted_default}")
    if (stored.get("Description") or "").strip() != (definition.get("description") or "").strip():
        updates.append("description")
    return updates


def _normalise_rules(rules):
    """Key/operator/value triples, order-independent and string-compared.

    The API echoes rules back in its own order and can widen a scalar to a list,
    so a naive == would report a difference on every run and rewrite the
    template each time.
    """
    if not rules:
        return []
    out = []
    for rule in rules.get("Must") or []:
        value = rule.get("Value")
        value = sorted(str(v) for v in value) if isinstance(value, list) else [str(value)]
        out.append((str(rule.get("Key")), str(rule.get("Operator")), tuple(value)))
    return sorted(out)


def _describe_rules(rules):
    """One readable line, for the run report."""
    if not rules:
        return "none - reachable only as the company default or by explicit id"
    parts = []
    for rule in rules.get("Must") or []:
        value = rule.get("Value")
        value = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
        parts.append(f"{rule.get('Key')} {rule.get('Operator')} {value}")
    return " AND ".join(parts)


def validate_definition(definition, reference):
    """Check the YAML before sending anything, so mistakes fail locally.

    The API would reject most of this too, but with terser messages and only
    after a partial template has already been created.
    """
    if not definition.get("name"):
        raise ConfigError("pipeline definition needs a 'name'")

    # Raises with the list of valid options when a match value is a word the
    # account does not know. The API's own answer is 400 'invalid filter rules',
    # which does not say which value it disliked.
    matching.build_filter_rules(
        definition.get("match"), reference, where=f"pipeline '{definition['name']}'"
    )

    steps = definition.get("steps") or []
    if not steps:
        raise ConfigError(f"pipeline '{definition['name']}' has no steps")

    for index, step in enumerate(steps, start=1):
        where = f"step {index} ('{step.get('name', '?')}')"
        if not step.get("name"):
            raise ConfigError(f"{where}: needs a 'name'")
        if not step.get("category"):
            raise ConfigError(f"{where}: needs a 'category'")
        # Raises with the list of valid categories when the value is wrong.
        reference.resolve("step_categories", step["category"], field_label=f"{where} category")

        agent = step.get("agent")
        if not agent:
            continue
        if agent.get("type", "proactive") not in VALID_AGENT_TYPES:
            raise ConfigError(f"{where}: agent type must be one of {sorted(VALID_AGENT_TYPES)}")
        setting = agent.get("human_in_loop", "always_off")
        if setting not in VALID_HIL_SETTINGS:
            raise ConfigError(f"{where}: human_in_loop must be one of {sorted(VALID_HIL_SETTINGS)}")
        # Cross-field rule from the spec: the API answers 422 when this is wrong.
        # Checking it here turns a server-side rejection into a clear local message.
        condition = (agent.get("condition") or "").strip()
        if setting == "conditional" and not condition:
            raise ConfigError(f"{where}: human_in_loop 'conditional' requires a 'condition'")
        if setting != "conditional" and condition:
            raise ConfigError(
                f"{where}: 'condition' is only allowed when human_in_loop is 'conditional' "
                f"(got '{setting}'); the API rejects this with 422"
            )
        if not (agent.get("instructions") or "").strip():
            raise ConfigError(f"{where}: agent needs 'instructions'")

        # Category-specific required fields. Without this check the template is
        # created, some steps succeed, and the run dies partway through with a
        # 422 that names a field but not the step it belongs to.
        category_id = reference.resolve("step_categories", step["category"])
        required_extra = REQUIRED_ADDITIONAL_FIELDS.get(category_id, ())
        provided = agent.get("additional_fields") or {}
        missing = [field for field in required_extra if not str(provided.get(field, "")).strip()]
        if missing:
            raise ConfigError(
                f"{where}: category {category_id} requires additional_fields "
                f"{', '.join(missing)} - the API rejects the agent with 422 otherwise"
            )


def _agent_payload(agent, category_id, person_slug):
    setting = agent.get("human_in_loop", "always_off")
    human_in_loop = {"Setting": setting}
    if setting == "conditional":
        human_in_loop["Condition"] = agent["condition"]

    payload = {
        "Name": agent["name"],
        "AgentType": agent.get("type", "proactive"),
        "Instructions": {"SystemPrompt": agent["instructions"].strip()},
        "HumanInLoop": human_in_loop,
        "IsActive": bool(agent.get("active", True)),
        "CreatedBy": person_slug,
        "CategoryID": category_id,
    }
    if agent.get("description"):
        payload["Description"] = agent["description"]

    # Some categories require extra fields, and the API only tells you which one
    # is missing once you try (AIVoiceInterview answers 422 "GreetingInstructions
    # is required"). The definition supplies them verbatim; the discriminator
    # that selects the variant is filled in from the step's category so the YAML
    # cannot contradict itself.
    extra = agent.get("additional_fields")
    if extra:
        payload["AdditionalFields"] = {"Discriminator": category_id, **extra}
    return payload


def _live_steps(client, template_id):
    """Steps already on the template, keyed by lowercased name."""
    if client.dry_run and template_id == DRY_RUN_TEMPLATE_ID:
        return {}
    data = client.get(f"{TEMPLATES_PATH}/{template_id}/steps") or {}
    steps = data.get("JobStepTemplates") if isinstance(data, dict) else data
    return {(s.get("Name") or "").strip().casefold(): s for s in (steps or [])}


def _live_agent_names(client, template_id, step_id):
    if client.dry_run and step_id == DRY_RUN_STEP_ID:
        return set()
    data = client.get(f"{TEMPLATES_PATH}/{template_id}/steps/{step_id}/agents") or {}
    agents = data.get("JobStepAgentTemplates") if isinstance(data, dict) else data
    return {(a.get("Name") or "").strip().casefold() for a in (agents or [])}


def ensure_template(client, reference, definition, person_slug):
    """Bring the pipeline template to the state the definition describes.

    Returns (template, actions). This reconciles rather than just creating: an
    existing template gets the steps and agents it is missing. That matters
    because a template can be left half-built - if one agent is rejected midway,
    the template and the steps before it already exist. A tool that skipped any
    template whose name it recognised would never repair that.
    """
    validate_definition(definition, reference)
    name = definition["name"]
    actions = []

    filter_rules = matching.build_filter_rules(
        definition.get("match"), reference, where=f"pipeline '{name}'"
    )

    existing = find_template_by_name(client, name)
    if existing:
        template_id = existing.get("ID")
        actions.append(f"template '{name}' already exists ({template_id}) - reconciling")
        # A definition that has moved on from what is stored has to win.
        # Compared before writing: an update is in place (same id, Version
        # stays 1, steps survive) but it does not move UpdatedAt either, so a
        # needless rewrite would leave nothing behind to notice it by.
        #
        # The comparison needs the detail endpoint: the template *list* omits
        # FilterRulesElastic altogether, so reusing the record found by name
        # would read every template as having no rules at all.
        if not client.dry_run:
            stored = client.get(f"{TEMPLATES_PATH}/{template_id}") or {}
            updates = _template_updates(stored, definition, filter_rules)
            if updates:
                client.put(f"{TEMPLATES_PATH}/{template_id}", json_body=_template_body(definition, filter_rules))
                for update in updates:
                    actions.append(f"  ~ {update}")
    else:
        created = client.post(TEMPLATES_PATH, json_body=_template_body(definition, filter_rules)) or {}
        template_id = created.get("ID") or created.get("id")
        if not template_id:
            if not client.dry_run:
                raise ConfigError(f"template creation returned no ID: {created}")
            template_id = DRY_RUN_TEMPLATE_ID
        actions.append(f"created template '{name}' ({template_id})")
        actions.append(f"  match rules: {_describe_rules(filter_rules)}")

    live_steps = _live_steps(client, template_id)

    for order, step in enumerate(definition["steps"], start=1):
        category_id = reference.resolve("step_categories", step["category"])
        live = live_steps.get(step["name"].strip().casefold())

        if live:
            step_id = live.get("ID")
            actions.append(f"  = step '{step['name']}' [{category_id}] already present")
        else:
            step_body = {
                "Name": step["name"],
                "Category": category_id,
                "OrderIndex": step.get("order_index", order),
            }
            if step.get("color"):
                step_body["Color"] = step["color"]
            step_created = client.post(f"{TEMPLATES_PATH}/{template_id}/steps", json_body=step_body) or {}
            step_id = step_created.get("ID") or step_created.get("id") or DRY_RUN_STEP_ID
            actions.append(f"  + step '{step['name']}' [{category_id}] ({step_id})")

        agent = step.get("agent")
        if not agent:
            continue
        if not person_slug:
            raise ConfigError(
                "cannot create agents: no person slug found on this account "
                "(agents require a CreatedBy person slug)"
            )
        if agent["name"].strip().casefold() in _live_agent_names(client, template_id, step_id):
            actions.append(f"    = agent '{agent['name']}' already present")
            continue
        try:
            client.post(
                f"{TEMPLATES_PATH}/{template_id}/steps/{step_id}/agents",
                json_body=_agent_payload(agent, category_id, person_slug),
            )
            actions.append(f"    + agent '{agent['name']}' ({agent.get('type', 'proactive')})")
        except ValidationError as exc:
            # Surface which step failed before re-raising - the API's message
            # names the missing field but not where it belongs.
            actions.append(f"    ! agent '{agent['name']}' on step '{step['name']}' rejected: {exc.message}")
            raise

    if client.dry_run:
        return {"ID": template_id, "Name": name}, actions
    return client.get(f"{TEMPLATES_PATH}/{template_id}"), actions
