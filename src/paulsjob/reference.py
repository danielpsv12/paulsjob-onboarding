"""Reference-data lookup.

Job creation wants IDs for category, career level, employment type and working
hours, but a customer hands over a spreadsheet with words in it ("Vollzeit").
This module resolves words to IDs against the live API, so nothing is hardcoded.

That matters here, because the ID schemes are not consistent: job categories and
career levels use semantic IDs ("AdministrationAndSecretariat", "C-Level")
while employment types use numeric strings ("1"). Any table of constants baked
into the client would drift the first time they add a category.
"""

from .errors import ConfigError

# (attribute, endpoint, key inside `data` - None means data is the list itself)
REFERENCE_SETS = [
    ("job_categories", "/recruiting/jobs/categories", None),
    ("career_levels", "/recruiting/jobs/career-levels", None),
    ("employment_types", "/recruiting/jobs/employment-types", None),
    ("working_hours", "/recruiting/jobs/working-hours", None),
    ("step_categories", "/recruiting/job-step-categories", "Categories"),
]


class ReferenceData:
    """Loads every reference list once per run and resolves names to IDs."""

    def __init__(self, client):
        self.client = client
        self._sets = {}

    def load(self):
        for attribute, path, key in REFERENCE_SETS:
            data = self.client.get(path)
            items = data.get(key) if key else data
            self._sets[attribute] = items or []
        return self

    def all(self, name):
        if name not in self._sets:
            raise ConfigError(f"unknown reference set '{name}'")
        return self._sets[name]

    def resolve(self, set_name, value, field_label=None):
        """Return the ID for `value`, which may already be an ID or a name.

        Matching is case-insensitive and also checks the localised NameMap that
        step categories carry, so both "Vorauswahl" and "PreScreening" work.
        """
        label = field_label or set_name
        if value in (None, ""):
            return None
        needle = str(value).strip().casefold()

        for item in self.all(set_name):
            candidates = [item.get("ID"), item.get("Name")]
            name_map = item.get("NameMap") or {}
            candidates.extend(name_map.values())
            if any(c and str(c).casefold() == needle for c in candidates):
                return item["ID"]

        raise ConfigError(
            f"{label}: '{value}' is not a valid value.\n"
            f"  valid options: {self._options(set_name)}"
        )

    def _options(self, set_name, limit=12):
        items = self.all(set_name)
        shown = [f"{i.get('ID')} ({i.get('Name')})" for i in items[:limit]]
        suffix = f", ... {len(items) - limit} more" if len(items) > limit else ""
        return ", ".join(shown) + suffix

    def summary(self):
        return {name: len(items) for name, items in self._sets.items()}
