# Deploying to Render (staging)

A staging environment so the frontend team have a real URL to build against.
**Not production** — see the limits at the end before anyone points real users
at it.

---

## What gets deployed

Two things: a web service and a Postgres database.

No Celery worker, no beat scheduler, no Redis. Free web services sleep when
idle, and a scheduler that sleeps does not schedule — so in staging you run the
one scheduled job by hand:

```bash
python manage.py send_due_pip_reminders
```

When it needs to be automatic, add a Render **Cron Job** service (about
$1/month) running that same command. The logic lives in a management command
precisely so the scheduler is a deployment decision rather than a code change.

---

## Before you start

You need a Render account with the free Postgres slot available — **one free
database per account**. If you already have one, either delete it (take a
`pg_dump` first; free databases have no backups) or upgrade it.

The repo must be pushed to GitHub or GitLab. Render deploys from a connected
repository, not from your laptop.

---

## Steps

**1. Push the repo** to GitHub or GitLab if it isn't there already.

**2. In Render: New → Blueprint**, connect the repository, and point it at
`render.yaml` in the root. Render reads that file and creates both services —
you shouldn't need to configure anything by hand.

**3. Wait for the first deploy.** The Docker build takes a few minutes.
Migrations run automatically as part of the start command.

**4. Seed the demo data.** From the web service's **Shell** tab:

```bash
python manage.py seed_demo
```

It prints the demo logins. Every person and complaint is invented — see the
note on data below.

**5. Create yourself a superuser**, if you want the Django admin:

```bash
python manage.py createsuperuser
```

Then link it to an Employee record in `/admin/` → Directory → Employees, or the
API will return *"No employee profile is linked to this account"*. Access is
decided per employee, not per user.

**6. Check it works.** Your URL will be something like
`https://bizedge-grievances-api.onrender.com`:

- `/api/docs/` — browsable API reference
- `/api/schema/` — the OpenAPI spec, also the health check path
- `/admin/` — Django admin

---

## What to send the frontend team

The base URL, and these two files from `docs/api/`:

- `index.html` — opens in any browser, no setup
- `API-NOTES.md` — the behaviour the schema can't express

Tell them to put the base URL in an environment variable rather than hardcoding
it, so moving to production later is a config change on their side.

The demo logins from `seed_demo` let them sign in as HR, as a line manager, and
as an ordinary employee — which matters, because **the same endpoint returns
different data depending on who is asking**. Testing only as HR will hide most
of the access rules.

---

## Data

**Staging holds invented data only.** Never seed it with real employee names or
real allegations. This is a grievance system; a staging database is not the
place for a real complaint about a real person, and free Render databases have
no backups and no encryption guarantees you've reviewed.

`seed_demo` refuses to run when `DEBUG=False` and the database already contains
complaints outside the demo organisation, so it can't quietly overwrite an
environment someone is using.

---

## Limits worth knowing

**The free database expires 30 days after creation**, with a 14-day grace
period before the data is deleted. Free databases have no backups. Diarise it —
the frontend team losing their environment mid-sprint is avoidable.

**The free web service sleeps after inactivity**, so the first request after a
quiet period takes 30–60 seconds. Warn the frontend team, or they'll report it
as a timeout bug.

**Storage is ephemeral.** Uploaded attachments live on the container filesystem
and are destroyed on every deploy. Fine for staging; see below.

---

## Before this could be production

Three things in the code, all flagged in `CLAUDE.md`:

**Attachments go to local disk** and are served by Django. The spec calls for
private storage with signed URLs. On Render the files also vanish on redeploy.
This is the one I'd genuinely block on — it's evidence in an employment dispute.

**Notifications only write to a log.** Nobody is told a complaint was filed,
that they've been appointed to investigate, or that a question is waiting. The
workflow runs; the people never find out.

**`LINE_MANAGER` complaints cannot be progressed.** They can be filed and viewed
but not triaged, because who acts on them is still an open product question.

Plus the infrastructure question: a production deployment holding real HR case
data needs an owner, a backup policy, and someone signing off on where that data
physically lives.
