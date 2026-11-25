"""v2 Phase 03A closure (EM review): robot, vehicle and charger creation is guarded at the database boundary
(hub/0015: fleet_create_robot, planning_create_vehicle, hub_create_charger).

Proven here, as ops_app:

* A direct INSERT into fleet_robot, planning_vehicle or hub_charger is refused.
* Each function refuses a foreign-hub technician (a real hub technician of the other hub), a forged actor (a
  real user whose role cannot create it), NULL, 0, a missing id and a role-less user, and writes nothing: no row,
  no planning resource, no audit row, and no default hub created on an empty workspace.
* A revoked or expired grant of the right role is refused.
* A regional actor of the right role creates at either hub; the function writes the same audit row the command
  wrote before, with the request id, and the van gets its planning resource.
* The existing creation pages (/fleet/new/, /vehicles/new/, /hub/charging/chargers/) and the API
  (POST /api/fleet/register-robot) still work for a permitted user, with Django's own connection as ops_app.

The roles that may create (ops lead, engineering lead, admin) are regional by the role_grant_hub_scope check
constraint, so a hub-qualified grant of them cannot exist: the "correct hub" case is the regional actor at each
hub. The functions still check the role AT the target hub (hub_actor_has_at, grants FOR SHARE), so a future
hub-scoped creator would be limited to its hub.

SYNTHETIC data only.
"""

from __future__ import annotations

import os
import re
from typing import Any

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import RoleGrant
from fleet.models import Robot
from hub.models import Charger
from planning.models import Resource, Vehicle
from platform_core.models import AuditEvent, Hub
from tests.conftest import make_user
from tests.test_readiness_authz_boundary import as_runtime_role

TOKEN_RE = re.compile(r'name="_form_token" value="([^"]+)"')

ROBOT = "SELECT fleet_create_robot({a}, 'R-N1', 'SN-N1', {hub}, 'rid-c')"
VAN = "SELECT planning_create_vehicle({a}, 'V-N1', 1, 'kit', {hub}, 'rid-c')"
CHARGER = "SELECT hub_create_charger({a}, 'C-N1', 'Bay N1', '', {hub}, 'rid-c')"
CALLS = {"robot": ROBOT, "vehicle": VAN, "charger": CHARGER}


def _app_conn() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get("DB_APP_USER", "ops_app"),
        password=os.environ.get("DB_APP_PASSWORD", "ops_app_dev"),
        autocommit=True,
    )


def _written() -> tuple[int, ...]:
    return (
        Robot.objects.count(),
        Vehicle.objects.count(),
        Charger.objects.count(),
        Resource.objects.count(),
        AuditEvent.objects.count(),
        Hub.objects.count(),
    )


def _two_hubs() -> tuple[Hub, Hub]:
    return Hub.objects.create(code="HA", name="Hub A"), Hub.objects.create(code="HB", name="Hub B")


# ------------------------------------------------------------------ no direct INSERT
@pytest.mark.django_db(transaction=True)
def test_a_direct_insert_of_a_robot_vehicle_or_charger_by_ops_app_is_refused():
    a, _b = _two_hubs()
    ops = make_user("di_ops", "ops_lead")
    inserts = [
        "INSERT INTO fleet_robot (code, serial, lifecycle, version, registered_at, registered_by_id, hub_id) "
        f"VALUES ('R-X', 'SN-X', 'commissioning', 1, now(), {ops.pk}, {a.pk})",
        f"INSERT INTO planning_vehicle (code, robot_capacity, restraint_kit, notes, hub_id) VALUES ('V-X', 1, '', '', {a.pk})",
        "INSERT INTO hub_charger (code, name, in_service, note, created_at, created_by_id, hub_id) "
        f"VALUES ('C-X', 'x', true, '', now(), {ops.pk}, {a.pk})",
    ]
    before = _written()
    with _app_conn() as conn:
        for sql in inserts:
            with conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(sql)
        with conn.cursor() as cur:  # reading and the other updates stay allowed
            cur.execute("SELECT count(*) FROM fleet_robot")
    assert _written() == before


# ------------------------------------------------------------------ bad actors write nothing
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("what", sorted(CALLS))
@pytest.mark.parametrize("actor", ["foreign_hub_tech", "forged", "NULL", "0", "missing", "role_less"])
def test_the_creation_functions_refuse_bad_actors_as_ops_app_and_write_nothing(what, actor):
    a, b = _two_hubs()
    tech_a = make_user("ba_tech_a")
    RoleGrant.objects.create(user=tech_a, role="hub_technician", hub=a)
    value = {
        "foreign_hub_tech": str(tech_a.pk),  # a real technician of HA, creating at HB
        "forged": str(make_user("ba_eng", "engineer", "safety_reviewer").pk),  # real roles that cannot create
        "NULL": "NULL::bigint",
        "0": "0::bigint",
        "missing": "987654321",
        "role_less": str(make_user("ba_none").pk),
    }[actor]
    before = _written()
    with _app_conn() as conn, conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
        cur.execute(CALLS[what].format(a=value, hub=b.pk))
    assert _written() == before


@pytest.mark.django_db(transaction=True)
def test_a_refused_actor_does_not_create_the_default_hub_on_an_empty_workspace():
    """The actor is checked before the hub is resolved: no HUB-1 appears for a caller who may not create."""
    nobody = make_user("eh_none")
    assert not Hub.objects.exists()
    with _app_conn() as conn:
        for sql in CALLS.values():
            with conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(sql.format(a=nobody.pk, hub="NULL"))
    assert not Hub.objects.exists() and _written() == (0, 0, 0, 0, 0, 0)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("state", ["revoked", "expired"])
def test_a_revoked_or_expired_grant_of_the_right_role_is_refused(state):
    a, _b = _two_hubs()
    admin = make_user("rv_admin", "admin")
    if state == "expired":
        lead = make_user("rv_lead", "ops_lead", expired=True)
    else:
        lead = make_user("rv_lead", "ops_lead")
        RoleGrant.objects.filter(user=lead).update(revoked_at=timezone.now(), revoked_by=admin)
    before = _written()
    with _app_conn() as conn:
        for sql in CALLS.values():
            with conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(sql.format(a=lead.pk, hub=a.pk))
    assert _written() == before


# ------------------------------------------------------------------ allowed creators
@pytest.mark.django_db(transaction=True)
def test_a_regional_actor_of_the_right_role_creates_at_either_hub_with_the_audit_row():
    a, b = _two_hubs()
    ops, lead, admin = (
        make_user("ok_ops", "ops_lead"),
        make_user("ok_lead", "engineering_lead"),
        make_user("ok_admin", "admin"),
    )
    with _app_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT fleet_create_robot(%s, 'R-A', 'SN-A', %s, 'rid-1')", [ops.pk, a.pk])
        cur.execute("SELECT fleet_create_robot(%s, 'R-B', 'SN-B', %s, 'rid-2')", [lead.pk, b.pk])
        cur.execute("SELECT planning_create_vehicle(%s, 'V-B', NULL, '', %s, 'rid-3')", [ops.pk, b.pk])
        cur.execute("SELECT hub_create_charger(%s, ' c-a ', 'Bay A', '', %s, 'rid-4')", [ops.pk, a.pk])
        cur.execute("SELECT hub_create_charger(%s, 'C-B', 'Bay B', 'n', %s, 'rid-5')", [admin.pk, b.pk])
        # the existing rules, unchanged: duplicates, a capacity below 1, a bad charger code or a missing name
        for sql, err in (
            (
                f"SELECT fleet_create_robot({ops.pk}, 'R-A', 'SN-NEW', {a.pk}, '')",
                psycopg.errors.UniqueViolation,
            ),
            (
                f"SELECT fleet_create_robot({ops.pk}, 'R-NEW', 'SN-A', {a.pk}, '')",
                psycopg.errors.UniqueViolation,
            ),
            (
                f"SELECT planning_create_vehicle({ops.pk}, 'V-B', 1, '', {a.pk}, '')",
                psycopg.errors.UniqueViolation,
            ),
            (
                f"SELECT planning_create_vehicle({ops.pk}, 'V-0', 0, '', {a.pk}, '')",
                psycopg.errors.CheckViolation,
            ),
            (
                f"SELECT hub_create_charger({ops.pk}, 'C A', 'x', '', {a.pk}, '')",
                psycopg.errors.CheckViolation,
            ),
            (
                f"SELECT hub_create_charger({ops.pk}, 'C-Z', ' ', '', {a.pk}, '')",
                psycopg.errors.CheckViolation,
            ),
            (
                f"SELECT hub_create_charger({ops.pk}, 'C-B', 'x', '', {a.pk}, '')",
                psycopg.errors.UniqueViolation,
            ),
        ):
            with pytest.raises(err):
                cur.execute(sql)
        # with several hubs the hub must be named (the database never guesses)
        with pytest.raises(psycopg.errors.CheckViolation, match="hub required"):
            cur.execute(f"SELECT fleet_create_robot({ops.pk}, 'R-N', 'SN-N', NULL, '')")
    assert {(r.code, r.hub_id, r.lifecycle, r.registered_by_id) for r in Robot.objects.all()} == {
        ("R-A", a.pk, "commissioning", ops.pk),
        ("R-B", b.pk, "commissioning", lead.pk),
    }
    van = Vehicle.objects.get(code="V-B")
    assert van.hub_id == b.pk and van.robot_capacity is None
    assert Resource.objects.filter(kind="vehicle", vehicle=van).count() == 1
    assert {(c.code, c.hub_id, c.in_service, c.created_by_id) for c in Charger.objects.all()} == {
        ("C-A", a.pk, True, ops.pk),
        ("C-B", b.pk, True, admin.pk),
    }
    audit = {
        (e.command, e.entity_id, e.actor_id, e.request_id, e.payload.get("hub"))
        for e in AuditEvent.objects.filter(outcome="committed")
    }
    assert audit == {
        ("fleet.register_robot", "R-A", ops.pk, "rid-1", "HA"),
        ("fleet.register_robot", "R-B", lead.pk, "rid-2", "HB"),
        ("planning.register_vehicle", "V-B", ops.pk, "rid-3", "HB"),
        ("hub.create_charger", "C-A", ops.pk, "rid-4", "HA"),
        ("hub.create_charger", "C-B", admin.pk, "rid-5", "HB"),
    }


# ------------------------------------------------------------------ the existing pages and API, as ops_app
def _page_post(c: Client, url: str, data: dict[str, Any]) -> Any:
    token = TOKEN_RE.search(c.get(url).content.decode())
    assert token, url
    return c.post(url, {**data, "_form_token": token.group(1)})


@pytest.mark.django_db(transaction=True)
def test_the_creation_pages_and_the_api_still_work_as_the_runtime_role():
    _a, _b = _two_hubs()
    ops, admin = make_user("pg_ops", "ops_lead"), make_user("pg_admin", "admin")
    field = make_user("pg_field")
    RoleGrant.objects.create(user=field, role="field_operator", hub=Hub.objects.get(code="HB"))
    ops_page, admin_page, field_page = Client(), Client(), Client()
    ops_page.force_login(ops)
    admin_page.force_login(admin)
    field_page.force_login(field)
    api = APIClient()
    api.force_authenticate(user=ops)
    with as_runtime_role():
        r = _page_post(ops_page, "/fleet/new/", {"code": "R-P1", "serial": "SN-P1", "hub": "HB"})
        assert r.status_code == 302, r.content.decode()[:500]
        r = _page_post(ops_page, "/vehicles/new/", {"code": "V-P1", "robot_capacity": "1", "hub": "HA"})
        assert r.status_code == 302, r.content.decode()[:500]
        r = _page_post(admin_page, "/hub/charging/chargers/", {"code": "C-P1", "name": "Bay P1", "hub": "HB"})
        assert r.status_code == 302, r.content.decode()[:500]
        r = api.post(
            "/api/fleet/register-robot", {"code": "R-P2", "serial": "SN-P2", "hub_code": "HA"}, format="json"
        )
        assert r.status_code in (200, 201), r.content
        # a person who may not create is refused by the page, and nothing is written
        before = _written()
        assert field_page.get("/fleet/new/").status_code == 403
        assert _written() == before
    assert Robot.objects.get(code="R-P1").hub.code == "HB" and Robot.objects.get(code="R-P2").hub.code == "HA"
    assert (
        Vehicle.objects.get(code="V-P1").hub.code == "HA"
        and Charger.objects.get(code="C-P1").hub.code == "HB"
    )
    assert Resource.objects.filter(kind="vehicle", vehicle__code="V-P1").exists()
    # one committed audit row per creation (written by the function; run_command adds none)
    for command, n in (
        ("fleet.register_robot", 2),
        ("planning.register_vehicle", 1),
        ("hub.create_charger", 1),
    ):
        assert AuditEvent.objects.filter(command=command, outcome="committed").count() == n, command
    created = AuditEvent.objects.filter(
        outcome="committed",
        command__in=["fleet.register_robot", "planning.register_vehicle", "hub.create_charger"],
    )
    assert all(e.request_id for e in created)  # correlated with the HTTP request, as before


@pytest.mark.django_db(transaction=True)
def test_the_services_map_the_function_refusals_to_the_usual_errors():
    from fleet import services as fs
    from hub import charging as hc
    from planning import services as ps
    from platform_core.commands import Conflict, Invalid, PermissionDenied

    _two_hubs()
    ops = make_user("sv_ops", "ops_lead")
    eng = make_user("sv_eng", "engineer")
    with as_runtime_role():
        fs.register_robot(ops, code="R-S1", serial="SN-S1", hub_code="HA")
        with pytest.raises(Conflict, match="already exists"):
            fs.register_robot(ops, code="R-S1", serial="SN-S9", hub_code="HA")
        with pytest.raises(PermissionDenied):
            fs.register_robot(eng, code="R-S2", serial="SN-S2", hub_code="HA")
        with pytest.raises(Invalid, match="at least 1"):
            ps.register_vehicle(ops, code="V-S0", robot_capacity=0, hub_code="HA")
        with pytest.raises(Invalid, match="letters, digits and dashes"):
            hc.create_charger(ops, code="C S", name="x", hub_code="HA")
    assert AuditEvent.objects.filter(outcome="denied", command="fleet.register_robot", actor=eng).count() == 1
    assert Robot.objects.count() == 1
