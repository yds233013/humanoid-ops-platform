"""URLs of the 01C setup and resource screens. Included from config/urls.py BEFORE ``fleet/<code>/``."""

from django.urls import path

from fleet import setup_views as fleet_setup
from platform_core import resource_views as resources
from platform_core import setup_views as setup

urlpatterns = [
    # 01C.1 workspace setup (admin)
    path("settings/setup/", setup.setup_checklist, name="settings-setup"),
    path("settings/company/", setup.company_settings, name="settings-company"),
    path("settings/hubs/", setup.hub_list, name="settings-hubs"),
    path("settings/hubs/new/", setup.hub_new, name="settings-hub-new"),
    path("settings/operating/", setup.operating_settings, name="settings-operating"),
    path("settings/users/", setup.user_list, name="settings-users"),
    path("settings/users/new/", setup.user_new, name="settings-user-new"),
    path("settings/users/<int:user_id>/", setup.user_detail, name="settings-user"),
    path("settings/users/<int:user_id>/password/", setup.user_password, name="settings-user-password"),
    path(
        "settings/users/<int:user_id>/revoke/<int:grant_id>/", setup.user_revoke, name="settings-user-revoke"
    ),
    # 01C.2 fleet
    path("fleet/new/", fleet_setup.robot_new, name="fleet-new"),
    path("fleet/patterns/", fleet_setup.pattern_list, name="fleet-patterns"),
    path("fleet/patterns/new/", fleet_setup.pattern_new, name="fleet-pattern-new"),
    path("fleet/patterns/<str:code>/profile/", fleet_setup.profile_new, name="fleet-profile-new"),
    path("fleet/patterns/<str:code>/decision/", fleet_setup.pattern_decision, name="fleet-pattern-decision"),
    path(
        "fleet/patterns/<str:code>/profile/approve/<int:profile_id>/",
        fleet_setup.profile_approve,
        name="fleet-profile-approve",
    ),
    path("fleet/<str:code>/authorize/", fleet_setup.robot_authorize, name="fleet-authorize"),
    path("fleet/<str:code>/authorize/revoke/", fleet_setup.robot_deauthorize, name="fleet-deauthorize"),
    path("fleet/<str:code>/manifest/", fleet_setup.robot_manifest, name="fleet-manifest"),
    path("fleet/<str:code>/lifecycle/", fleet_setup.robot_lifecycle, name="fleet-lifecycle"),
    # 01C.2 people, vehicles, crews
    path("people/", resources.people_list, name="people"),
    path("people/new/", resources.person_new, name="person-new"),
    path("people/<str:code>/", resources.person_detail, name="person"),
    path(
        "people/<str:code>/qualifications/<int:qid>/revoke/",
        resources.qualification_revoke,
        name="qual-revoke",
    ),
    path("vehicles/", resources.vehicle_list, name="vehicles"),
    path("vehicles/new/", resources.vehicle_new, name="vehicle-new"),
    path("crews/", resources.crew_list, name="crews"),
]
