# CLAUDE.md — BizEdge Grievances

Read this before writing code in this repo.

---

## Standing practice: robust-code

**The `robust-code` skill applies to every change in this project, without being
asked.** Before writing anything: size the response to the request, read the
neighbouring files, spend thirty seconds on failure modes, then write the
smallest thing that handles them.

The parts that have already earned their keep here:

- **Fail loudly at the boundary.** Validate where data enters — request body,
  header, filename, query param — then downstream code can trust its inputs.
- **Catch narrow.** One deliberate broad handler exists, in
  `notifications.notify`, and it is documented. Don't add a second without a
  reason you can say out loud.
- **Error messages name the offending value.** `f"{value!r} is not a valid
  date"` ends the investigation; "invalid input" starts one.
- **Bound external input.** Search terms, prompts, findings, user agents and
  filenames are all length-capped.
- **Test the failure, not the happy path.** Roughly one test per handled
  failure mode.

### Mutation-test the guards

After a suite goes green, break each guard you added and confirm a test fails.
**Three real bugs in this codebase were found that way and would not have been
found otherwise.** A guard with no failing test is decoration.

```bash
# pattern: back up, break one guard, run the targeted tests, restore
cp apps/grievances/access.py /tmp/x.bak
# ...remove the check...
docker compose exec -T web pytest apps/grievances/tests/test_access.py -q
cp /tmp/x.bak apps/grievances/access.py
```

### Never trust SQLite

**The suite runs on Postgres in the container. Do not switch it to SQLite for
speed.** SQLite silently accepts values Postgres rejects. A production-breaking
bug here — an unvalidated `X-Forwarded-For` reaching an `inet` column, which
would have 500'd every audited endpoint — was completely invisible under
SQLite.

Verify in the container, not in a local or sandboxed interpreter:

```bash
docker compose exec web pytest
```

---

## Architecture rules

These are not style preferences. Breaking them causes leaks or corrupt state.

**One access policy.** `apps/grievances/access.py` is the single source of truth
for who can see a complaint. `can_view()` and `visible_queryset()` are paired
implementations — one Python, one SQL — and
`test_object_and_queryset_rules_never_disagree` exists because drift between
them means a list endpoint returning rows the detail endpoint refuses. **Change
one, change the other, run that test.**

**Never `Complaint.objects.all()` in a view.** Filtering happens in
`get_queryset()` via the policy. Object-level permissions do not run for list
endpoints.

**Branch on access level, not role.** `ComplaintAccessPolicy.access_level()`
returns `NONE` / `RESTRICTED` / `FULL`. A respondent who is also HR gets
`RESTRICTED`. Being the subject of a complaint caps your access; it never adds
to it.

**Separate serializers, never popped fields.** The respondent gets
`ComplaintRestrictedSerializer`, which *omits* keys rather than nulling them. A
field that isn't there can't be reintroduced by an unrelated refactor.

**One canonical `state`.** `status` and `stage` are derived in the serializer.
Storing them separately produces impossible rows like "Closed / Investigation".

**Transitions live in the service layer.** Never PATCH `state`. Every transition
does the same four things in one transaction: guard → mutate → write
`ComplaintEvent` → notify. There is no path that changes a complaint without
recording it.

**Lock before checking state.** `select_for_update()` then
`transitions.check()`. Reading state before locking lets two callers both pass
the guard. The race is not reproducible single-threaded, so there are white-box
tests asserting the lock is *requested*.

**`ComplaintEvent` is append-only.** Never update or delete a row. This table is
evidence in an employment dispute.

**Services accept loose input.** They are called from the API, the admin,
management commands and imports. Coerce date strings; validate free text at the
service boundary too, not only in the serializer.

---

## Domain rules that surprise people

**HR does not see every complaint.** `visibility` is genuinely three-way:
`HR` = HR only, `LINE_MANAGER` = that manager only, `BOTH` = both. Any "all
complaints" metric under-reports by design and must not be labelled "all".

**The respondent sees nothing until an investigation opens**, and never learns
who filed it unless HR releases the identity.

**Line manager is resolved at query time**, never stored on the complaint —
access follows the employee's *current* manager. A respondent must never gain
line-manager visibility of a complaint against themselves.

**Conflict of interest.** An HR user named in a complaint cannot triage it,
cannot be appointed its investigator, and cannot decide it. This rule was added
by us, not specified by Product — worth re-confirming if it ever gets
questioned.

**Every case needs an investigator before it can close.** There is no
`SUBMITTED → RESOLVED` path. HR self-assigns for minor complaints.

---

## Commands

```bash
make up          # start the stack
make test        # pytest in the container
make migrate
make docs        # regenerate docs/api/schema.yml + index.html
make docs-check  # fails if the committed schema is stale
```

**Run `make docs` after any change to serializers, views or routes.** A stale
schema is worse than none, because the frontend team trusts it. Fix schema
warnings — they produce a subtly wrong generated client.

---

## Layout

```
config/                    settings (base/dev/prod), urls
apps/core/                 tenancy, timestamps, soft-delete bases
apps/directory/            STUB Employee/Department/Training — deleted at merge
apps/grievances/
  access.py                complaint visibility — the important one
  investigation_access.py  investigation rights, collaborator isolation
  transitions.py           the state machine table
  services.py              all writes; one transaction each
  enums.py                 fixed value sets (Product-confirmed)
```

This module is standalone today and merges into BizEdge/MAKAY later. Platform
models are referenced through `GRIEVANCES_*_MODEL` settings — **never import
`apps.directory` from `apps.grievances`**. Use `apps.core.platform`.

---

## Outstanding

- **LINE_MANAGER complaints cannot be progressed.** Who acts on them is an open
  product question. They can be filed and viewed only.
- **Attachments go to local disk** and are served by Django. The spec requires
  private storage with signed URLs. Fine for dev, wrong for production.
- **Notifications only log.** `notifications.py` is a seam; the platform
  notification service has no owner yet.
- **PIP follow-up reminders need Celery Beat** — the only part of this module
  not driven by a user action.
