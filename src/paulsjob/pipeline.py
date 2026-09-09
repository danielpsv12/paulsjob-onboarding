"""Initialising a job's pipeline, and then checking that it really is there.

Initialising is one call. The interesting half is the verification: the API
reports success for the init, but success only means "the request was accepted".
Whether the job now has the steps and agents the customer was promised is a
separate question, and it is the one an implementation manager actually has to
answer at go-live.
"""

import logging
import time

from .errors import NotFoundError, ValidationError

log = logging.getLogger("paulsjob")

# Severity of a verification finding.
CRITICAL = "critical"
WARNING = "warning"
INFO = "info"


def steps_path(job_id):
    return f"/recruiting/jobs/{job_id}/steps"


def init_pipeline(client, job_id, template_id=None, auto_init=False, lang=None):
    """Initialise the job's steps from a pipeline template.

    The API resolves the template in a documented order: an explicit
    PipelineTemplateID wins; otherwise AutoInit matches the job against each
    template's filter rules; otherwise the company default is used. When none of
    those produce a template it answers 422 - which is the normal state of a
    fresh account, not a bug.
    """
    body = {}
    if template_id:
        body["PipelineTemplateID"] = template_id
    if auto_init:
        body["AutoInit"] = True

    params = {"lang": lang} if lang else None
    try:
        client.post(f"{steps_path(job_id)}/init", json_body=body, params=params)
    except ValidationError as exc:
        if auto_init and not template_id:
            raise ValidationError(
                exc.status,
                f"{exc.message} - no pipeline template matched this job and no company "
                f"default exists. Create a template first (see 'ensure template' in the "
                f"README) or pass an explicit template id.",
                exc.path,
                exc.method,
                request_id=exc.request_id,
                body=exc.body,
            ) from exc
        raise
    return True


# A job is matched to a template by a search query over the job index, and that
# index trails job creation by a few seconds. Measured on the dev deployment: a
# job created and initialised immediately falls back to the company default,
# while the same job initialised 5s later matches its rule. The API reports 200
# either way, so the wait is not optional - it is the difference between the
# right pipeline and a silently wrong one.
SEARCHABLE_TIMEOUT_SECONDS = 45
SEARCHABLE_POLL_SECONDS = 1.0


def wait_until_searchable(client, external_id, timeout=SEARCHABLE_TIMEOUT_SECONDS):
    """Block until the job is findable in the job search, or the timeout passes.

    Returns the seconds waited, or None if it never appeared. Polling the search
    is better than a fixed sleep: it is the same index AutoInit matches against,
    so it answers the actual question rather than guessing at a duration.
    """
    from .jobs import find_by_external_id

    started = time.monotonic()
    while True:
        if find_by_external_id(client, external_id):
            return time.monotonic() - started
        if time.monotonic() - started >= timeout:
            return None
        time.sleep(SEARCHABLE_POLL_SECONDS)


def assigned_template(client, job_id):
    """The pipeline template the job actually ended up with, or None.

    404 here means the job was never initialised from a template at all.
    """
    try:
        status = client.get(f"{steps_path(job_id)}/pipeline-template") or {}
    except NotFoundError:
        return None
    return status.get("PipelineTemplate") or None


def fetch_pipeline_state(client, job_id):
    """Read back what the job actually has: steps, their agents, and the template link."""
    steps = client.get(steps_path(job_id)) or []
    if isinstance(steps, dict):
        steps = steps.get("JobSteps") or steps.get("Steps") or []

    for step in steps:
        step_id = step.get("ID")
        if not step_id:
            step["_agents"] = []
            continue
        try:
            step["_agents"] = list(
                client.paginate_cursor(f"{steps_path(job_id)}/{step_id}/agents", "Agents")
            )
        except NotFoundError:
            step["_agents"] = []

    try:
        template_status = client.get(f"{steps_path(job_id)}/pipeline-template")
    except NotFoundError:
        template_status = None

    return {"steps": steps, "template_status": template_status}


def verify(state, definition):
    """Compare the live pipeline against the template definition it came from.

    Returns a list of findings. An empty list means the job matches what was
    configured. Findings are graded, because "a step is missing" and "a step has
    a different colour" are not the same conversation.
    """
    findings = []
    steps = state["steps"]
    expected_steps = definition.get("steps") or []

    if not steps:
        findings.append(
            {
                "severity": CRITICAL,
                "check": "steps_exist",
                "detail": "the job has no steps at all - the pipeline was not initialised",
            }
        )
        return findings

    live_by_name = {(s.get("Name") or "").strip().casefold(): s for s in steps}

    for expected in expected_steps:
        name = (expected.get("name") or "").strip()
        live = live_by_name.get(name.casefold())
        if not live:
            findings.append(
                {
                    "severity": CRITICAL,
                    "check": "step_missing",
                    "detail": f"step '{name}' from the template is not present on the job",
                }
            )
            continue

        expected_category = expected.get("category")
        live_category = (live.get("Category") or {}).get("ID")
        if expected_category and live_category and live_category != expected_category:
            findings.append(
                {
                    "severity": CRITICAL,
                    "check": "step_category",
                    "detail": f"step '{name}': expected category {expected_category}, live is {live_category}",
                }
            )

        if live.get("IsHidden"):
            findings.append(
                {
                    "severity": WARNING,
                    "check": "step_hidden",
                    "detail": f"step '{name}' exists but is deactivated (IsHidden), so it is not in the live flow",
                }
            )

        # The API tracks template deviation itself, but only as a boolean. It is
        # a useful independent signal: it catches edits made in the UI that this
        # tool never saw.
        if live.get("HasChangedFromTemplate"):
            findings.append(
                {
                    "severity": WARNING,
                    "check": "step_drifted",
                    "detail": f"step '{name}' is flagged HasChangedFromTemplate - it was edited away from its template",
                }
            )

        expected_agent = expected.get("agent")
        live_agents = live.get("_agents") or []
        if expected_agent and not live_agents:
            findings.append(
                {
                    "severity": CRITICAL,
                    "check": "agent_missing",
                    "detail": f"step '{name}': template defines agent '{expected_agent.get('name')}' but the job has none",
                }
            )
        elif expected_agent:
            _verify_agent(findings, name, expected_agent, live_agents)
        elif live_agents:
            # The mirror of agent_missing, and the more uncomfortable one: an
            # agent nobody agreed to is talking to candidates and spending
            # credits. Graded critical for the same reason a missing agent is -
            # the definition is the agreement, and this job does not match it.
            names = ", ".join(f"'{a.get('Name') or '?'}'" for a in live_agents)
            findings.append(
                {
                    "severity": CRITICAL,
                    "check": "agent_extra",
                    "detail": (
                        f"step '{name}': the definition configures no agent, but {names} "
                        f"{'is' if len(live_agents) == 1 else 'are'} live on this step"
                    ),
                }
            )

    extra = set(live_by_name) - {(s.get("name") or "").strip().casefold() for s in expected_steps}
    for name in sorted(extra):
        live = live_by_name[name]
        findings.append(
            {
                "severity": INFO,
                "check": "step_extra",
                "detail": (
                    f"step '{live.get('Name')}' [{(live.get('Category') or {}).get('ID')}] exists on the job "
                    f"but is not in the template definition"
                ),
            }
        )

    status = state.get("template_status") or {}
    if isinstance(status, dict) and status.get("AllowPipelineTemplateOverride"):
        findings.append(
            {
                "severity": WARNING,
                "check": "template_unmanaged",
                "detail": (
                    "this job is flagged as allowed to override its template, so future template "
                    "changes will no longer be pushed down to it"
                ),
            }
        )

    return findings


def _verify_agent(findings, step_name, expected_agent, live_agents):
    expected_name = (expected_agent.get("name") or "").strip().casefold()
    match = next((a for a in live_agents if (a.get("Name") or "").strip().casefold() == expected_name), None)
    if not match:
        names = ", ".join(a.get("Name") or "?" for a in live_agents)
        findings.append(
            {
                "severity": CRITICAL,
                "check": "agent_name",
                "detail": f"step '{step_name}': expected agent '{expected_agent.get('name')}', found: {names}",
            }
        )
        return

    if not match.get("IsActive"):
        findings.append(
            {
                "severity": CRITICAL,
                "check": "agent_inactive",
                "detail": f"step '{step_name}': agent '{match.get('Name')}' exists but IsActive is false",
            }
        )

    # Everything below checks the agent that was agreed. Anything else sharing
    # the step is reported separately - it is not a deviation *of* that agent.
    extras = [a for a in live_agents if a is not match]
    if extras:
        names = ", ".join(f"'{a.get('Name') or '?'}'" for a in extras)
        findings.append(
            {
                "severity": WARNING,
                "check": "agent_extra",
                "detail": (
                    f"step '{step_name}': agent '{match.get('Name')}' is configured as agreed, "
                    f"but {names} also {'runs' if len(extras) == 1 else 'run'} on this step"
                ),
            }
        )

    expected_type = expected_agent.get("type", "proactive")
    if match.get("AgentType") and match["AgentType"] != expected_type:
        findings.append(
            {
                "severity": WARNING,
                "check": "agent_type",
                "detail": f"step '{step_name}': expected agent type {expected_type}, live is {match['AgentType']}",
            }
        )

    expected_hil = expected_agent.get("human_in_loop", "always_off")
    live_hil = (match.get("HumanInLoop") or {}).get("Setting")
    if live_hil and live_hil != expected_hil:
        findings.append(
            {
                "severity": WARNING,
                "check": "agent_human_in_loop",
                "detail": f"step '{step_name}': expected human-in-loop {expected_hil}, live is {live_hil}",
            }
        )

    # The prompt is the part most likely to be quietly edited in the UI, and the
    # part a customer is most likely to care about.
    expected_prompt = (expected_agent.get("instructions") or "").strip()
    live_prompt = ((match.get("Instructions") or {}).get("SystemPrompt") or "").strip()
    if expected_prompt and live_prompt and expected_prompt != live_prompt:
        findings.append(
            {
                "severity": WARNING,
                "check": "agent_prompt",
                "detail": (
                    f"step '{step_name}': agent prompt differs from the template definition "
                    f"({len(expected_prompt)} chars expected, {len(live_prompt)} live)"
                ),
            }
        )


def worst_severity(findings):
    if any(f["severity"] == CRITICAL for f in findings):
        return CRITICAL
    if any(f["severity"] == WARNING for f in findings):
        return WARNING
    if findings:
        return INFO
    return None
