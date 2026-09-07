"""Account-level lookups: who this API key belongs to, and what it can spend."""

from .errors import ApiError

PERSON_LIST_PATH = "/company/person/list"
CREDITS_PATH = "/company/credits"
PROFILE_PATH = "/company/profile"


def resolve_person_slug(client, email=None):
    """Find the person slug used as the owner of created agents.

    Agents require a `CreatedBy` person slug (not a numeric user id). When no
    email is given we take the first person on the account, which is the account
    owner in a single-user setup.
    """
    # /company/person/list is a POST, so the cursor helper (which GETs) does not
    # apply. The account list is small; one page is enough to find the owner.
    data = client.post(PERSON_LIST_PATH, json_body={"PerPage": 50}, read_only=True) or {}
    persons = data.get("Persons") or []
    if not persons:
        return None
    if email:
        for person in persons:
            if (person.get("Email") or "").casefold() == email.casefold():
                return person.get("PersonSlug")
    return persons[0].get("PersonSlug")


def credits_snapshot(client):
    """Return remaining credits, or None if the endpoint is not available."""
    try:
        data = client.get(CREDITS_PATH) or {}
    except ApiError:
        return None
    return {
        "remaining": data.get("RemainingCredits"),
        "used": data.get("CreditsUsed"),
        "total": data.get("TotalCredits"),
    }


def company_name(client):
    try:
        data = client.get(PROFILE_PATH) or {}
    except ApiError:
        return None
    return data.get("company_name")
