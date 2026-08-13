"""Investigation endpoints.

Two audiences with very different rights, kept in separate viewsets rather
than one with conditional serializers:

* :class:`InvestigationViewSet` -- the lead running the case, plus HR reading it.
* :class:`MyInformationRequestViewSet` -- a collaborator answering one question.

They are separate on purpose. Merging them would mean one serializer deciding,
per request, whether to include the complaint description -- and that is
precisely the kind of conditional that leaks after a refactor.
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.grievances.access import employee_for
from apps.grievances.investigation_access import InvestigationAccessPolicy
from apps.grievances.models import (
    InformationRequest,
    Investigation,
    InvestigationCollaborator,
)
from apps.grievances.permissions import IsEmployee
from apps.grievances.serializers import (
    InformationRequestSerializer,
    InvestigationDetailSerializer,
    InvestigationNoteSerializer,
    InviteCollaboratorSerializer,
    MeetingSerializer,
    MyInformationRequestSerializer,
    RecordMeetingSerializer,
    RequestInformationSerializer,
    RespondToRequestSerializer,
)
from apps.grievances.services import (
    ServiceError,
    invite_collaborator,
    record_meeting,
    remove_collaborator,
    request_information,
    respond_to_request,
    submit_report,
)
from apps.grievances.transitions import TransitionError


class InvestigationViewSet(viewsets.GenericViewSet):
    """The lead's workspace.

    Read requires FULL access to the complaint; every write requires being the
    lead. HR can watch but not drive -- taking over means reassigning the case,
    which is auditable, rather than acting inside someone else's investigation.
    """

    permission_classes = [IsEmployee]
    serializer_class = InvestigationDetailSerializer

    def get_queryset(self):
        employee = employee_for(self.request.user)
        if employee is None:
            return Investigation.objects.none()
        return Investigation.objects.select_related(
            "complaint", "lead"
        ).filter(complaint__organisation_id=employee.organisation_id)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        employee = employee_for(self.request.user)
        context["employee"] = employee
        context["organisation_id"] = getattr(employee, "organisation_id", None)
        return context

    # -- helpers -----------------------------------------------------------

    def _get_viewable(self) -> Investigation:
        investigation = self.get_object()
        if not InvestigationAccessPolicy.can_view(
            investigation, employee_for(self.request.user)
        ):
            raise PermissionDenied(_("You do not have access to this investigation."))
        return investigation

    def _get_manageable(self) -> Investigation:
        investigation = self.get_object()
        if not InvestigationAccessPolicy.can_manage(
            investigation, employee_for(self.request.user)
        ):
            raise PermissionDenied(
                _("Only the investigation lead can do this.")
            )
        return investigation

    def _collaborator(self, investigation, collaborator_id):
        collaborator = InvestigationCollaborator.objects.filter(
            pk=collaborator_id, investigation=investigation
        ).first()
        if collaborator is None:
            raise NotFound(_("That person is not on this investigation."))
        return collaborator

    @staticmethod
    def _handle(fn, /, **kwargs):
        """Translate service failures into the right status code.

        A guard rejecting the request is a 400; a state conflict is a 409.
        Neither should surface as a 500.
        """
        try:
            return fn(**kwargs)
        except TransitionError as exc:
            raise _Conflict(str(exc)) from exc
        except ServiceError as exc:
            raise ValidationError({"detail": str(exc)}) from exc

    # -- read --------------------------------------------------------------

    @extend_schema(responses={200: InvestigationDetailSerializer})
    def retrieve(self, request, pk=None):
        investigation = self._get_viewable()
        serializer = InvestigationDetailSerializer(
            investigation, context=self.get_serializer_context()
        )
        return Response(serializer.data)

    # -- collaborators -----------------------------------------------------

    @extend_schema(
        request=InviteCollaboratorSerializer,
        responses={201: dict},
        description="Add someone to the investigation. Repeating an invite is safe.",
    )
    @action(detail=True, methods=["post"], url_path="collaborators")
    def add_collaborator(self, request, pk=None):
        investigation = self._get_manageable()
        serializer = InviteCollaboratorSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)

        collaborator = self._handle(
            invite_collaborator,
            investigation=investigation,
            actor=employee_for(request.user),
            employee=serializer.validated_data["employee"],
            role=serializer.validated_data["role"],
            request=request,
        )
        return Response(
            {
                "id": str(collaborator.pk),
                "employee": str(collaborator.employee_id),
                "role": collaborator.role,
                "status": collaborator.status,
            },
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "collaborator_id",
                str,
                OpenApiParameter.PATH,
                description="Collaborator id on this investigation.",
            )
        ],
        responses={204: None},
        description=(
            "Remove someone from the investigation. Soft: their answers stay on "
            "the record and any outstanding question is expired."
        ),
    )
    @action(
        detail=True,
        methods=["delete"],
        url_path=r"collaborators/(?P<collaborator_id>[^/.]+)",
    )
    def remove_collaborator(self, request, pk=None, collaborator_id=None):
        investigation = self._get_manageable()
        collaborator = self._collaborator(investigation, collaborator_id)

        self._handle(
            remove_collaborator,
            collaborator=collaborator,
            actor=employee_for(request.user),
            request=request,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    # -- information requests ---------------------------------------------

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "collaborator_id",
                str,
                OpenApiParameter.PATH,
                description="Collaborator id on this investigation.",
            )
        ],
        request=RequestInformationSerializer,
        responses={201: InformationRequestSerializer},
        description=(
            "Ask a collaborator a question. Asking again appends to the thread "
            "rather than replacing the previous question."
        ),
    )
    @action(
        detail=True,
        methods=["post"],
        url_path=r"collaborators/(?P<collaborator_id>[^/.]+)/request-information",
    )
    def request_information(self, request, pk=None, collaborator_id=None):
        investigation = self._get_manageable()
        collaborator = self._collaborator(investigation, collaborator_id)

        serializer = RequestInformationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        info_request = self._handle(
            request_information,
            collaborator=collaborator,
            actor=employee_for(request.user),
            prompt=serializer.validated_data["prompt"],
            due_at=serializer.validated_data.get("due_at"),
            request=request,
        )
        return Response(
            InformationRequestSerializer(info_request).data,
            status=status.HTTP_201_CREATED,
        )

    # -- meetings and notes ------------------------------------------------

    @extend_schema(
        request=RecordMeetingSerializer,
        responses={201: MeetingSerializer},
        description=(
            "Record a meeting. Attendees not already on the case are added as "
            "collaborators."
        ),
    )
    @action(detail=True, methods=["post"], url_path="meetings")
    def add_meeting(self, request, pk=None):
        investigation = self._get_manageable()
        serializer = RecordMeetingSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)

        meeting = self._handle(
            record_meeting,
            investigation=investigation,
            actor=employee_for(request.user),
            meeting_date=serializer.validated_data["meeting_date"],
            findings=serializer.validated_data["findings"],
            attendee_employees=list(serializer.validated_data.get("attendees", [])),
            request=request,
        )
        return Response(
            MeetingSerializer(meeting).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(
        request=InvestigationNoteSerializer,
        responses={201: InvestigationNoteSerializer},
    )
    @action(detail=True, methods=["post"], url_path="notes")
    def add_note(self, request, pk=None):
        investigation = self._get_manageable()
        serializer = InvestigationNoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        note = investigation.notes.create(
            body=serializer.validated_data["body"],
            author=employee_for(request.user),
        )
        return Response(
            InvestigationNoteSerializer(note).data, status=status.HTTP_201_CREATED
        )

    # -- submission --------------------------------------------------------

    @extend_schema(
        request=None,
        responses={200: InvestigationDetailSerializer},
        description=(
            "Submit the report and hand the case back to HR for a decision. "
            "Returns 409 if it has already been submitted."
        ),
    )
    @action(detail=True, methods=["post"], url_path="submit-report")
    def submit_report(self, request, pk=None):
        investigation = self._get_manageable()

        self._handle(
            submit_report,
            investigation=investigation,
            actor=employee_for(request.user),
            request=request,
        )
        investigation.refresh_from_db()
        return Response(
            InvestigationDetailSerializer(
                investigation, context=self.get_serializer_context()
            ).data
        )


class MyInformationRequestViewSet(viewsets.GenericViewSet):
    """A collaborator's inbox.

    The narrowest surface in the module. Someone asked to give evidence can
    list the questions put to them and answer one. They cannot reach the
    complaint, the investigation, other collaborators, or anyone else's answer
    through this viewset -- there is no route that would let them.
    """

    permission_classes = [IsEmployee]
    serializer_class = MyInformationRequestSerializer

    def get_queryset(self):
        return InvestigationAccessPolicy.pending_requests_for(
            employee_for(self.request.user)
        )

    @extend_schema(responses={200: MyInformationRequestSerializer(many=True)})
    def list(self, request):
        queryset = self.get_queryset()
        return Response(
            MyInformationRequestSerializer(queryset, many=True).data
        )

    @extend_schema(
        request=RespondToRequestSerializer,
        responses={201: dict},
        description="Answer a question that was put to you.",
    )
    @action(detail=True, methods=["post"], url_path="respond")
    def respond(self, request, pk=None):
        employee = employee_for(request.user)

        # Looked up across all requests, not just pending ones, so that
        # answering twice returns a clear message rather than a 404.
        info_request = InformationRequest.objects.filter(pk=pk).first()
        if info_request is None or not InvestigationAccessPolicy.can_respond(
            info_request, employee
        ):
            # Deliberately the same response either way: a 403 on someone
            # else's request would confirm that the id exists.
            raise NotFound(_("No such request."))

        serializer = RespondToRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            response = respond_to_request(
                info_request=info_request,
                actor=employee,
                body=serializer.validated_data["body"],
                request=request,
            )
        except ServiceError as exc:
            raise ValidationError({"detail": str(exc)}) from exc

        return Response(
            {"id": str(response.pk), "responded_at": response.responded_at},
            status=status.HTTP_201_CREATED,
        )


class _Conflict(ValidationError):
    """409: the request was fine, the resource had moved on."""

    status_code = status.HTTP_409_CONFLICT
