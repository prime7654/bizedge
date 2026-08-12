# BizEdge — Grievances / Case Management

Backend for the complaints and investigations workflow across MAKAY (employee
app) and Bizedge (HR console).

Built from `grievances-workflow-v4-BUILD.md`. Read section 2 (visibility) before
touching anything that queries complaints.

## Stack

Django 5 · DRF · Postgres 16 · Celery + Redis · Docker Compose

## Getting started

```bash
cp .env.example .env          # edit SECRET_KEY at minimum
docker compose up --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
```

API docs at `/api/docs/`, admin at `/admin/`.

## Layout

```
config/           project settings (base / dev / prod), urls, wsgi
apps/core/        tenancy, timestamps, soft-delete base models
apps/directory/   STUB Employee / Department / Training — replaced at merge
apps/grievances/  the module itself
```

## Merging into the platform later

This is standalone today. Every platform-owned model is referenced through a
setting rather than imported:

| Setting | Default |
|---|---|
| `GRIEVANCES_EMPLOYEE_MODEL` | `directory.Employee` |
| `GRIEVANCES_DEPARTMENT_MODEL` | `directory.Department` |
| `GRIEVANCES_TRAINING_MODEL` | `directory.Training` |
| `GRIEVANCES_ORGANISATION_MODEL` | `core.Organisation` |

At merge: point these at the real models, drop `apps.directory`, run migrations.
Never import a platform model directly inside `apps.grievances` — use
`apps.core.platform`.

## Rules that are not negotiable

**`Complaint.state` is the only lifecycle field.** The UI shows Status, Stage
and Decision as three columns; they are derived from this one field. Storing
them separately produces impossible rows like "Closed / Investigation".

**Visibility is genuinely three-way.** HR does *not* see `LINE_MANAGER`
complaints. Any "all complaints" count or export will legitimately under-report.

**Nothing is hard deleted.** `objects` hides soft-deleted rows; `all_objects`
sees them. Deletion is permitted only while `SUBMITTED`, and only to the HR user
who created the record.

**`ComplaintEvent` is append-only.** A grievance record is evidence. Never
update or delete a row in that table.

**Line manager is resolved at query time**, never copied onto a complaint —
access follows the employee's *current* manager. A respondent must never gain
line-manager visibility of a complaint against themselves.

## Build progress

- [x] 1 — Schema, migrations, admin, tenancy, `ComplaintEvent`
- [x] 2 — `ComplaintAccessPolicy` + object-level permissions + orphan fallback
- [x] 3 — Intake: four variants, conditional validation, intake override
- [x] 4 — Lists, filters, detail views, metadata + summary endpoints
- [x] 5 — State machine, triage, due date, respondent visibility
- [x] 6 — Investigation
- [ ] 7 — Decision & resolution
- [ ] 8 — Withdrawal, both paths
- [ ] 9 — PIP, follow-ups, Celery reminders
- [ ] 10 — Reopen
- [ ] 11 — Soft delete authorization
- [ ] 12 — LM-only triage — deferred, awaiting Product

Build step 2 before any endpoint. Retrofitting permissions is how leaks happen.

## Endpoints so far

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/v1/complaints/` | File a complaint — all four routes |
| `GET` | `/api/v1/complaints/` | List, filtered by the access policy |
| `GET` | `/api/v1/complaints/{id}/` | Detail; shape depends on access level |
| `POST` | `/api/v1/complaints/{id}/attachments/` | Attach evidence |
| `GET` | `/api/v1/complaints/metadata/` | Every dropdown and form rule, in one call |
| `GET` | `/api/v1/complaints/summary/` | Tab and status counts |
| `GET` | `/api/v1/complaints/{id}/timeline/` | Case history — full access only |
| `GET` | `/api/v1/employees/` | Employee picker (`?q=`, `?department=`, `?exclude_self=`) |
| `GET` | `/api/v1/departments/` | Department picker |
| `GET` | `/api/v1/trainings/` | Training catalogue for PIPs |
| `POST` | `/api/v1/complaints/{id}/appoint-investigator/` | Open the case — HR only |
| `GET` | `/api/v1/complaints/{id}/investigations/` | Rounds, including superseded ones |
| `GET` | `/api/v1/investigations/{id}/` | The lead's workspace |
| `POST` | `/api/v1/investigations/{id}/collaborators/` | Invite someone (safe to repeat) |
| `DELETE` | `/api/v1/investigations/{id}/collaborators/{cid}` | Soft-remove |
| `POST` | `.../collaborators/{cid}/request-information/` | Ask a question |
| `POST` | `/api/v1/investigations/{id}/meetings/` | Record a meeting |
| `POST` | `/api/v1/investigations/{id}/notes/` | Add a note |
| `POST` | `/api/v1/investigations/{id}/submit-report/` | Hand back to HR |
| `GET` | `/api/v1/me/information-requests/` | A collaborator's inbox |
| `POST` | `/api/v1/me/information-requests/{id}/respond/` | Answer a question |

**Filters on the list endpoint:** `relation` (`reported_by_me` / `against_me`),
`source_tab` (`employee` / `hr`), `status`, `stage`, `type`, `reported_to`,
`state`, `q`, `date_from`, `date_to`, `ordering`.

`status` and `stage` are derived from `state`, so those filters map a display
value back to the states it covers. An unrecognised value returns nothing
rather than everything — filters fail closed.

Live docs at `/api/docs/` while the server is running (localhost only).

To share with the frontend team, run `make docs`. That regenerates:

- `docs/api/schema.yml` — the contract; committed, so API changes show in diffs
- `docs/api/index.html` — self-contained, opens in any browser, nothing to install

Send them `index.html` and `docs/api/API-NOTES.md` together. The notes cover the
behaviour the schema cannot express — nullable `complainant`, three-way
visibility, derived `status`/`stage`, and the two different detail shapes.

**Re-run `make docs` after any change to serializers, views or routes.** A stale
schema is worse than none, because people trust it. `make docs-check` fails if
the committed copy has drifted.

## Tests

```bash
docker compose exec web pytest
docker compose exec web python scripts/verify_constraints.py
```

The access tests in `apps/grievances/tests/test_access.py` are the ones that
matter. A bug there does not raise — it quietly shows a grievance to someone who
should never have seen it. `test_object_and_queryset_rules_never_disagree`
exists because `can_view()` and `visible_queryset()` are separate
implementations of one rule, and drift between them is a leak.

## Code standards

This project follows the `robust-code` practice. In short: handle the input
nobody pictured, fail loudly at the boundary, and never swallow an error you
cannot act on.

The specifics that matter here:

- **Validate at the edge.** Anything arriving from a client — request body,
  header, filename, query param — is validated before it reaches the database.
  Past that boundary, code can trust its inputs.
- **Catch narrow.** One deliberate broad handler exists, in
  `notifications.notify`, and it is documented: a failed notification must
  never roll back the transaction that triggered it.
- **Bound external input.** Search terms, user agents and filenames are
  length-capped. Uploads are checked on size, MIME type and extension.
- **Services accept loose input.** They are called from the API, the admin,
  management commands and imports, so they coerce dates rather than assuming
  DRF already parsed them.
- **Test the failure, not the happy path.** Roughly one test per handled
  failure mode. `test_hardening.py` covers hostile input specifically.
- **Mutation-test the guards.** After a suite goes green, break each guard and
  confirm a test fails. Two real bugs in this codebase were found that way and
  would not have been found otherwise.

### A caveat about SQLite

The suite runs on Postgres in the container. Do not switch it to SQLite for
speed: SQLite silently accepts values Postgres rejects, and at least one
production-breaking bug here (an unvalidated `X-Forwarded-For` reaching an
`inet` column) was invisible under SQLite.
