"""The run report.

A colleague running this at a customer kickoff needs to answer one question
afterwards: is this customer ready to go live, and if not, what exactly is
missing. So the console output leads with that, and a JSON copy is written for
attaching to a ticket.
"""

import datetime
import json
import os

from .pipeline import CRITICAL, INFO, WARNING

MARKS = {CRITICAL: "FAIL", WARNING: "WARN", INFO: "note", None: "OK"}


class RunReport:
    def __init__(self, correlation_id, dry_run=False):
        self.correlation_id = correlation_id
        self.dry_run = dry_run
        self.started_at = datetime.datetime.now(datetime.timezone.utc)
        self.template_actions = []
        self.jobs = []
        self.context = {}

    def add_job(self, external_id, title, job_id, created, actions, findings, error=None):
        self.jobs.append(
            {
                "external_id": external_id,
                "title": title,
                "job_id": job_id,
                "created": created,
                "actions": actions,
                "findings": findings,
                "error": error,
            }
        )

    # ------------------------------------------------------------------ views

    @property
    def failed(self):
        return [j for j in self.jobs if j["error"] or any(f["severity"] == CRITICAL for f in j["findings"])]

    @property
    def ok(self):
        return [j for j in self.jobs if j not in self.failed]

    def exit_code(self):
        """Non-zero when something needs a human. Makes the tool usable in CI."""
        return 1 if self.failed else 0

    # --------------------------------------------------------------- rendering

    def to_console(self):
        lines = []
        add = lines.append
        banner = "DRY RUN - nothing was created" if self.dry_run else "onboarding run"
        add("=" * 72)
        add(f"Paul's Job {banner}")
        add(f"correlation id : {self.correlation_id}")
        for key, value in self.context.items():
            add(f"{key:15s}: {value}")
        add("=" * 72)

        if self.template_actions:
            add("")
            add("Pipeline template")
            for action in self.template_actions:
                add(f"  {action}")

        add("")
        add(f"Jobs ({len(self.jobs)})")
        for job in self.jobs:
            severity = CRITICAL if job["error"] else _worst(job["findings"])
            add("")
            add(f"  [{MARKS[severity]:4s}] {job['external_id']} - {job['title']}")
            if job["job_id"]:
                add(f"         job id: {job['job_id']}")
            for action in job["actions"]:
                add(f"         {action}")
            if job["error"]:
                add(_indent(f"error: {job['error']}", 9))
            for finding in job["findings"]:
                add(_indent(f"{MARKS[finding['severity']]:4s} {finding['check']}: {finding['detail']}", 9))

        add("")
        add("-" * 72)
        add(f"ready: {len(self.ok)}   needs attention: {len(self.failed)}")
        if self.failed:
            add("")
            add("Not ready to go live:")
            for job in self.failed:
                reason = job["error"] or "; ".join(
                    f["detail"] for f in job["findings"] if f["severity"] == CRITICAL
                )
                add(_indent(f"- {job['external_id']}: {reason}", 2))
        add("-" * 72)
        return "\n".join(lines)

    def to_dict(self):
        return {
            "correlation_id": self.correlation_id,
            "started_at": self.started_at.isoformat(),
            "dry_run": self.dry_run,
            "context": self.context,
            "template_actions": self.template_actions,
            "jobs": self.jobs,
            "summary": {
                "total": len(self.jobs),
                "ready": len(self.ok),
                "needs_attention": len(self.failed),
            },
        }

    def write_json(self, directory="out"):
        os.makedirs(directory, exist_ok=True)
        stamp = self.started_at.strftime("%Y%m%dT%H%M%SZ")
        path = os.path.join(directory, f"run-{stamp}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, ensure_ascii=False)
        return path


def _indent(text, width):
    """Keep multi-line messages (like a list of valid options) aligned."""
    pad = " " * width
    lines = str(text).splitlines() or [""]
    return "\n".join(pad + line.strip() if i else pad + line for i, line in enumerate(lines))


def _worst(findings):
    for severity in (CRITICAL, WARNING, INFO):
        if any(f["severity"] == severity for f in findings):
            return severity
    return None
