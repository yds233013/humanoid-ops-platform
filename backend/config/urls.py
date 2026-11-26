from django.contrib.auth import views as auth_views
from django.urls import include, path

from accounts import api as accounts_api
from accounts import views as accounts_views
from fleet import api as fleet_api
from fleet import views as fleet_views
from operations import pause_views
from planning import zone_views
from platform_core import api as core_api
from platform_core import views as core_views

handler403 = "platform_core.views.permission_denied"
handler404 = "platform_core.views.page_not_found"
handler500 = "platform_core.views.server_error"

urlpatterns = [
    # v2 01A: operations (health check for the host) and first-run setup
    path("healthz", core_views.healthz),
    path("setup/", core_views.setup, name="setup"),
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
    path("", core_views.landing, name="landing"),
    path("login/", auth_views.LoginView.as_view()),
    path("logout/", auth_views.LogoutView.as_view(next_page="/login/")),
    path("account/password/", accounts_views.password_change, name="password-change"),
    path("fleet/", fleet_views.fleet_list, name="fleet-list"),
    path("", include("hub.urls")),  # v2 02A hub work queue (/hub/, /api/hub/...)
    path("", include("platform_core.setup_urls")),  # v2 01C screens; must precede fleet/<code>/
    # v2 03B-1: S5 zones and travel times (setup), S4 regional pause (before the planning/ include)
    path("settings/hubs/<str:hub_code>/zones/", zone_views.zone_list, name="settings-zones"),
    path("settings/hubs/<str:hub_code>/zones/new/", zone_views.zone_new, name="settings-zone-new"),
    path("settings/hubs/<str:hub_code>/zones/base/", zone_views.zone_base, name="settings-zone-base"),
    path(
        "settings/hubs/<str:hub_code>/zones/<str:zone_code>/retire/",
        zone_views.zone_retire,
        name="settings-zone-retire",
    ),
    path("settings/travel-times/", zone_views.travel_matrix, name="settings-travel"),
    path(
        "settings/travel-times/<int:from_id>/<int:to_id>/",
        zone_views.travel_pair,
        name="settings-travel-pair",
    ),
    path("planning/pause/", pause_views.pause_page, name="planning-pause"),
    path("planning/pause/lift/<int:pause_id>/", pause_views.lift_confirm, name="planning-pause-lift"),
    path("planning/pause/<str:hub_code>/", pause_views.pause_confirm, name="planning-pause-hub"),
    path(
        "planning/pause/<str:hub_code>/<str:zone_code>/",
        pause_views.pause_confirm,
        name="planning-pause-zone",
    ),
    # Slice 7 (Engineer C): hold-policy lifecycle and pattern pages; before fleet/<code>/ (it would match)
    path("fleet/hold-policies/", fleet_views.hold_policies, name="fleet-hold-policies"),
    path("fleet/hold-policies/create/", fleet_views.hold_policy_create_view),
    path("fleet/hold-policies/<int:version>/approve/", fleet_views.hold_policy_approve_view),
    path("fleet/hold-policies/<int:version>/withdraw/", fleet_views.hold_policy_withdraw_view),
    path("fleet/patterns/<str:code>/", fleet_views.pattern_detail, name="fleet-pattern"),
    path("fleet/patterns/<str:code>/templates/create/", fleet_views.template_create_view),
    path("fleet/patterns/<str:code>/templates/approve/", fleet_views.template_approve_view),
    path("fleet/patterns/<str:code>/templates/withdraw/", fleet_views.template_withdraw_view),
    path("fleet/patterns/<str:code>/periodic-rules/create/", fleet_views.periodic_rule_create_view),
    path("fleet/patterns/<str:code>/periodic-rules/approve/", fleet_views.periodic_rule_approve_view),
    path("fleet/patterns/<str:code>/periodic-rules/withdraw/", fleet_views.periodic_rule_withdraw_view),
    # v2 02C maintenance rules (configuration page) and the robot maintenance panel
    path("fleet/patterns/<str:code>/maintenance-rules/create/", fleet_views.maintenance_rule_create_view),
    path("fleet/patterns/<str:code>/maintenance-rules/approve/", fleet_views.maintenance_rule_approve_view),
    path("fleet/patterns/<str:code>/maintenance-rules/withdraw/", fleet_views.maintenance_rule_withdraw_view),
    path("fleet/<str:code>/maintenance/", fleet_views.robot_maintenance, name="fleet-maintenance"),
    path("fleet/<str:code>/", fleet_views.robot_detail, name="fleet-detail"),
    path("fleet/<str:code>/hold/", fleet_views.place_hold_view),
    path("fleet/<str:code>/approve/", fleet_views.approve_view),
    path("fleet/<str:code>/release/", fleet_views.release_view),
    path("fleet/<str:code>/check/", fleet_views.check_view),
    path("fleet/<str:code>/rebase/", fleet_views.rebase_request_view),
    path("fleet/<str:code>/rebase/approve/", fleet_views.rebase_approve_view),
    # Slice 2: planner
    path("readiness/", include("readiness.urls")),
    path("execution/", include("execution.urls")),
    path("ops/capacity/", include("capacity.urls")),
    path("ops/", include("operations.urls")),
    path("planning/", include("planning.urls")),
    path("api/ops/", include("operations.api")),
]
