# Job Onboarding Automation — Paul's Job API

Takes the list of jobs a customer hands over at kickoff and brings each one to an
active, verified pipeline: **ensure template → create job → initialise pipeline →
verify → report**.

Built for the PJRI Technical Project Manager API challenge (Option 2).

---

## The problem it solves

At kickoff a customer hands over their open positions — usually an export from
their own ATS. Someone then creates each job in Paul's Job, picks the right
pipeline, initialises it, and clicks through the result to check that the steps
and agents are actually there. For forty jobs that is a day of clicking, and the
checking is the part that gets skipped when the day runs long.

This tool does the run and, more importantly, **states whether the result is
correct**. It ends with a per-job verdict — *ready* or *not ready, and here is
exactly what is missing* — because that is the question an implementation
manager has to answer at go-live, and "the API returned 200" does not answer it.

**Who benefits:** whoever runs the implementation (kickoff → go-live). Secondarily
Customer Success, who can re-run `verify` at any time to confirm that a
customer's pipelines still match what was agreed, without touching anything.

---

## Setup

Requires Python 3.11+.

```bash
git clone <this repo>
cd paulsjob-onboarding
pip install -r requirements.txt

cp .env.example .env      # then put your API key in .env
```

`.env` is gitignored. Generate the key in the Paul's Job dashboard under settings.

```
PAULSJOB_API_KEY=your_api_key_here
PAULSJOB_BASE_URL=https://api.paulsjob.ai/dev
```

Keep `PAULSJOB_BASE_URL` exactly as above — without the version segment. The
OpenAPI `servers` block lists `https://api.paulsjob.ai/dev/v1`; the client
appends `/v1` itself, so the env var stays the value the task specifies. (A base
that already ends in `/v1` is tolerated.)

Check the connection and see what your account contains:

```bash
py onboard.py doctor
```

---

## Usage

```bash
py onboard.py doctor                        # connectivity, auth, account contents
py onboard.py onboard --dry-run             # show what would happen, write nothing
py onboard.py onboard                       # the real run
py onboard.py verify                        # re-check existing jobs, change nothing
py -m unittest discover -s tests            # 30 tests, no network needed
```

Useful flags: `--jobs FILE`, `--pipeline FILE`, `--lang de`, `--out DIR`,
`--no-json`, `-v` for debug logging.

Exit codes: `0` all jobs ready · `1` at least one job needs attention ·
`2` configuration or API problem. So it can gate a CI step.

`scripts/probe.py` is the read-only reconnaissance script I used before writing
any of this — it hits a list of endpoints and prints response *shapes* rather
than contents. It is kept because it is how the account was surveyed in the
first place, and it is the fastest way to check a new account's contents.

To repeat a demo from zero (deletes only what the two input files describe):

```bash
py scripts/reset_demo.py            # preview
py scripts/reset_demo.py --confirm  # delete
```

### Input files

**`jobs.example.yaml`** — what the customer hands over. `external_id` is their
own job id and is what makes re-runs safe.

```yaml
customer: "Beispiel GmbH"
jobs:
  - external_id: "BSP-2026-001"
    title: "Senior Backend Engineer (m/w/d)"
    location: "Berlin"
    category: "IT"
    career_level: "Oberstufe"      # localised names and IDs both resolve
    working_hours: "FullTime"
    published: false
    description: |
      Wir suchen ...
```

**`pipelines/tech_de.yaml`** — the agreed configuration: steps and the agent on
each. This doubles as the expected state that `verify` checks against.

Reference values (`category`, `career_level`, `employment_type`,
`working_hours`, step `category`) accept either the ID or the human name, in
German or English, case-insensitively. They are resolved against the live API, so
nothing is hardcoded — and an unknown value fails **before any request** with the
list of valid options:

```
error: job 'BSP-2026-003' working_hours: 'Vollzeit (40h)' is not a valid value.
valid options: DualStudies (Duales Studium), Freelancer (Freiberuflich),
FullTime (Vollzeit), FullTimeOrPartTime (Vollzeit oder Teilzeit), ...
```

---

## Example output

Full captures are in [`docs/`](docs/):

| File | What it shows |
|---|---|
| [`example-doctor.txt`](docs/example-doctor.txt) | account state before a run |
| [`example-run-1-fresh.txt`](docs/example-run-1-fresh.txt) | first run: template, steps, agents and jobs created |
| [`example-run-2-rerun.txt`](docs/example-run-2-rerun.txt) | same command again: everything reused, nothing duplicated |
| [`example-run-3-verify.txt`](docs/example-run-3-verify.txt) | verification passing against live data |
| [`example-run-4-drift.txt`](docs/example-run-4-drift.txt) | verification catching six kinds of deviation |

A run against a fresh account, abbreviated:

```
Pipeline template
  created template 'Tech Recruiting DE' (01a07d00-9069-73a3-883b-a11f8135d7af)
    + step 'Neu' [New]
    + step 'Vorauswahl' [PreScreening]
      + agent 'Vorauswahl-Agent' (proactive)
    + step 'KI-Interview' [AIVoiceInterview]
      + agent 'Interview-Agent' (proactive)
    ...

Jobs (3)
  [OK  ] BSP-2026-001 - Senior Backend Engineer (m/w/d)
         created job 'BSP-2026-001' -> 182687
         initialised pipeline from template 01a07d00-...
         verified 7 steps

  [FAIL] BSP-2026-003 - Head of Engineering (m/w/d)
         error: job 'BSP-2026-003' working_hours: 'Vollzeit (40h)' is not a valid value.

ready: 2   needs attention: 1
```

And what verification reports when the live pipeline no longer matches:

```
WARN agent_type: step 'Vorauswahl': expected agent type reactive, live is proactive
WARN agent_prompt: step 'KI-Interview': agent prompt differs (24 chars expected, 375 live)
FAIL step_missing: step 'Fachgespräch' from the template is not present on the job
FAIL agent_missing: step 'Teamentscheidung': template defines agent 'Entscheidungs-Agent' but the job has none
note step_extra: step 'Persönliches Interview' exists on the job but is not in the template definition
```

Findings are graded: **FAIL** blocks go-live (missing step, missing or inactive
agent), **WARN** is a deviation worth a conversation (changed prompt, changed
human-in-loop, a job detached from its template), **note** is informational.
A JSON copy of every run lands in `out/` for attaching to a ticket.

---

## Endpoints used

| Purpose | Endpoint |
|---|---|
| Reference data | `GET /recruiting/jobs/{categories,career-levels,employment-types,working-hours}` |
| Step categories | `GET /recruiting/job-step-categories` |
| Account | `GET /company/profile`, `POST /company/person/list`, `GET /company/credits` |
| Templates | `GET,POST /recruiting/job-step-templates/pipelines` |
| Template steps | `GET,POST .../pipelines/{id}/steps` |
| Template agents | `GET,POST .../pipelines/{id}/steps/{stepId}/agents` |
| Company default | `GET /recruiting/job-step-templates/pipelines/default` |
| Job lookup | `GET /recruiting/jobs/by-external-id/{externalId}` |
| Job list | `POST /recruiting/jobs/search-jobs` |
| Job create | `POST /recruiting/jobs` |
| Job owner | `GET,POST /recruiting/jobs/{id}/hiring-managers` |
| Pipeline init | `POST /recruiting/jobs/{id}/steps/init` |
| Verification | `GET /recruiting/jobs/{id}/steps`, `.../steps/{stepId}/agents`, `.../steps/pipeline-template` |
| Cleanup script | `DELETE /recruiting/jobs/{id}`, `DELETE /recruiting/job-step-templates/pipelines/{id}` |

Auth is `x-company-api-key` on every call. Each request also carries
`paul-correlation-id` (one per run) and `paul-request-id` (per request), as the
API description asks — the correlation id is printed at the top of every run, so
a support question can point at one exact run.

---

## Assumptions

1. **A pipeline template has to be created.** The task says "match it with an
   appropriate pipeline template", but a fresh account has none: no company
   templates, an empty platform template library, and `GET .../pipelines/default`
   answers 404. So the tool creates the template from `pipelines/tech_de.yaml`
   if it is absent. That file is also the expected state for verification.
2. **The pipeline template is chosen explicitly, not by `AutoInit`.** `AutoInit`
   matches a job against each template's `FilterRulesElastic`, and with a single
   template and no filter rules there is nothing to match on — it answers 422.
   Passing the template id makes the run deterministic. Filter-rule matching is
   the natural next step (see below).
3. **Create and initialise are separate calls.** `POST /recruiting/jobs` accepts
   a `PipelineTemplateID` and would do both at once. Splitting them follows the
   task's stages and keeps the failure modes distinguishable — "job created but
   pipeline failed" is a different situation from "job not created".
4. **The account owner owns every job.** Overridable per job with `owner_slug`.
5. **Test jobs stay unpublished and unindexed.** `published: false` because the
   account feeds public job XML endpoints (`/job/xml-feed/stepstone`), and
   `Indexing: false` because it defaults to true and sends the job to the AI
   indexer immediately. Both are per-job settings in the input file.
6. **The agent prompt is compared verbatim.** Any edit shows as a deviation. That
   is deliberate for a go-live check; a semantic comparison would hide exactly
   the change you want to see.

---

## Tech note

### Error handling

- **Typed errors per status code** (`src/paulsjob/errors.py`), not string
  matching. The API runs two auth middlewares that word the same condition
  differently, so message text is not a reliable signal. 401 and 403 stay
  distinct: 401 means the key is wrong, 403 means the key is fine but not
  permitted (`/company/api-keys/available-scopes` needs a bearer token) — and
  only one of those is worth retrying.
- **404 is sometimes the expected answer.** A fresh account legitimately has no
  company default template, so that is read as "absent", not "broken".
- **Local validation before any write.** Unknown reference values, a missing
  `condition` on a `conditional` human-in-loop (the API answers 422), and
  category-specific required fields are all caught before the first request, with
  the valid options in the message.
- **One bad job does not stop the batch.** Each job is isolated; failures are
  collected and the run continues, which is the whole point of a batch tool. The
  report ends with what still needs attention and the exit code reflects it.
- **Retries** with exponential backoff and jitter on 429 and 5xx, honouring
  `Retry-After` when present. The spec documents no rate limits at all — no 429
  responses, no `Retry-After`, no `X-RateLimit` headers — but Cloudflare fronts
  the API, so undocumented limits certainly exist and silence is not a guarantee.
- **Idempotency on two levels.** Every job is looked up by `JobExternalID`
  before anything is created, so a re-run reuses instead of duplicating (see
  `example-run-2-rerun.txt`). On top of that, `paul-request-id` is derived
  deterministically from the external id, so a retry inside a run presents the
  same request id rather than looking like a second request.
- **Reconciliation, not just creation.** An existing template gets the steps and
  agents it is missing. This was not a design idea — it came from a run that
  created a template, added two steps, and then died on the third. A tool that
  skipped any template whose name it recognised would have left that half-built
  template broken forever.
- **The key never reaches a log line.** Every error message and traceback passes
  through a redaction helper, and run reports were checked for leakage.

### Pagination

Two different styles, handled behind one interface:

- `POST /recruiting/jobs/search-jobs` pages with `Page`/`PerPage` and reports `TotalPage`.
- Templates, agents and applications return an opaque `LastEvaluatedKey` cursor.
  It is base64 and contains `=`, `+` and `/`, so it must be URL-encoded when sent
  back; passing it raw is a quiet way to lose results. The cursor loop also stops
  if the same cursor is returned twice, so a server-side quirk cannot hang a run.

### What the API taught me that the spec did not

Everything below was found by running against the deployment and is reproducible:

1. **Three documented routes are gone.** `GET /recruiting/jobs`,
   `POST /recruiting/jobs/search` and `POST /recruiting/jobs/{id}/steps/pipeline-template/override`
   all return 404. All three are marked deprecated in the spec — the deployment
   has dropped them, the published spec has not. I verified each route with an
   unauthenticated request (401 = exists, 404 = gone) before depending on it.
2. **`POST /steps/init` requires a job owner**, answering
   `422 job must have a job owner before initializing pipeline template`. That
   prerequisite is not in the init endpoint's documentation. The `JobDetailsPayload`
   note that "the creator is added automatically" does not cover it either: a job
   created with a **company API key** has no creating person, so the job is
   ownerless and init refuses it. The tool sets `HiringManagers` at creation and
   repairs ownerless jobs on later runs.
3. **A job record carries three identifiers** and only one works in a URL:
   `PaulsjobJobID` (182687) is the `{paulsjob_job_id}` path parameter,
   `JobPositionID` (1) is a per-company counter, `JobExternalID` is the
   customer's. Using the wrong one produces `404: job not found` on a job that
   plainly exists. Note the casing: `PaulsjobJobID` in responses,
   `paulsjob_job_id` in the docs.
4. **`JobPositionDescription` is an object, not a string** — required field
   `JobRequirements`, optional `IdealCandidateProfile` and `AboutCompany`. The
   input file accepts either a plain text block or the structured form.
5. **`AIVoiceInterview` agents require `GreetingInstructions` and
   `EndOfCallProcedure`** inside `AdditionalFields`. Discoverable only by getting
   a 422 back, since the requirement lives in a `oneOf` variant selected by a
   discriminator. The tool now validates this locally.
6. **An agent on a job cannot be deactivated.** On
   `PUT /recruiting/jobs/{id}/steps/{stepId}/agents/{agentId}`, `IsActive: false`
   is rejected with `400 ['IsActive is required']` — and **omitting** the field
   produces the *identical* error, so the validator cannot tell "false" from
   "absent". `IsActive: true` gets past validation (it is then stopped at 422 by
   the permission check on a template-managed job), which places the asymmetry in
   request binding rather than in policy. The cause is visible in the spec:
   `AgentPayload.IsActive` carries `binding: "required"`, and Go's validator
   fails the zero value of a non-pointer bool. The template endpoint declares the
   same constraint but accepts `false` and persists it — so the two paths enforce
   the same rule differently. Consequence: a read-modify-write round trip on an
   inactive agent cannot succeed. Written up in `../API-Befund-IsActive.md`.
7. **Template-managed jobs are locked**, which is good: job-level agent edits
   answer `422 job agent update not allowed`. It also means drift cannot be
   induced through the API while a job is template-managed — so the drift demo
   in `docs/example-run-4-drift.txt` uses a changed *expectation* file rather
   than a faked live edit.
8. **The same concept uses different response keys** at template and job level:
   `JobStepAgentTemplates` vs `Agents`, `JobStepTemplates` vs a bare array.
9. **Cloudflare blocks the default Python user agent** with error 1010 before the
   request reaches the API. Any client needs to identify itself.
10. **The `LastEvaluatedKey` cursor is base64 of an internal DynamoDB key** and
    decodes to include the internal `CompanyID`. Harmless for a customer's own
    key, but it is internal structure leaking into a public response, and it is
    the reason run reports never log cursors.

### Limitations

- **Single-threaded.** Forty jobs take roughly forty times one job. Fine at
  kickoff scale, not for thousands.
- **No rollback.** If a job is created and its pipeline init then fails, the job
  stays. That is the safer default — the next run reuses and repairs it rather
  than deleting a job someone may already have touched — but it does mean a
  failed run can leave a job without a pipeline. The report says so explicitly.
- **Verification compares against the local definition file, not the live
  template.** So it answers "does this job match what we agreed", not "does this
  job match the template as it exists in Paul's Job right now". Comparing job
  against live template would catch template edits too.
- **Only agent fields the definition describes are compared** — name, type,
  active, human-in-loop, prompt. `Tools`, `NextStepRules`, `DeliverySettings` and
  `QuietHours` are created and read but not diffed.
- **Retries are not bounded by a global deadline**, only by attempt count.
- **Credit consumption is not fully accounted for.** Configuration-only runs did
  consume credits in testing (1000 → 997.36 over several runs) even with
  `Indexing: false` and no candidates. I could not attribute that to a specific
  call and did not want to guess. `PipelineWithoutAI: true` on a template is the
  documented way to run a pipeline that consumes none.
- **Tested against one account with one template.** Multi-entity behaviour
  (`EntityCustomID`) and pipeline template versioning are untouched.

### What I would do first in production

1. **Template matching via `FilterRulesElastic`.** The platform already matches
   jobs to templates by filter rules — that is what `AutoInit` uses. Setting
   those rules on the template and letting the API choose is better than the
   client naming a template id, and it makes "which pipeline does this job get"
   a customer-configurable decision rather than a line in a YAML file.
2. **Diff against the live template, not just the local file**, so template edits
   are caught as well. That is a small change to `verify` and it turns this into
   a genuine drift detector that Customer Success could run on a schedule.
3. **Concurrency with a shared rate limiter.** Per-job work is independent; a
   small worker pool with one token bucket across all workers would cut a
   40-job kickoff to minutes without risking the undocumented limits.
4. **A repair command.** Verification already knows exactly what is missing; the
   obvious next step is `onboard.py repair --job X` acting on those findings
   instead of a human reading them.
5. **A real config format decision.** YAML is right for a human-edited pipeline
   definition, but the job list should read the customer's actual CSV export
   directly, with a mapping file, rather than making someone convert it first.

---

## Security

- The API key is read only from the environment or `.env`. `.env` is gitignored
  (verified with `git check-ignore` before the key was ever created), and
  `.env.example` is committed in its place.
- The key is redacted from every error message and traceback.
- Run reports in `out/` are gitignored: they contain customer job data and
  pagination cursors that carry internal identifiers.
- No API key and no pagination cursor appears in any committed file. The captured
  example outputs do contain job and template ids — they are this throwaway test
  account's own demo data, and the jobs were never published.
