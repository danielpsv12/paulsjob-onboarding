# API finding: an agent on a job cannot be deactivated

**Endpoint:** `PUT /recruiting/jobs/{paulsjob_job_id}/steps/{job_step_id}/agents/{agent_id}`
**Environment:** `https://api.paulsjob.ai/dev/v1`, company API key
**Found:** 07.09.2026, while building the onboarding tool for the API challenge
**Re-verified:** 08.09.2026, all calls repeated, and on a second job as well

---

## Summary

`IsActive: false` is rejected with `400 ['IsActive is required']`. A payload that
**omits** the field is rejected with the **identical** error. So the validator
cannot distinguish "set to false" from "not provided", and `IsActive` can never
be set to `false` through this endpoint.

The equivalent endpoint on the template side accepts `false` without complaint.

---

## Reproduction

Same payload three times, only `IsActive` differs:

```
PUT /recruiting/jobs/182688/steps/01a07d00-ae64-.../agents/01a07d00-ae7d-...

  IsActive: true      -> 422  job agent update not allowed   [req d5176e7a-566a-44ca-8a5c-d8683be48920]
  IsActive: false     -> 400  ['IsActive is required']       [req 72a34cee-7996-4d88-909f-6c46e0969527]
  IsActive omitted    -> 400  ['IsActive is required']       [req 3d4e2162-4492-407f-9060-78491653e2ed]
```

The `422` on `true` is expected and correct — that job is template-managed, so
job-level edits are refused by design. It is also the useful part of the
evidence: `true` gets **past** validation and is stopped by the permission check,
while `false` never reaches it. The two responses come from different layers, so
the asymmetry sits in request binding and is independent of template management.

Repeating the three calls on 08.09.2026 gave the same three status codes. So did
the same three calls on a second job, created for the check and deleted after it,
with its own pipeline initialisation and its own agent — the behaviour is not a
property of this one job.

The same three calls against the template endpoint behave differently:

```
PUT /recruiting/job-step-templates/pipelines/{id}/steps/{stepId}/agents/{agentId}

  IsActive: true      -> 200
  IsActive: false     -> 200   (and the value persists: a later GET returns IsActive = false)
  IsActive omitted    -> 200   (and stores false: the constraint is not enforced here)
```

---

## Cause

`AgentPayload` in the OpenAPI spec declares:

```yaml
IsActive:
  type: boolean
  x-oapi-codegen-extra-tags:
    binding: "required"
```

With go-playground/validator, `required` on a **non-pointer `bool`** fails on the
zero value. `false` and "absent" are the same thing to the validator, which is
exactly what the two identical 400s show.

Worth noting: `JobStepAgentTemplate` declares the same `binding: "required"` on
its `IsActive`, yet that endpoint accepts `false`. It does not merely accept it —
it does not enforce the constraint at all. Setting the template agent to `true`
and then sending a PUT that **omits** `IsActive` returns `200`, and a later GET
reads back `false`.

So an absent field binds to the zero value on both paths. The only difference is
what happens next: the job path rejects that zero value, the template path stores
it. The two paths state the same constraint and enforce it differently, which
suggests the behaviour is an oversight rather than an intended rule.

---

## Impact

- An agent on a job cannot be switched off via the API. The only field that
  expresses "this agent is configured but should not run" is unsettable.
- Any client that reads an agent and writes it back unchanged will fail as soon
  as that agent is inactive — a read-modify-write round trip is not safe.
- The error message actively misleads. `IsActive is required` sends the caller
  looking for a missing field in a payload that plainly contains it.
- On the template endpoint the same declaration has the opposite failure mode: a
  payload that omits `IsActive` deactivates the agent silently, with a `200`.

## Suggested fix

Make the field a pointer (`*bool`) so the validator can tell `false` from absent,
or drop `required` and default it explicitly.

Exactly three boolean fields in the whole spec carry `binding: "required"`:
`AgentPayload.IsActive`, `JobStepAgentTemplate.IsActive` and
`CreateKeyPairPayload.HasExpiryDate`. The first two are the pair described above.
The third is untested here, because testing it means creating an API key.

`Published` and `Expired` on `JobDetailsPayload` are not part of this group,
which is worth stating explicitly: they sit in the schema's `required` list but
carry no `binding` tag at all, so no validator constraint applies to them. That
is why job creation with `Published: false` works.

---

## How this was found

Building the onboarding tool, I wanted to demonstrate the verification step
catching a deactivated agent, so I tried to deactivate one. The first attempt
returned `400 ['IsActive is required']` on a body that contained `IsActive`.
Isolating it meant sending the same payload with only that value changed, then
repeating it against the template endpoint to rule out the job's
template-managed state as the cause.

**Limit of the evidence:** because the test job was template-managed, `true`
returned `422` rather than `200`, so the `200` case is not measured. It cannot be
measured through this API. A job only gets steps from a pipeline template —
`POST /recruiting/jobs/{id}/steps` is deprecated and answers `404` on this
deployment — and the route that detaches a job from its template,
`POST /recruiting/jobs/{id}/steps/pipeline-template/override`, is deprecated and
answers `404` as well. Every job that can hold an agent is therefore
template-managed, and no reachable state lets a job-agent PUT past the permission
check. The ordering of the two responses (400 at binding, 422 at permission)
makes the inference sound, but it stays an inference.
