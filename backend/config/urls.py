from django.contrib.auth import views as auth_views
from django.urls import path
from django.views.generic import RedirectView

from accounts import api as accounts_api
from fleet import api as fleet_api
from fleet import views as fleet_views
from planning import views as planning_views
from platform_core import api as core_api

urlpatterns = [
    # JSON API
    path("api/health", core_api.health),
    path("api/auth/csrf", accounts_api.csrf),
    path("api/auth/login", accounts_api.login_view),
    path("api/auth/logout", accounts_api.logout_view),
    path("api/auth/me", accounts_api.me),
    path("api/commands/grant-role", accounts_api.grant_role_view),
    path("api/commands/revoke-role", accounts_api.revoke_role_view),
    path("api/audit-events", core_api.audit_events),
    path("api/fleet/robots", fleet_api.robots),
    path("api/fleet/robots/<str:code>", fleet_api.robot_detail),
    path("api/fleet/<str:name>", fleet_api.command),
    # Minimal fleet UI
    path("", RedirectView.as_view(url="/fleet/")),
    path("login/", auth_views.LoginView.as_view()),
    path("logout/", auth_views.LogoutView.as_view(next_page="/login/")),
    path("fleet/", fleet_views.fleet_list, name="fleet-list"),
    path("fleet/<str:code>/", fleet_views.robot_detail, name="fleet-detail"),
    path("fleet/<str:code>/hold/", fleet_views.place_hold_view),
    path("fleet/<str:code>/approve/", fleet_views.approve_view),
    path("fleet/<str:code>/release/", fleet_views.release_view),
    path("fleet/<str:code>/check/", fleet_views.check_view),
    # Slice 2: planner
    path("planning/", planning_views.week, name="planning-week"),
    path("planning/sites/", planning_views.sites),
    path("planning/visits/<str:code>/", planning_views.visit_detail, name="planning-visit"),
    path("planning/visits/<str:code>/plan/", planning_views.plan_view),
    path("planning/visits/<str:code>/confirm/", planning_views.confirm_view),
    path("planning/visits/<str:code>/cancel/", planning_views.cancel_view),
]
