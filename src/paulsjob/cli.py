"""Command line entry point.

Three commands:

  doctor   - check connectivity, auth and what this account already contains
  onboard  - the actual run: ensure template, create jobs, init pipelines, verify
  verify   - re-check jobs that were onboarded earlier, without changing anything
"""

import argparse
import logging
import sys

import yaml

from . import account, jobs as jobs_module, pipeline, templates
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
    definition = _load_yaml(args.pipeline)
    payload = _load_yaml(args.jobs)
    job_list = payload.get("jobs") or []
    if not job_list:
        raise ConfigError(f"{args.jobs} contains no 'jobs:' entries")

    report = RunReport(client.correlation_id, dry_run=args.dry_run)
    reference = ReferenceData(client).load()
    person_slug = account.resolve_person_slug(client)

    report.context["customer"] = payload.get("customer", "(unnamed)")
    report.context["pipeline"] = definition.get("name", "(unnamed)")
    credits = account.credits_snapshot(client)
    if credits:
        report.context["credits"] = f"{credits['remaining']} remaining"

    template, actions = templates.ensure_template(client, reference, definition, person_slug)
    report.template_actions = actions
    template_id = (template or {}).get("ID") or "dry-run-template-id"

    for job in job_list:
        external_id = str(job.get("external_id", "?"))
        job_actions = []
        try:
            record, created, action = jobs_module.ensure_job(client, reference, job, person_slug=person_slug)
            job_actions.append(action)
            job_id = jobs_module.job_id_of(record) or "dry-run-job-id"

            if args.dry_run:
                report.add_job(external_id, job.get("title", ""), job_id, created, job_actions, [])
                continue

            # Init refuses an ownerless job, and a job created earlier by another
            # route may not have an owner yet, so this is checked every run.
            if jobs_module.ensure_job_owner(client, job_id, job.get("owner_slug") or person_slug):
                job_actions.append("assigned job owner")

            pipeline.init_pipeline(client, job_id, template_id=template_id, lang=args.lang)
            job_actions.append(f"initialised pipeline from template {template_id}")

            state = pipeline.fetch_pipeline_state(client, job_id)
            findings = pipeline.verify(state, definition)
            job_actions.append(f"verified {len(state['steps'])} steps")
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
    definition = _load_yaml(args.pipeline)
    payload = _load_yaml(args.jobs)
    report = RunReport(client.correlation_id)
    report.context["customer"] = payload.get("customer", "(unnamed)")
    report.context["mode"] = "verify only - nothing is created or changed"

    for job in payload.get("jobs") or []:
        external_id = str(job.get("external_id", "?"))
        try:
            record = jobs_module.find_by_external_id(client, external_id)
            if not record:
                report.add_job(external_id, job.get("title", ""), None, False, [], [], error="job does not exist")
                continue
            job_id = jobs_module.job_id_of(record)
            state = pipeline.fetch_pipeline_state(client, job_id)
            findings = pipeline.verify(state, definition)
            report.add_job(
                external_id, job.get("title", ""), job_id, False, [f"checked {len(state['steps'])} steps"], findings
            )
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
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check connectivity and what this account contains")
    doctor.set_defaults(func=cmd_doctor)

    common = dict(
        pipeline_default="pipelines/tech_de.yaml",
        jobs_default="jobs.example.yaml",
    )

    onboard = sub.add_parser("onboard", help="run the onboarding")
    onboard.add_argument("--jobs", default=common["jobs_default"], help="YAML file of jobs to onboard")
    onboard.add_argument("--pipeline", default=common["pipeline_default"], help="pipeline template definition")
    onboard.add_argument("--dry-run", action="store_true", help="show what would happen, write nothing")
    onboard.add_argument("--lang", default="de", help="language for step names created from the template")
    onboard.add_argument("--out", default="out", help="directory for the JSON report")
    onboard.add_argument("--no-json", action="store_true", help="skip the JSON report")
    onboard.set_defaults(func=cmd_onboard)

    verify = sub.add_parser("verify", help="re-check already onboarded jobs, changing nothing")
    verify.add_argument("--jobs", default=common["jobs_default"])
    verify.add_argument("--pipeline", default=common["pipeline_default"])
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
