"""Job creation, made repeatable.

The point of an onboarding tool is that a customer hands over a list of jobs and
you run it. Runs get interrupted, inputs get corrected, someone re-runs the same
file. None of that may produce duplicate jobs, so every job carries an external
ID from the customer's own system and we look it up before creating anything.
"""

import logging
import uuid

from .errors import ConfigError, NotFoundError

log = logging.getLogger("paulsjob")

JOBS_PATH = "/recruiting/jobs"
BY_EXTERNAL_ID_PATH = "/recruiting/jobs/by-external-id"
SEARCH_PATH = "/recruiting/jobs/search-jobs"

REQUIRED_FIELDS = ("external_id", "title", "description")


def find_by_external_id(client, external_id):
    """Return the existing job for this customer-side ID, or None."""
    try:
        return client.get(f"{BY_EXTERNAL_ID_PATH}/{external_id}")
    except NotFoundError:
        return None


def _description(value):
    """Build the JobPositionDescription object.

    The API does not take a description string - it takes an object whose only
    required field is JobRequirements. A customer export usually has one free
    text block, so a plain string is accepted and mapped onto that field; a
    structured block can fill the other two.
    """
    if isinstance(value, str):
        return {"JobRequirements": value.strip()}
    if isinstance(value, dict):
        requirements = (value.get("requirements") or value.get("JobRequirements") or "").strip()
        if not requirements:
            raise ConfigError("description: 'requirements' is required")
        description = {"JobRequirements": requirements}
        optional = {
            "IdealCandidateProfile": value.get("ideal_candidate") or value.get("IdealCandidateProfile"),
            "AboutCompany": value.get("about_company") or value.get("AboutCompany"),
        }
        description.update({k: v.strip() for k, v in optional.items() if v})
        return description
    raise ConfigError(f"description must be text or a block, got {type(value).__name__}")


def ensure_job_owner(client, job_id, person_slug):
    """Make sure the job has an owner, because init refuses to run without one.

    POST /steps/init answers "422: job must have a job owner before initializing
    pipeline template". That prerequisite is not mentioned in the init endpoint's
    documentation, and the JobDetailsPayload note that the creator is added
    automatically does not apply here: a job created with a company API key has
    no creating person. So the owner is set explicitly.
    """
    if not person_slug:
        raise ConfigError(
            "no person slug available to own the job - jobs cannot be initialised without an owner"
        )
    try:
        existing = client.get(f"{JOBS_PATH}/{job_id}/hiring-managers") or []
    except NotFoundError:
        existing = []
    if isinstance(existing, dict):
        existing = existing.get("HiringManagers") or existing.get("JobHiringManagers") or []

    if any(manager.get("IsJobOwner") for manager in existing):
        return False

    client.post(
        f"{JOBS_PATH}/{job_id}/hiring-managers",
        json_body={"PersonSlug": person_slug, "IsJobOwner": True},
    )
    return True


def build_payload(job, reference, person_slug=None):
    """Translate one input job into the API's JobDetailsPayload.

    Words from the customer's spreadsheet ("Vollzeit", "Berufserfahren") are
    resolved to IDs against the live reference lists rather than a hardcoded map.
    """
    missing = [field for field in REQUIRED_FIELDS if not job.get(field)]
    if missing:
        raise ConfigError(f"job '{job.get('external_id') or job.get('title') or '?'}': missing {', '.join(missing)}")

    payload = {
        "JobPositionTitle": job["title"],
        "JobPositionDescription": _description(job["description"]),
        "JobExternalID": str(job["external_id"]),
        # Published/Expired are required by the schema and drive visibility.
        "Published": bool(job.get("published", True)),
        "Expired": bool(job.get("expired", False)),
        # Defaults to true in the API, which sends the job to the AI indexer the
        # moment it is created. Left off by default here so a demo run does not
        # spend credits; set `indexing: true` in the input to enable it.
        "Indexing": bool(job.get("indexing", False)),
    }

    optional = {
        "InternalName": job.get("internal_name"),
        "Location": job.get("location"),
        "Region": job.get("region"),
        "JobURL": job.get("url"),
    }
    payload.update({k: v for k, v in optional.items() if v})

    lookups = (
        ("JobCategoryID", "job_categories", job.get("category"), "category"),
        ("JobCareerLevelID", "career_levels", job.get("career_level"), "career_level"),
        ("EmploymentTypeID", "employment_types", job.get("employment_type"), "employment_type"),
        ("WorkingHoursID", "working_hours", job.get("working_hours"), "working_hours"),
    )
    for field, set_name, value, label in lookups:
        if value:
            payload[field] = reference.resolve(set_name, value, field_label=f"job '{job['external_id']}' {label}")

    # Set the owner at creation. The schema says the creator is added
    # automatically when this is omitted, but an API key has no creating person,
    # so without this the job is created ownerless and init then refuses it.
    owner = job.get("owner_slug") or person_slug
    if owner:
        payload["HiringManagers"] = [{"PersonSlug": owner, "IsJobOwner": True}]

    return payload


def ensure_job(client, reference, job, person_slug=None):
    """Create the job unless one with the same external ID already exists.

    Returns (job_record, created, action). The lookup is what makes a re-run
    safe; the request id sent with the POST guards the narrower case of a retry
    inside a single run.
    """
    external_id = str(job["external_id"])
    existing = find_by_external_id(client, external_id)
    if existing:
        return existing, False, f"job '{external_id}' already exists ({job_id_of(existing)}) - reused"

    payload = build_payload(job, reference, person_slug=person_slug)
    created = client.post(
        JOBS_PATH,
        json_body=payload,
        # A request id derived from the external ID is stable across retries and
        # across runs, so the same job always presents the same request id. The
        # API documents paul-request-id as an idempotency aid; the lookup above
        # is the real guarantee, this is the second line of defence.
        idempotency_key=str(uuid.uuid5(uuid.NAMESPACE_URL, f"paulsjob-onboarding/job/{external_id}")),
    ) or {}
    job_id = job_id_of(created) or "dry-run-job-id"
    return created, True, f"created job '{external_id}' -> {job_id}"


def job_id_of(job_record):
    """Return the PaulsjobJobID - the id every other endpoint expects.

    A job record carries three different identifiers and only one of them works
    in a URL:

      PaulsjobJobID  182681        <- the {paulsjob_job_id} path parameter
      JobPositionID  1            <- a per-company counter
      JobExternalID  BSP-2026-001 <- the customer's own id

    Picking the wrong one produces "404: job not found" on a job that plainly
    exists. Matching is case-insensitive because the spelling is PaulsjobJobID
    in the response while the docs write paulsjob_job_id.
    """
    if not isinstance(job_record, dict):
        return None
    lowered = {key.casefold(): value for key, value in job_record.items()}
    for key in ("paulsjobjobid", "jobpositionid", "id"):
        if lowered.get(key):
            return lowered[key]
    return None
