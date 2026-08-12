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
- [ ] 4 — Lists, filters, detail views, metadata + summary endpoints
- [ ] 5 — State machine, triage, due date, respondent visibility
- [ ] 6 — Investigation
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

Browsable schema at `/api/docs/` — hand that URL to the frontend team so they
can mock against it while the rest lands.

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
