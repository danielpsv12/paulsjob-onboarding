"""Remove what a demo run created, so the run can be repeated from zero.

Deletes only the jobs listed in the given jobs file and the pipeline template
named in the given pipeline file - nothing else in the account is touched.

    py scripts/reset_demo.py                 # show what would be deleted
    py scripts/reset_demo.py --confirm       # actually delete
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import yaml  # noqa: E402

from paulsjob.client import PaulsJobClient  # noqa: E402
from paulsjob.errors import ApiError  # noqa: E402
from paulsjob.jobs import find_by_external_id, job_id_of  # noqa: E402
from paulsjob.templates import TEMPLATES_PATH, find_template_by_name  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jobs", default="jobs.example.yaml")
    parser.add_argument("--pipeline", default="pipelines/tech_de.yaml")
    parser.add_argument("--confirm", action="store_true", help="perform the deletion")
    args = parser.parse_args()

    client = PaulsJobClient()
    jobs_file = yaml.safe_load(Path(args.jobs).read_text(encoding="utf-8")) or {}
    pipeline_file = yaml.safe_load(Path(args.pipeline).read_text(encoding="utf-8")) or {}

    targets = []
    for job in jobs_file.get("jobs") or []:
        external_id = str(job.get("external_id"))
        record = find_by_external_id(client, external_id)
        if record:
            targets.append(("job", external_id, job_id_of(record), f"/recruiting/jobs/{job_id_of(record)}"))

    template = find_template_by_name(client, pipeline_file.get("name", ""))
    if template:
        targets.append(
            ("template", template.get("Name"), template.get("ID"), f"{TEMPLATES_PATH}/{template.get('ID')}")
        )

    if not targets:
        print("nothing to delete - the account is already clean")
        return 0

    for kind, label, identifier, path in targets:
        print(f"  {kind:9s} {label} ({identifier})")

    if not args.confirm:
        print("\ndry run - re-run with --confirm to delete these")
        return 0

    # Jobs first: a template still in use by a job cannot be removed.
    for kind, label, identifier, path in targets:
        try:
            client.delete(path)
            print(f"deleted {kind} {label}")
        except ApiError as exc:
            print(f"could not delete {kind} {label}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
