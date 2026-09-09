# Job Onboarding Automation — Paul's Job API

Takes the list of jobs a customer hands over at kickoff and brings each one to an
active, verified pipeline: **ensure templates → create job → let the API match it
to a pipeline → verify what it actually got → report**.

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

`.env` is gitignored. Grab the key in the Paul's Job dashboard under settings.

```
PAULSJOB_API_KEY=your_api_key_here
PAULSJOB_BASE_URL=https://api.paulsjob.ai/dev
```

Keep `PAULSJOB_BASE_URL` exactly as above without the version segment. 

Check the connection and see what your account contains:

```bash
py onboard.py doctor
```

**What a run does to the account:** it creates the seven templates from
`pipelines/` and the eight jobs from the jobs file, and `standard_de.yaml`
claims the company default — so on an account that already has a default
template, that flag moves to `Standard DE` (the API transfers the flag rather
than duplicating it, see finding 12). `py scripts/reset_demo.py --confirm`
removes exactly what the run created and nothing else.

---

## Usage

```bash
py onboard.py doctor                        # connectivity, auth, account contents
py onboard.py onboard --dry-run             # show what would happen, write nothing
py onboard.py onboard                       # the real run
py onboard.py verify                        # re-check existing jobs, change nothing
py -m unittest discover -s tests            # 68 tests, no network needed
```

Useful flags:

```bash
--jobs FILE          # the job list to read (default: jobs.example.yaml)
--pipeline FILE      # a pipeline definition; repeatable, and a directory loads every
                     #   *.yaml in it (default: all of pipelines/)
--lang de            # language for the step names created from the template   [onboard]
--dry-run            # show what would happen, write nothing                   [onboard]
--out DIR            # where the JSON report lands (default: out/)
--no-json            # console output only, no JSON report
-v                   # debug logging: py onboard.py -v onboard
```

The default `--pipeline` set is every definition in `pipelines/` *except* the
`*.drift-demo.yaml` variant, which exists only to feed the drift capture and
must be passed explicitly.

Exit codes: `0` all jobs ready · `1` at least one job needs attention ·
`2` configuration or API problem · `130` interrupted. So `onboard` and `verify`
can gate a CI step; `doctor` is diagnostic and returns `0` whenever it can reach
the API at all.

`scripts/probe.py` is the read-only reconnaissance script I used before writing
any of this — it hits a list of endpoints and prints response *shapes* rather
than contents. It is kept because it is how the account was surveyed in the
first place, and it is the fastest way to check a new account's contents.

To repeat a demo from zero (deletes only the jobs in the jobs file and the
templates named by the pipeline definitions, each with its version group —
nothing else in the account):

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

**`pipelines/*.yaml`** — one file per pipeline the customer runs: its steps, the
agent on each, and the rule that decides which jobs belong in it. Each file
doubles as the expected state that `verify` checks against. Seven are included,
and they are deliberately not variations of one process — a care home does not
hire the way a sales team does:

| Pipeline | Steps | Routed by | Shaped by |
|---|---|---|---|
| `standard_de.yaml` | 3 | nothing — **fallback only** | see below |
| `tech_de.yaml` | 7 | IT, from mid-level up | AI voice interview before the personal round |
| `werkstudent_de.yaml` | 5 | IT, entry level | short; no AI voice interview |
| `vertrieb_de.yaml` | 7 | Vertrieb | voice screen *then* a role-play — in sales the call is part of the assessment |
| `marketing_de.yaml` | 7 | Marketing und Kommunikation | a `DataCollection` step for work samples, so portfolios stop living in mail attachments |
| `pflege_de.yaml` | 5 | the four nursing categories | the shortest one: care hiring is a race, so a `RecruitingDay` (Hospitationstag) replaces the interview rounds |
| `verwaltung_de.yaml` | 6 | the three administration categories | weight on pre-screening, exactly one personal interview |

An eighth file, `tech_de.drift-demo.yaml`, is not a pipeline: it is a copy of
`tech_de.yaml` with deliberate deviations, used only to produce the drift
capture. It claims the same template name, so it is excluded from the default
load and must be passed explicitly.

The category list this account exposes is dominated by care, cleaning, facility
and security roles — that is the market Paul's Job serves, so those are the
pipelines a real kickoff needs.

**`standard_de.yaml` is the fallback, and it is deliberately not a hiring
process.** Whatever is the company default is what every unmatched job receives,
so that template should mean one thing: *nobody has decided yet how this role is
hired*. It carries no `match:` block (nothing routes to it by rule — it is only
ever reached by falling through), no interview, no offer and no agent, and
`pipeline_without_ai: true` guarantees a misrouted job cannot spend a credit
while it waits. "Job is in Standard DE" then reads as "matched nothing", in the
report and to anyone looking in the Paul's Job UI.

`tech_de.yaml` used to hold that role, which was wrong: its pre-screening agent
opens with *"Du führst die Vorauswahl für eine technische Position durch"*, so an
unmatched Küchenhilfe was screened on her project experience with the
technologies named in the job ad — by an AI voice interview that costs money.
`BSP-2026-008` in the job list exists to demonstrate exactly this path.

The `match:` block is the part that routes a job:

```yaml
match:
  category: "IT"
  career_level: ["Oberstufe", "Mittleres Niveau", "Führen"]
```

A list means *any of these*, a single value means *exactly this*, and the fields
are ANDed. The rules live on the template as `FilterRulesElastic`, and **the API
matches each job against them when the pipeline is initialised — this tool never
names a template for a job.** See [Assumptions](#assumptions) for why that is
worth insisting on, and what it costs.

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
| [`example-doctor.txt`](docs/example-doctor.txt) | what the account holds, and whether auth and reference data work |
| [`example-run-1-fresh.txt`](docs/example-run-1-fresh.txt) | first run: seven templates created, six jobs matched to their pipeline, one falling through to the default |
| [`example-run-2-rerun.txt`](docs/example-run-2-rerun.txt) | same command again: everything reused, nothing duplicated |
| [`example-run-3-verify.txt`](docs/example-run-3-verify.txt) | verification against live data: six jobs on the right pipeline, one in the fallback |
| [`example-run-4-drift.txt`](docs/example-run-4-drift.txt) | verification catching six kinds of deviation |

A run against a fresh account, abbreviated:

```
Pipeline template
  created template 'Standard DE' (01a080fe-87fb-7a5a-ad6a-fbc33499fb81)
    match rules: none - reachable only as the company default or by explicit id
    + step 'Neu' [New]
    + step 'Zuordnung offen' [TeamDiscussion]
    + step 'Abgelehnt' [Rejected]
  created template 'Tech Recruiting DE' (01a080fe-89c6-7e61-ac5c-7f5e61b855b7)
    match rules: job_career_level_id in SeniorLevel, MidLevel, Lead AND job_category_id is IT
    + step 'Neu' [New]
    + step 'Vorauswahl' [PreScreening]
      + agent 'Vorauswahl-Agent' (proactive)
    ...
  created template 'Werkstudenten DE' (01a080fe-94e9-7b99-8851-6f7ab93510c0)
    match rules: job_career_level_id is EntryLevel AND job_category_id is IT
    ...

Jobs (8)
  [OK  ] BSP-2026-001 - Senior Backend Engineer (m/w/d)
         created job 'BSP-2026-001' -> 182838
         searchable after 2s
         AutoInit assigned pipeline 'Tech Recruiting DE'
         verified 7 steps against 'Tech Recruiting DE'

  [OK  ] BSP-2026-004 - Account Executive Neukundengeschäft (m/w/d)
         AutoInit assigned pipeline 'Vertrieb DE'
         verified 7 steps against 'Vertrieb DE'

  [OK  ] BSP-2026-006 - Pflegefachkraft Wohnbereich 2 (m/w/d)
         AutoInit assigned pipeline 'Pflege DE'
         verified 5 steps against 'Pflege DE'
    ...

  [WARN] BSP-2026-008 - Küchenhilfe (m/w/d)
         AutoInit assigned pipeline 'Standard DE'
         verified 3 steps against 'Standard DE'
         WARN template_fallback: no match rule covers this job, so the API fell
              back to 'Standard DE' (the company default).

  [FAIL] BSP-2026-003 - Head of Engineering (m/w/d)
         error: job 'BSP-2026-003' working_hours: 'Vollzeit (40h)' is not a valid value.

ready: 7   needs attention: 1
```

Six jobs from one list, six different pipelines, and the API assigned every one
of them — it matched each job against the rules stored on the templates. The
differing step counts are the point: these are genuinely different processes,
not one process under six names. The seventh job matched nothing, and says so.

And what verification reports when the live pipeline no longer matches:

```
WARN agent_type: step 'Vorauswahl': expected agent type reactive, live is proactive
WARN agent_prompt: step 'KI-Interview': agent prompt differs (24 chars expected, 375 live)
FAIL step_missing: step 'Fachgespräch' from the template is not present on the job
FAIL agent_missing: step 'Teamentscheidung': template defines agent 'Entscheidungs-Agent' but the job has none
note step_extra: step 'Persönliches Interview' exists on the job but is not in the template definition
```

Findings are graded: **FAIL** blocks go-live (missing step, missing or inactive
agent, an agent on a step that should have none, a job on the wrong pipeline),
**WARN** is a deviation worth a conversation (changed prompt, changed
human-in-loop, a second agent beside the agreed one, a job that matched no rule
and took the default), **note** is informational. Nineteen checks in total, each
one covered by a test.
A JSON copy of every run lands in `out/` for attaching to a ticket.

---

## Endpoints used

| Purpose | Endpoint |
|---|---|
| Reference data | `GET /recruiting/jobs/{categories,career-levels,employment-types,working-hours}` |
| Step categories | `GET /recruiting/job-step-categories` |
| Account | `GET /company/profile`, `POST /company/person/list`, `GET /company/credits` |
| Templates | `GET,POST /recruiting/job-step-templates/pipelines`, `GET,PUT .../pipelines/{id}` |
| Template steps | `GET,POST .../pipelines/{id}/steps` |
| Template agents | `GET,POST .../pipelines/{id}/steps/{stepId}/agents` |
| Company default | `GET /recruiting/job-step-templates/pipelines/default` |
| Job lookup | `GET /recruiting/jobs/by-external-id/{externalId}` |
| Job list | `POST /recruiting/jobs/search-jobs` |
| Job create | `POST /recruiting/jobs` |
| Job owner | `GET,POST /recruiting/jobs/{id}/hiring-managers` |
| Pipeline init | `POST /recruiting/jobs/{id}/steps/init` with `AutoInit` |
| Verification | `GET /recruiting/jobs/{id}/steps`, `.../steps/{stepId}/agents`, `.../steps/pipeline-template` |
| Cleanup script | `DELETE /recruiting/jobs/{id}`, `DELETE .../pipelines/{id}`, `GET,DELETE .../pipeline-version-groups/{rootId}` |

`GET /recruiting/jobs/filters` is deliberately **not** in that list: it is where
the filter keys and operators in `src/paulsjob/matching.py` come from, but the
tool does not call it at runtime — a kickoff run should not depend on a lookup
whose answer is fixed at the time the match fields were chosen.

Auth is `x-company-api-key` on every call. Each request also carries
`paul-correlation-id` (one per run) and `paul-request-id` (per request), as the
API description asks — the correlation id is printed at the top of every run, so
a support question can point at one exact run.

---

## Assumptions

1. **The pipeline templates have to be created first.** The task says "match it
   with an appropriate pipeline template", but a fresh account has nothing to
   match against: no company templates, an empty platform template library, and
   `GET .../pipelines/default` answers 404. So the tool creates each template
   from `pipelines/*.yaml` if it is absent, including the match rules — those
   rules are what the platform matches on, so a template without them can only
   be reached as the company default.
2. **The match is the platform's decision, not the tool's.** Each template
   carries its rules as `FilterRulesElastic`, and the job is initialised with
   `AutoInit: true`; the API picks the template. The alternative — naming a
   template id per run — is deterministic but puts "which pipeline does this job
   get" in a YAML file instead of in the customer's configuration, where
   Customer Success can change it without us.

   This costs something, and the cost is the reason for two safeguards. Matching
   runs against the job **search index**, which trails job creation by a few
   seconds, and a job that matches nothing is not an error: init answers `200`
   and quietly assigns the company default. So the tool (a) waits until the job
   is findable in the job search before initialising, polling the same index the
   matching reads, and (b) reads back which template the job actually received
   and compares it against the rules. A job that lands on the wrong pipeline is
   reported as `template_mismatch` and fails the run. Without the readback, "the
   API returned 200" would again mean nothing — see finding 11.
3. **The default template is a holding pen, not a hiring process.** Since the
   company default is what every unmatched job silently receives, the safest
   thing it can be is obviously incomplete: `standard_de.yaml` has three steps,
   no agent and no AI, so a misrouted job stops and waits for a person instead
   of being run through the wrong process at the customer's expense. The
   trade-off is deliberate — a job that falls through does *not* progress on
   its own. The alternative, a plausible generic pipeline, keeps candidates
   moving but hides the missing rule; here the run reports `template_fallback`
   and the job visibly sits still until someone acts.
4. **Create and initialise are separate calls.** `POST /recruiting/jobs` accepts
   a `PipelineTemplateID` and would do both at once. Splitting them follows the
   task's stages and keeps the failure modes distinguishable — "job created but
   pipeline failed" is a different situation from "job not created".
5. **The account owner owns every job.** Overridable per job with `owner_slug`.
6. **Test jobs stay unpublished and unindexed.** `published: false` because the
   account feeds public job XML endpoints (`/job/xml-feed/stepstone`), and
   `Indexing: false` because it defaults to true and sends the job to the AI
   indexer immediately. Both are per-job settings in the input file.
7. **The agent prompt is compared verbatim.** Any edit shows as a deviation. That
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
  Since the deployment never rate-limited me, this path is exercised against a
  stub session instead: that a 429 waits exactly as long as the server asked,
  that backoff grows without a `Retry-After`, that a retry re-sends the *same*
  `paul-request-id` rather than looking like a second write, and that a 422 is
  never retried. Otherwise it would be code that has never run.
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
   The body settles it: these 404s are the router's plain `404 page not found`,
   returned before any handler runs, while a live route answers JSON
   (`{"message":"unauthorized, ...","status":401}`) and a handler's own 404 reads
   `job not found` in JSON — so the route is unregistered, not a handler saying
   the record is missing.
2. **`POST /steps/init` requires a job owner**, answering
   `422 job must have a job owner before initializing pipeline template`. That
   prerequisite is not in the init endpoint's documentation. The `JobDetailsPayload`
   note that "the creator is added automatically" does not cover it either: a job
   created with a **company API key** has no creating person, so the job is
   ownerless and init refuses it. The tool sets `HiringManagers` at creation and
   repairs ownerless jobs on later runs.
3. **A job record carries three identifiers** and only one works in a URL:
   `PaulsjobJobID` (182838) is the `{paulsjob_job_id}` path parameter,
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
   same constraint and does not enforce it at all: it accepts `false`, and a PUT
   that omits the field also returns `200` and stores `false`. An absent field
   binds to the zero value on both paths — the job path then rejects that value,
   the template path stores it. Two consequences: a read-modify-write round trip
   on an inactive agent cannot succeed, and on the template side a payload that
   leaves `IsActive` out deactivates the agent silently. Only three booleans in
   the whole spec carry `binding: "required"`, and two of them are this pair.
   Written up in `docs/API-Befund-IsActive.md`.
7. **Template-managed jobs are locked**, which is good: job-level agent edits
   answer `422 job agent update not allowed`. It also means drift cannot be
   induced through the API while a job is template-managed — so the drift demo
   in `docs/example-run-4-drift.txt` uses a changed *expectation* file rather
   than a faked live edit.
8. **The same concept uses different response keys** at template and job level:
   `JobStepAgentTemplates` vs `Agents`, `JobStepTemplates` vs a bare array. The
   template *list* also returns fewer fields than the detail endpoint: it omits
   `FilterRulesElastic` entirely, so comparing a template's stored rules against
   the definition has to re-fetch it by id. Reusing the record found by name
   reads every template as having no rules, and rewrites them on every run.
9. **Cloudflare blocks the `Python-urllib` user agent** with error 1010 before the
   request reaches the API — a 403 carrying an HTML block page instead of the
   API's JSON envelope. It is that one user agent rather than Python in general:
   on re-measurement `python-requests`, `curl/8.4.0` and a request sending no
   `User-Agent` header at all each reach the API and get its own 401. So this is
   a user-agent blocklist, not a requirement to identify yourself — but it is
   still worth knowing, because the 403 looks nothing like an API error and
   `urllib` is what a quick probe script reaches for first.
10. **The `LastEvaluatedKey` cursor is base64 of an internal DynamoDB key** and
    decodes to include the internal `CompanyID`. Harmless for a customer's own
    key, but it is internal structure leaking into a public response, and it is
    the reason run reports never log cursors.
11. **`AutoInit` matching depends on the job search index, and misses silently.**
    A job created and initialised immediately matches no rule and is given the
    company default; the same job initialised a few seconds later matches
    correctly. Same job, same template, only the delay differs:

    ```
    wait  0s before init -> 200, fell back to the default template
    wait  5s before init -> 200, matched its filter rule
    wait 20s before init -> 200, matched its filter rule
    ```

    The response is `200` either way and nothing in it says which template was
    used, so an onboarding tool that trusts the status code puts customers on
    the wrong hiring process. Polling the job search until the job appears is a
    reliable gate (it is the same index), and reading the assignment back is the
    only way to be sure. Both are in the run.
12. **The company default is exclusive, and deleting it leaves none.** Marking a
    template as default transfers the flag rather than duplicating it — two
    templates can never both be default, so handing the role over is a single
    write. But deleting whichever template holds it leaves the account with no
    default at all, and from then on an unmatched job gets `422` from init
    instead of a fallback. Nothing warns about that, which makes "delete this
    old template" a quietly load-bearing action. `ensure_template` therefore
    reconciles `is_default` as well as the match rules, so a definition can
    both claim and give up the role.
13. **`FilterRulesElastic` is validated server-side, but only says so vaguely.**
    A career level of `Senior` instead of `SeniorLevel` is refused with
    `400 invalid filter rules`, without naming the offending value. The tool
    resolves match values against the live reference lists first, so a wrong
    word fails locally with the list of valid options.
14. **Deleting a pipeline template leaves its version group behind**, and the
    dashboard lists version groups. So a deleted template keeps appearing under
    its own name with version 0, next to the real one — it reads as a duplicate
    template, which is how I found it: after several demo resets the account
    showed 21 version groups for 7 templates. The template list endpoint shows
    none of this, because it returns templates, not groups.

    ```
    delete the template only      -> 1 version group left behind, latest version 0
    delete the template + group   -> 0 left
    delete the group while the template lives -> refused, 400
    ```

    The refusal is the useful half: a group cannot be removed while it still
    holds versions, so the correct order is template first, group second. The
    reset script now does both, and `DELETE .../pipelines/{id}` on its own
    should probably be considered incomplete cleanup by any client.

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
  `QuietHours` are created and read but not diffed. An agent the definition does
  not mention at all *is* caught (`agent_extra`), which is the asymmetry that
  matters: a missing agent is a broken promise, an unannounced one is an AI
  talking to candidates that nobody agreed to.
- **Retries are not bounded by a global deadline**, only by attempt count.
- **Credit consumption is not fully accounted for.** Configuration-only runs did
  consume credits in testing (1000 → 988.87 over every run behind this repo,
  including several full resets) even with `Indexing: false` and no candidates. I could not attribute that to a specific call and did not want to guess. `PipelineWithoutAI: true` on a template is the documented way to run a pipeline that consumes none.
- **Tested against one account with seven templates.** Multi-entity behaviour
  (`EntityCustomID`) is untouched, and pipeline template versioning nearly so:
  updating a template is an in-place write — the id and `Version: 1` stay, the
  steps survive, and no second version appears — but what does create a
  version 2 (the schema carries `VersionRootPipelineTemplateID`, and there is a
  separate template-switch endpoint) I did not establish. A customer whose
  templates are versioned may behave differently.

### What I would do first in production

1. **Ask Paul's Job to make the matching observable.** The response to
   `POST /steps/init` says `success` and nothing else — not which template was
   chosen, nor whether a rule matched or the default was used. One field in that
   response would remove the readback and the index-lag wait from every client
   that does this, mine included. Failing that, a `MatchedByRule: true|false`
   on the pipeline-template endpoint would do.
2. **Diff against the live template, not just the local file**, so template edits
   are caught as well. That is a small change to `verify` and it turns this into
   a genuine drift detector that Customer Success could run on a schedule.
3. **A repair command.** Verification already knows exactly what is missing; the
   obvious next step is `onboard.py repair --job X` acting on those findings
   instead of a human reading them.
4. **A real config format decision.** YAML is right for a human-edited pipeline
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
