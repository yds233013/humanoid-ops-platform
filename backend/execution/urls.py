"""Field execution and incident UI routes (Slice 4, Engineer D). Mounted at /execution/ (config/urls.py)."""

from django.urls import path

from execution import views

app_name = "execution"

urlpatterns = [
    path("", views.index, name="index"),
    path("mine/", views.my_visits, name="mine"),
    # v2 03D-3: the offline page shell the service worker caches (no run or account data)
    path("field-shell/", views.field_shell, name="field-shell"),
    path("runs/<str:code>/", views.run_detail, name="run"),
    path("runs/<str:code>/verify/", views.verify_view, name="verify"),
    path("runs/<str:code>/trip-event/", views.trip_event_view, name="trip-event"),
    path("runs/<str:code>/custody/", views.transfer_view, name="transfer"),
    path("runs/<str:code>/site-check/", views.site_check_view, name="site-check"),
    path("runs/<str:code>/session/", views.session_view, name="session"),
    path("runs/<str:code>/interruptions/open/", views.interruption_open_view, name="interruption-open"),
    path(
        "runs/<str:code>/interruptions/resolve/", views.interruption_resolve_view, name="interruption-resolve"
    ),
    path("runs/<str:code>/disposition/", views.disposition_view, name="disposition"),
    path("runs/<str:code>/reconcile/", views.reconcile_view, name="reconcile"),
    path("runs/<str:code>/close/", views.close_view, name="close"),
    path("incidents/", views.incidents, name="incidents"),
    path("incidents/report/", views.report_view, name="report"),
    path("incidents/<str:code>/", views.incident_detail, name="incident"),
    path("incidents/<str:code>/fault/", views.fault_view, name="fault"),
    path("incidents/<str:code>/severity/", views.severity_view, name="severity"),
    path("incidents/<str:code>/status/", views.status_view, name="status"),
    path("incidents/<str:code>/escalate/", views.escalate_view, name="escalate"),
    path("incidents/<str:code>/ack/", views.ack_view, name="ack"),
    path("incidents/<str:code>/recover/", views.recover_view, name="recover"),
    path("policies/", views.policies, name="policies"),
    path("policies/create/", views.policy_create_view, name="policy-create"),
    path("policies/<int:version>/approve/", views.policy_approve_view, name="policy-approve"),
    path("policies/<int:version>/withdraw/", views.policy_withdraw_view, name="policy-withdraw"),
    path("visits/<str:visit_code>/dossier/", views.dossier, name="dossier"),
    # Slice 7 (X-4, Engineer C): escalation deadline sets
    path("escalation-deadlines/", views.escalation_deadlines, name="escalation-deadlines"),
    path("escalation-deadlines/create/", views.deadline_set_create_view, name="deadline-create"),
    path(
        "escalation-deadlines/<int:set_id>/approve/", views.deadline_set_approve_view, name="deadline-approve"
    ),
    path(
        "escalation-deadlines/<int:set_id>/withdraw/",
        views.deadline_set_withdraw_view,
        name="deadline-withdraw",
    ),
]
