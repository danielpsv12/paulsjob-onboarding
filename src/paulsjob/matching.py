"""Matching a job to a pipeline template.

The platform matches jobs to templates itself: a template carries
`FilterRulesElastic`, and `POST .../steps/init` with `AutoInit: true` lets the
API pick the template whose rules the job satisfies. This module translates the
`match:` block of a pipeline definition into those rules, using the same
customer words the job list uses ("Oberstufe", "Werkstudent"), resolved against
the live reference lists.

It also evaluates the same rules locally. That is not a second implementation of
the platform's matching and is not used to choose anything - it exists because
the API's fallback is silent: when no rule matches, init still answers 200 and
quietly assigns the company default template. Predicting the match locally is
what lets the run say "expected X, the API assigned Y" instead of reporting a
job as fine when it went live on the wrong process.
"""

from .errors import ConfigError

# match key -> (filter key sent to the API, reference set for resolving words,
#               field in the job payload, operator for a single value)
#
# The filter keys come from GET /recruiting/jobs/filters, which is also where
# their permitted operators are documented.
MATCH_FIELDS = {
    "category": ("job_category_id", "job_categories", "JobCategoryID", "is"),
    "career_level": ("job_career_level_id", "career_levels", "JobCareerLevelID", "is"),
    "employment_type": ("employment_type_id", "employment_types", "EmploymentTypeID", "is"),
    "working_hours": ("working_hours_id", "working_hours", "WorkingHoursID", "is"),
    "location": ("location", None, "Location", "is"),
    "region": ("region", None, "Region", "is"),
    "title_contains": ("job_position_title", None, "JobPositionTitle", "contains"),
    "location_contains": ("location", None, "Location", "contains"),
}

# Only these two are produced from a `match:` block. `contains` is
# case-insensitive and word-wise on the API side, which the local evaluation
# below mirrors.
LIST_OPERATOR = "in"


def build_filter_rules(match, reference, where="pipeline"):
    """Translate a `match:` block into a FilterRulesElastic payload.

    Returns None when the definition has no `match:` block, which means the
    template carries no rules and can only be reached as the company default or
    by naming its id.
    """
    if not match:
        return None
    if not isinstance(match, dict):
        raise ConfigError(f"{where}: 'match' must be a mapping of field -> value")

    rules = []
    for key, value in match.items():
        if key not in MATCH_FIELDS:
            raise ConfigError(
                f"{where}: unknown match field '{key}'. "
                f"Available: {', '.join(sorted(MATCH_FIELDS))}"
            )
        filter_key, reference_set, _, single_operator = MATCH_FIELDS[key]

        values = value if isinstance(value, list) else [value]
        if not values or any(v is None or str(v).strip() == "" for v in values):
            raise ConfigError(f"{where}: match field '{key}' has an empty value")
        if single_operator == "contains" and isinstance(value, list):
            raise ConfigError(f"{where}: match field '{key}' takes a single value, not a list")

        # Words the customer wrote are resolved to IDs, exactly as job fields
        # are. Without this the API answers 400 'invalid filter rules' and does
        # not say which value it disliked.
        if reference_set:
            values = [
                reference.resolve(reference_set, v, field_label=f"{where} match {key}")
                for v in values
            ]

        rules.append(
            {
                "Key": filter_key,
                "Operator": LIST_OPERATOR if isinstance(value, list) else single_operator,
                "Value": values if isinstance(value, list) else values[0],
            }
        )

    # Must means AND, which is what a kickoff rule ("senior IT roles") reads
    # like. Should/OR is expressible in the API but not offered here: a rule set
    # nobody can predict in their head is worse than two templates.
    return {"Must": sorted(rules, key=lambda r: r["Key"])}


def matches(rules, payload):
    """Does this job payload satisfy these filter rules? Local approximation."""
    if not rules:
        return False
    by_filter_key = {spec[0]: spec[2] for spec in MATCH_FIELDS.values()}
    for rule in rules.get("Must") or []:
        field = by_filter_key.get(rule.get("Key"))
        if not field:
            # A rule the local evaluation does not model. Better to admit that
            # than to claim a mismatch the API would not agree with.
            return False
        if not _holds(rule, payload.get(field)):
            return False
    return True


def _holds(rule, actual):
    operator = rule.get("Operator")
    expected = rule.get("Value")
    if actual is None:
        return operator in ("is_not", "not_in", "not_contains")
    actual = str(actual)
    if operator == "is":
        return actual == str(expected)
    if operator == "is_not":
        return actual != str(expected)
    if operator == "in":
        return actual in [str(v) for v in (expected or [])]
    if operator == "not_in":
        return actual not in [str(v) for v in (expected or [])]
    if operator in ("contains", "not_contains"):
        # The API treats each space-separated word as its own contains check.
        words = str(expected).casefold().split()
        hit = all(word in actual.casefold() for word in words)
        return hit if operator == "contains" else not hit
    return False


def predict(definitions, payload):
    """Names of the definitions whose rules this job satisfies locally.

    More than one name means the definitions overlap and which template the job
    gets is the platform's choice, not something this tool can promise.
    """
    return [d["name"] for d in definitions if matches(d.get("_filter_rules"), payload)]
