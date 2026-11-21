"""Readiness UI routes. Mounted by the integration lead at /readiness/ (config/urls.py)."""

from django.urls import path

from readiness import views

app_name = "readiness"

urlpatterns = [
    path("", views.index, name="index"),
    path("visits/<str:code>/", views.visit_detail, name="visit"),
    path("visits/<str:code>/evidence/", views.record_evidence_view, name="record-evidence"),
    path("visits/<str:code>/void/", views.void_evidence_view, name="void-evidence"),
    path("visits/<str:code>/recovery-plan/", views.recovery_plan_view, name="recovery-plan"),
    path("visits/<str:code>/inspection/", views.visit_inspection, name="visit-inspection"),
    path("robots/<str:code>/inspection/", views.robot_inspection, name="robot-inspection"),
    path("visits/<str:code>/authorize/", views.authorize_view, name="authorize"),
    path("authorizations/<str:code>/depart/", views.depart_view, name="depart"),
    path("authorizations/<str:code>/revoke/", views.revoke_view, name="revoke"),
    path("policies/", views.policies, name="policies"),
    path("policies/create/", views.policy_create_view, name="policy-create"),
    path("policies/<int:version>/approve/", views.policy_approve_view, name="policy-approve"),
    path("policies/<int:version>/withdraw/", views.policy_withdraw_view, name="policy-withdraw"),
]
