"""Command line entry point.

Three commands:

  doctor   - check connectivity, auth and what this account already contains
  onboard  - the actual run: ensure template, create jobs, init pipelines, verify
  verify   - re-check jobs that were onboarded earlier, without changing anything
"""

import argparse
import glob
import logging
import os
import sys

import yaml

from . import account, jobs as jobs_module, matching, pipeline, templates
from .client import PaulsJobClient
from .errors import ApiError, ConfigError, PaulsJobError
from .reference import ReferenceData
from .report import RunReport


def _load_yaml(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except FileNotFoundError:
        raise ConfigError(f"file not found: {path}")
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}")


# Default: every pipeline the customer has. The drift-demo variants are
# deliberate copies of another definition under the same template name, used to
# show what verification reports - loading them would fight over that template.
DEFAULT_PIPELINE_GLOB = os.path.join("pipelines", "*.yaml")
VARIANT_MARKER = ".drift-demo."


def _pipeline_files(paths):
    if not paths:
        found = [f for f in sorted(glob.glob(DEFAULT_PIPELINE_GLOB)) if VARIANT_MARKER not in os.path.basename(f)]
        if not found:
            raise ConfigError(f"no pipeline definitions found in {DEFAULT_PIPELINE_GLOB}")
        return found

    files = []
    for path in paths:
        if os.path.isdir(path):
            files.extend(sorted(glob.glob(os.path.join(path, "*.yaml"))))
        else:
            files.append(path)
    return files


def _load_definitions(paths, reference):
    """Load the pipeline definitions and attach the rules the API will match on.

    Two definitions claiming the same template name would race each other in the
    account, so that is refused here rather than discovered as a reconciliation
    loop later.
    """
    definitions = []
    seen = {}
    for path in _pipeline_files(paths):
        definition = _load_yaml(path)
        name = definition.get("name")
        if not name:
            raise ConfigError(f"{path}: pipeline definition needs a 'name'")
        if name.strip().casefold() in seen:
            raise ConfigError(
                f"{path} and {seen[name.strip().casefold()]} both define a template named '{name}' - "
                f"pick one, or give them different names"
            )
        seen[name.strip().casefold()] = path
        definition["_source"] = path
        definition["_filter_rules"] = matching.build_filter_rules(
            definition.get("match"), reference, where=f"{path} ('{name}')"
        )
        definitions.append(definition)
    return definitions


def _definition_named(definitions, name):
    needle = (name or "").strip().casefold()
    return next((d for d in definitions if (d["name"] or "").strip().casefold() == needle), None)


def _assignment_findings(definitions, assigned_name, expected):
    """Grade what the API's own matching did against what the rules predicted.

    The API answers 200 whether it matched a rule or fell back to the company
    default, so this comparison is the only thing standing between a wrong
    pipeline and a green report.
    """
    findings = []
    if not assigned_name:
        findings.append(
            {
                "severity": pipeline.CRITICAL,
                "check": "template_assigned",
                "detail": "the job has no pipeline template - AutoInit matched nothing and there is no default",
            }
        )
        return findings

    if expected and assigned_name not in expected:
        findings.append(
            {
                "severity": pipeline.CRITICAL,
                "check": "template_mismatch",
                "detail": (
                    f"expected pipeline '{expected[0]}' by its match rules, but the API assigned "
                    f"'{assigned_name}' - the job is live on the wrong process"
                ),
            }
        )
    elif not expected:
        findings.append(
            {
                "severity": pipeline.WARNING,
                "check": "template_fallback",
                "detail": (
                    f"no match rule covers this job, so the API fell back to '{assigned_name}' "
                    f"(the company default). Add a rule if that is not intended."
                ),
            }
        )
    elif len(expected) > 1:
        findings.append(
            {
                "severity": pipeline.INFO,
                "check": "template_overlap",
                "detail": f"match rules overlap: {', '.join(expected)} all fit this job - the API chose '{assigned_name}'",
            }
        )

    if not _definition_named(definitions, assigned_name):
        findings.append(
            {
                "severity": pipeline.WARNING,
                "check": "template_unknown",
                "detail": (
                    f"'{assigned_name}' has no local definition, so its steps and agents were not verified"
                ),
            }
        )
    return findings


def _setup_logging(verbose):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
        stream=sys.stderr,
    )


# ------------------------------------------------------------------- commands


def cmd_doctor(args):
    client = PaulsJobClient()
    print(f"base url       : {client.base_url}")
    print(f"correlation id : {client.correlation_id}")

    name = account.company_name(client)
    print(f"company        : {name or '(unknown)'}")

    slug = account.resolve_person_slug(client)
    print(f"person slug    : {slug or '(none found - agents cannot be created)'}")

    credits = account.credits_snapshot(client)
    if credits:
        print(f"credits        : {credits['remaining']} remaining of {credits['total']}")

    reference = ReferenceData(client).load()
    print(f"reference data : {reference.summary()}")

    default_template = templates.get_company_default(client)
    print(f"default template: {default_template.get('Name') if default_template else '(none - AutoInit will 422)'}")

    company_templates = list(client.paginate_cursor(templates.TEMPLATES_PATH, "PipelineTemplates"))
    print(f"templates      : {len(company_templates)}")
    for template in company_templates:
        print(f"  - {template.get('Name')} ({template.get('ID')})")

    job_count = sum(1 for _ in client.paginate_pages(jobs_module.SEARCH_PATH, "Jobs", page_size=50))
    print(f"jobs           : {job_count}")
    print(f"api calls made : {client.calls}")
    return 0


def cmd_onboard(args):
    client = PaulsJobClient(dry_run=args.dry_run)
    payload = _load_yaml(args.jobs)
    job_list = payload.get("jobs") or []
    if not job_list:
        raise ConfigError(f"{args.jobs} contains no 'jobs:' entries")

    report = RunReport(client.correlation_id, dry_run=args.dry_run)
    reference = ReferenceData(client).load()
    person_slug = account.resolve_person_slug(client)
    definitions = _load_definitions(args.pipeline, reference)

    report.context["customer"] = payload.get("customer", "(unnamed)")
    report.context["pipelines"] = ", ".join(d["name"] for d in definitions)
    credits = account.credits_snapshot(client)
    if credits:
        report.context["credits"] = f"{credits['remaining']} remaining"

    # Every pipeline this customer has, each carrying the rules that decide
    # which jobs reach it. They all have to exist before the first job is
    # matched, because the API matches against what is stored on the template.
    for definition in definitions:
        _template, actions = templates.ensure_template(client, reference, definition, person_slug)
        report.template_actions.extend(actions)

    for job in job_list:
        external_id = str(job.get("external_id", "?"))
        job_actions = []
        try:
            # What the rules say this job should get. Worked out before anything
            # is sent, so the run can compare it against what the API does.
            expected = matching.predict(definitions, jobs_module.build_payload(job, reference, person_slug))

            record, created, action = jobs_module.ensure_job(client, reference, job, person_slug=person_slug)
            job_actions.append(action)
            job_id = jobs_module.job_id_of(record) or "dry-run-job-id"

            if args.dry_run:
                job_actions.append(
                    f"would be matched to: {', '.join(expected) or '(no rule matches - company default)'}"
                )
                report.add_job(external_id, job.get("title", ""), job_id, created, job_actions, [])
                continue

            # Init refuses an ownerless job, and a job created earlier by another
            # route may not have an owner yet, so this is checked every run.
            if jobs_module.ensure_job_owner(client, job_id, job.get("owner_slug") or person_slug):
                job_actions.append("assigned job owner")

            # AutoInit matches against the job search index, and that index
            # trails job creation by a few seconds. Initialising too early
            # matches nothing and quietly takes the company default instead.
            waited = pipeline.wait_until_searchable(client, external_id)
            if waited is None:
                job_actions.append(
                    f"job not searchable within {pipeline.SEARCHABLE_TIMEOUT_SECONDS}s - matching may fall back"
                )
            elif created:
                job_actions.append(f"searchable after {waited:.0f}s")

            pipeline.init_pipeline(client, job_id, auto_init=True, lang=args.lang)
            assigned = pipeline.assigned_template(client, job_id) or {}
            assigned_name = assigned.get("Name")
            job_actions.append(f"AutoInit assigned pipeline '{assigned_name or '(none)'}'")

            findings = _assignment_findings(definitions, assigned_name, expected)
            definition = _definition_named(definitions, assigned_name)
            if definition:
                state = pipeline.fetch_pipeline_state(client, job_id)
                findings.extend(pipeline.verify(state, definition))
                job_actions.append(f"verified {len(state['steps'])} steps against '{definition['name']}'")
            report.add_job(external_id, job.get("title", ""), job_id, created, job_actions, findings)

        except (ApiError, ConfigError) as exc:
            # One bad job must not abort the other 39. It is recorded and the
            # run continues, which is what makes a batch usable.
            report.add_job(external_id, job.get("title", ""), None, False, job_actions, [], error=str(exc))

    print(report.to_console())
    if not args.no_json:
        print(f"\nreport written to {report.write_json(args.out)}")
    return report.exit_code()


def cmd_verify(args):
    client = PaulsJobClient()
    payload = _load_yaml(args.jobs)
    reference = ReferenceData(client).load()
    definitions = _load_definitions(args.pipeline, reference)
    report = RunReport(client.correlation_id)
    report.context["customer"] = payload.get("customer", "(unnamed)")
    report.context["pipelines"] = ", ".join(d["name"] for d in definitions)
    report.context["mode"] = "verify only - nothing is created or changed"

    for job in payload.get("jobs") or []:
        external_id = str(job.get("external_id", "?"))
        try:
            record = jobs_module.find_by_external_id(client, external_id)
            if not record:
                report.add_job(external_id, job.get("title", ""), None, False, [], [], error="job does not exist")
                continue
            job_id = jobs_module.job_id_of(record)

            # Each job is checked against the pipeline it actually has, not one
            # chosen for it here. That is also how a job moved to another
            # pipeline in the UI shows up.
            expected = matching.predict(definitions, jobs_module.build_payload(job, reference))
            assigned_name = (pipeline.assigned_template(client, job_id) or {}).get("Name")
            actions = [f"pipeline '{assigned_name or '(none)'}'"]
            findings = _assignment_findings(definitions, assigned_name, expected)

            definition = _definition_named(definitions, assigned_name)
            if definition:
                state = pipeline.fetch_pipeline_state(client, job_id)
                findings.extend(pipeline.verify(state, definition))
                actions.append(f"checked {len(state['steps'])} steps against '{definition['name']}'")
            report.add_job(external_id, job.get("title", ""), job_id, False, actions, findings)
        except (ApiError, ConfigError) as exc:
            report.add_job(external_id, job.get("title", ""), None, False, [], [], error=str(exc))

    print(report.to_console())
    if not args.no_json:
        print(f"\nreport written to {report.write_json(args.out)}")
    return report.exit_code()


# --------------------------------------------------------------------- parser


def build_parser():
    parser = argparse.ArgumentParser(
        prog="onboard",
        description="Onboard a customer's jobs into Paul's Job: create jobs, initialise their "
        "pipelines from a template, and verify the result.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub_parser = parser.add_subparsers(dest="command", required=True)

    doctor = sub_parser.add_parser("doctor", help="check connectivity and what this account contains")
    doctor.set_defaults(func=cmd_doctor)

    jobs_default = "jobs.example.yaml"
    pipeline_help = (
        "pipeline definition, repeatable; a directory loads every *.yaml in it. "
        "Default: every definition in pipelines/"
    )

    onboard = sub_parser.add_parser("onboard", help="run the onboarding")
    onboard.add_argument("--jobs", default=jobs_default, help="YAML file of jobs to onboard")
    onboard.add_argument("--pipeline", action="append", help=pipeline_help)
    onboard.add_argument("--dry-run", action="store_true", help="show what would happen, write nothing")
    onboard.add_argument("--lang", default="de", help="language for step names created from the template")
    onboard.add_argument("--out", default="out", help="directory for the JSON report")
    onboard.add_argument("--no-json", action="store_true", help="skip the JSON report")
    onboard.set_defaults(func=cmd_onboard)

    verify = sub_parser.add_parser("verify", help="re-check already onboarded jobs, changing nothing")
    verify.add_argument("--jobs", default=jobs_default)
    verify.add_argument("--pipeline", action="append", help=pipeline_help)
    verify.add_argument("--out", default="out")
    verify.add_argument("--no-json", action="store_true")
    verify.set_defaults(func=cmd_verify)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"\nconfiguration problem:\n  {exc}\n", file=sys.stderr)
        return 2
    except PaulsJobError as exc:
        print(f"\nAPI problem:\n  {exc}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
