"""Populate a staging environment with realistic-looking demo data.

    python manage.py seed_demo [--reset]

Gives the frontend team lists with something in them -- complaints in every
state, an org chart with real reporting lines, a running investigation and a
closed case with a PIP -- instead of empty arrays that make it impossible to
tell "no data" from "broken endpoint".

**Every person and complaint here is invented.** This is a grievance system;
staging must never hold a real allegation about a real employee. The
descriptions are deliberately bland for the same reason.

Refuses to run when DEBUG is False *and* the database already holds
complaints, so it cannot quietly scribble over a real environment.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.core.models import Organisation
from apps.directory.models import Department, Employee, Training
from apps.grievances import enums
from apps.grievances.models import Complaint
from apps.grievances.services import (
    appoint_investigator,
    file_complaint,
    invite_collaborator,
    request_information,
    resolve_complaint,
    submit_report,
)

ORG_NAME = "Northwind Demo"
ORG_SLUG = "northwind-demo"
PASSWORD = "demo-password-1"  # noqa: S105 - staging only, printed below

#: name, job title, department, is_hr, reports_to
PEOPLE = [
    ("Priya Raman", "HR Business Partner", "People", True, None),
    ("Tobias Klein", "HR Coordinator", "People", True, None),
    ("Alice Bennett", "Engineering Manager", "Engineering", False, None),
    ("Bob Ncube", "Software Engineer", "Engineering", False, "Alice Bennett"),
    ("Carol Diaz", "Software Engineer", "Engineering", False, "Alice Bennett"),
    ("Daniel Osei", "QA Analyst", "Engineering", False, "Alice Bennett"),
    ("Emma Lindqvist", "Head of Operations", "Operations", False, None),
    ("Femi Adeyemi", "Operations Assistant", "Operations", False, "Emma Lindqvist"),
]


class Command(BaseCommand):
    help = "Create demo data for a staging environment. Invented data only."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete the demo organisation first, then rebuild it.",
        )

    def handle(self, *args, **options) -> None:
        existing = Complaint.all_objects.exclude(
            organisation__slug=ORG_SLUG
        ).count()
        if not settings.DEBUG and existing and not options["reset"]:
            raise CommandError(
                f"This database already holds {existing} complaint(s) outside the "
                f"demo organisation. Refusing to seed. Re-run with --reset only "
                f"if you are certain this is a throwaway environment."
            )

        with transaction.atomic():
            if options["reset"]:
                Organisation.objects.filter(slug=ORG_SLUG).delete()

            org, created = Organisation.objects.get_or_create(
                slug=ORG_SLUG, defaults={"name": ORG_NAME}
            )
            if not created and not options["reset"]:
                self.stdout.write("Demo organisation already exists. Nothing to do.")
                self.stdout.write("Use --reset to rebuild it.")
                return

            people = self._create_people(org)
            self._create_trainings(org)
            self._create_complaints(org, people)

        self.stdout.write(self.style.SUCCESS("\nDemo data created.\n"))
        self.stdout.write("Sign in with any of these (all share one password):\n")
        for name, title, _dept, is_hr, _mgr in PEOPLE:
            role = "HR" if is_hr else "employee"
            self.stdout.write(f"  {self._username(name):<18} {name} — {title} ({role})")
        self.stdout.write(f"\n  password: {PASSWORD}\n")
        self.stdout.write(
            "Priya Raman sees the HR console view; Alice Bennett is a line "
            "manager; Bob and Carol are ordinary employees."
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _username(full_name: str) -> str:
        return full_name.split()[0].lower()

    def _create_people(self, org) -> dict[str, Employee]:
        departments = {
            name: Department.objects.get_or_create(organisation=org, name=name)[0]
            for name in {p[2] for p in PEOPLE}
        }

        people: dict[str, Employee] = {}
        for name, title, dept, is_hr, _mgr in PEOPLE:
            username = self._username(name)
            user, _ = User.objects.get_or_create(
                username=username,
                defaults={"email": f"{username}@northwind.example"},
            )
            user.set_password(PASSWORD)
            user.save(update_fields=["password"])

            people[name] = Employee.objects.create(
                organisation=org,
                user=user,
                full_name=name,
                email=f"{username}@northwind.example",
                job_title=title,
                department=departments[dept],
                is_hr=is_hr,
            )

        # Reporting lines, once everyone exists.
        for name, _title, _dept, _is_hr, manager in PEOPLE:
            if manager:
                people[name].line_manager = people[manager]
                people[name].save(update_fields=["line_manager"])

        return people

    def _create_trainings(self, org) -> None:
        for name in ("Respect at Work", "Managing Difficult Conversations"):
            Training.objects.get_or_create(organisation=org, name=name)

    def _create_complaints(self, org, people: dict[str, Employee]) -> None:
        hr = people["Priya Raman"]
        today = timezone.localdate()

        # 1. Just filed, awaiting triage.
        file_complaint(
            organisation=org, filed_by=people["Bob Ncube"],
            source=enums.ComplaintSource.SELF,
            subject_type=enums.SubjectType.GENERAL,
            complaint_type=enums.ComplaintType.UNSUSTAINABLE_WORKLOAD,
            description="Sustained overtime across the last three sprints.",
            visibility=enums.Visibility.HR,
            complainant=people["Bob Ncube"],
        )

        # 2. Filed to the line manager only -- HR cannot see this one.
        file_complaint(
            organisation=org, filed_by=people["Daniel Osei"],
            source=enums.ComplaintSource.SELF,
            subject_type=enums.SubjectType.GENERAL,
            complaint_type=enums.ComplaintType.HOSTILE_WORK_ENVIRONMENT,
            description="Tone in stand-ups has become difficult.",
            visibility=enums.Visibility.LINE_MANAGER,
            complainant=people["Daniel Osei"],
        )

        # 3. Under investigation, with a question outstanding.
        under_investigation = file_complaint(
            organisation=org, filed_by=people["Carol Diaz"],
            source=enums.ComplaintSource.SELF,
            subject_type=enums.SubjectType.EMPLOYEE,
            complaint_type=enums.ComplaintType.THEFT,
            description="A team laptop went missing from the third floor.",
            visibility=enums.Visibility.HR,
            complainant=people["Carol Diaz"],
            respondent=people["Femi Adeyemi"],
            frequency=enums.Frequency.ONE_TIME,
            incident_date=today - timedelta(days=21),
        )
        investigation = appoint_investigator(
            complaint=under_investigation, actor=hr,
            lead=people["Tobias Klein"], due_date=today + timedelta(days=21),
        )
        witness = invite_collaborator(
            investigation=investigation, actor=people["Tobias Klein"],
            employee=people["Daniel Osei"], role=enums.CollaboratorRole.WITNESS,
        )
        request_information(
            collaborator=witness, actor=people["Tobias Klein"],
            prompt="Were you on the third floor that afternoon?",
        )

        # 4. Closed, with a PIP running.
        closed = file_complaint(
            organisation=org, filed_by=hr,
            source=enums.ComplaintSource.HR_FOR_EMPLOYEE,
            subject_type=enums.SubjectType.EMPLOYEE,
            complaint_type=enums.ComplaintType.HOSTILE_WORK_ENVIRONMENT,
            description="Repeated dismissive comments in team meetings.",
            visibility=enums.Visibility.BOTH,
            complainant=people["Femi Adeyemi"],
            respondent=people["Carol Diaz"],
            frequency=enums.Frequency.REPEAT_BEHAVIOR,
            occurrence_count=4,
            incident_date=today - timedelta(days=60),
        )
        closed_investigation = appoint_investigator(
            complaint=closed, actor=hr, lead=hr,
            due_date=today - timedelta(days=10),
        )
        submit_report(investigation=closed_investigation, actor=hr)
        closed.refresh_from_db()
        resolve_complaint(
            complaint=closed, actor=hr,
            decision=enums.InvestigationDecision.PARTIALLY_SUBSTANTIATED,
            resolution_type=enums.ResolutionType.INFORMAL,
            informal_resolution_type=enums.InformalResolutionType.COACHING,
            decision_notes="Some of the account was supported. Coaching agreed.",
            pip={
                "start_date": today - timedelta(days=7),
                "end_date": today + timedelta(days=53),
                "trainings": list(Training.objects.filter(organisation=org)[:1]),
                "follow_ups": [
                    {"scheduled_date": today + timedelta(days=7),
                     "kind": enums.FollowUpKind.TWO_WEEK_CHECKIN},
                    {"scheduled_date": today + timedelta(days=35)},
                ],
            },
        )

        # 5. Withdrawn before anything happened.
        withdrawn = file_complaint(
            organisation=org, filed_by=people["Femi Adeyemi"],
            source=enums.ComplaintSource.SELF,
            subject_type=enums.SubjectType.GENERAL,
            complaint_type=enums.ComplaintType.OTHERS,
            complaint_type_note="Rota fairness",
            description="Weekend rota allocation feels uneven.",
            visibility=enums.Visibility.BOTH,
            complainant=people["Femi Adeyemi"],
        )
        from apps.grievances.services import withdraw_complaint

        withdraw_complaint(
            complaint=withdrawn, actor=people["Femi Adeyemi"],
            reason="Resolved directly with the team.",
        )
