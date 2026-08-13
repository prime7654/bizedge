# Grievances API — notes for the frontend team

Read alongside `index.html` (open it in any browser) or `schema.yml` (generate
a client from it).

These are the things the schema cannot tell you. Most of them will otherwise
show up as a bug report.

---

## 1. Authentication

Session auth. Log in, and the session cookie carries the request. Every
endpoint returns 403 without it.

One extra requirement: the account must have an **Employee profile** linked to
it. Access is decided per employee, not per user, so a valid login with no
profile gets `403 No employee profile is linked to this account`.

## 2. `complainant` can be null — handle it

Two different reasons, and you cannot tell them apart from the payload:

- **The complaint was filed by HR on behalf of the company.** There is no
  individual complainant.
- **The viewer is the respondent and the identity is withheld.** The person
  complained about sees the allegation, not who made it, unless HR explicitly
  releases the identity.

Do not render "Filed by: —". Something like "Not disclosed" is closer to the
truth in both cases.

`respondent` is null on general complaints, which is normal.

## 3. HR does not see every complaint

The surprising one. `visibility` is genuinely three-way:

| Value | Who can see it |
|---|---|
| `HR` | HR only |
| `LINE_MANAGER` | The complainant's current line manager only |
| `BOTH` | HR and the line manager |

So an HR dashboard showing "all complaints" is under-reporting by design, and
**must not be labelled "all"**. It is "all complaints filed to HR".

Two behaviours that follow, both intentional:

- A complaint filed as `LINE_MANAGER` about the complainant's *own* manager is
  silently rerouted to HR. `visibility` will come back `HR` while
  `visibility_requested` shows what was chosen. Don't treat the difference as
  an error.
- If a `LINE_MANAGER` complaint would otherwise be visible to nobody, it falls
  back to HR.

## 4. `status` and `stage` are derived — read them, don't set them

The server stores one field, `state`. `status` and `stage` are computed from it
and are read-only.

| `state` | `status` | `stage` |
|---|---|---|
| `SUBMITTED` | Pending | N/A |
| `UNDER_INVESTIGATION` | Open | Investigation |
| `AWAITING_DECISION` | Open | Resolution |
| `RESOLVED` | Closed | Resolution |
| `WITHDRAWN` | Closed | N/A |

Filter with `?status=Open` or `?stage=Investigation` — the server maps them
back. Note that "Open" covers two states.

## 5. Two different detail shapes

`GET /complaints/{id}/` does not always return the same fields.

A respondent gets a **restricted** payload: no `witnesses`, no `attachments`,
no `filed_by`, no `visibility`, and no `complainant`. Those keys are **absent**,
not null.

Anyone else with access gets the full payload. Write your rendering
defensively — check for the key, don't assume it.

## 6. Fetch `/complaints/metadata/` on load

One call returns every dropdown option and, in `field_rules`, the conditional
logic behind the form: which fields become required when, which are forbidden,
minimums and maximums.

Drive your show/hide and validation from that rather than hardcoding it. The
option lists are fixed in code and changing them is a backend release, so a
hardcoded copy will be wrong eventually. `field_rules` is the same table the
server validates against.

## 7. Validation errors are keyed by form field

A 400 comes back as `{"incident_date": "Enter the date this happened."}` — keys
match the field names in the payload, so you can attach errors to inputs
directly. Some rules only fire in combination (repeat behaviour requires a
count; "Others" requires a note).

## 8. Lists

`GET /complaints/` is paginated (`count`, `next`, `previous`, `results`).

| Filter | Values |
|---|---|
| `relation` | `reported_by_me`, `against_me` — the employee tabs |
| `source_tab` | `employee`, `hr` — the HR console tabs |
| `status` | Pending, Open, Closed |
| `stage` | N/A, Investigation, Resolution |
| `type` | a `complaint_type` value |
| `reported_to` | a `visibility` value |
| `q` | reference or person name |
| `date_from`, `date_to` | ISO dates |
| `ordering` | `created_at`, `due_date`, `state`, `reference` (prefix `-`) |

Two things to know:

- **Search does not cover complaint descriptions.** Deliberate — searchable
  descriptions would let anyone with list access probe for content in cases
  they can only partly see.
- **Unknown filter values return nothing, not everything.** A typo gives an
  empty list rather than an unfiltered one.

`GET /complaints/summary/` gives tab and status counts in one call, computed
over the same visible set, so badges always match the list beneath them.

## 9. `due_date` is null until an investigator is appointed

HR sets it at triage. Expect null on anything still Pending.

## 10. Read `available_transitions`, don't hardcode the state machine

The complaint detail payload includes `available_transitions` — the moves that
are legal from the case's current state. Render your action buttons from that
rather than writing `if (state === 'SUBMITTED')` in the client.

Appointing an investigator returns **409 Conflict** if the case has already
moved on. That is the normal response to a double submission from a slow modal,
not an error worth surfacing as one — refetch and re-render.

## 11. The collaborator inbox is deliberately tiny

`/me/information-requests/` is what someone sees when they are asked to give
evidence. It returns the question, who asked it, and the case reference —
nothing else. No complaint description, no complainant, no other answers.

That is not an oversight, and please don't work around it. If a screen needs
more, raise it rather than pulling from another endpoint: a collaborator has no
access to the complaint or the investigation, and those calls will 404.

Answering twice returns **400**. Answering someone else's question returns
**404**, not 403 — a 403 would confirm the id exists.

## 12. HR can read an investigation but not write to it

Only the appointed lead can invite collaborators, ask questions, record
meetings or submit the report. HR gets 403 on those. Taking over means
reassigning the case, which leaves an audit trail.

## 13. Closing a case is one call, not four

`POST /complaints/{id}/resolution/` creates the decision, any PIP, its training
assignments and its follow-up schedule together. Send the whole thing in one
request — do not create the resolution and then add the PIP separately. There
is no endpoint for that, deliberately: a network drop halfway would leave a PIP
attached to a complaint that is still open.

If any part is invalid, nothing is saved and you get field errors back. Retry
the whole payload.

The formal/informal rules are strict and mirror database constraints:

| `resolution_type` | Required | Must be blank |
|---|---|---|
| `FORMAL` | `formal_resolution_type` | `informal_resolution_type` |
| `INFORMAL` | `informal_resolution_type` | `formal_resolution_type` |
| `NO_RESOLUTION_REQUIRED` | — | both |

Either sub-type set to `OTHERS` additionally requires `resolution_note`.

A PIP requires the complaint to name a respondent — a general complaint has
nobody for the plan to apply to.

Resolving an already-resolved case returns **409**.

## 14. Not built yet

Currently available: filing, listing, detail, attachments, timeline, metadata,
summary, and the employee/department/training pickers.

Also available now: appointing an investigator (`appoint-investigator`), which
opens the case, sets `due_date`, and is the point at which the respondent can
first see the complaint.

The investigation is now available too: collaborators, information requests,
meetings, notes and report submission.

Decision and resolution is now available, including PIP creation.

Still to come: withdrawal, the PIP follow-up reminders (they need a scheduled
task runner), and reopening.

Complaints filed as `LINE_MANAGER` can be created and viewed, but cannot yet be
progressed — who acts on those is an open product question.

---

Questions to Alfred. If something in the schema looks wrong, it probably is —
say so rather than working around it.
