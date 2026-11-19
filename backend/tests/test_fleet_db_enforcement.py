"""Fleet invariants hold against DIRECT SQL from the runtime role (ops_app), bypassing Python entirely."""

import psycopg
import pytest
from psycopg.types.json import Jsonb

from fleet import services as s
from fleet.models import Hold, HoldPolicy, Robot
from tests.fleet_world import build_world


@pytest.fixture
def w(transactional_db):
    return build_world(robots=2, prefix="db_")


def _refused(conn, sql, params=None, match=None):
    with conn.cursor() as cur:
        with pytest.raises(psycopg.Error) as err:
            cur.execute(sql, params or [])
    if match:
        assert match in str(err.value)
    return err.value


def test_hold_cannot_be_released_by_raw_sql_without_approvals(w, app_role_conn):
    # Slice 7 change (07a section 9.1 / 11.7): a direct-SQL release must take the settings lock SHARED first;
    # without it the trigger refuses before deciding. With the lock, the approval rules still refuse.
    h = s.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="x")["entity_id"]
    _refused(
        app_role_conn,
        "UPDATE fleet_hold SET released_at = now(), released_by_id = %s WHERE code = %s",
        [w.lead.pk, h],
        match="releasing a hold depends on the deployment mode",
    )
    with app_role_conn.transaction(), app_role_conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock_shared(64942988919)")
        with pytest.raises(psycopg.Error) as err, app_role_conn.transaction():
            cur.execute(
                "UPDATE fleet_hold SET released_at = now(), released_by_id = %s WHERE code = %s",
                [w.lead.pk, h],
            )
        assert "cannot release" in str(err.value)
    assert Hold.objects.get(code=h).released_at is None


def test_hold_cannot_be_deleted_or_rewritten(w, app_role_conn):
    h = s.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="x")["entity_id"]
    _refused(app_role_conn, "DELETE FROM fleet_hold WHERE code = %s", [h], match="cannot be deleted")
    _refused(
        app_role_conn,
        "UPDATE fleet_hold SET reason = 'nothing to see' WHERE code = %s",
        [h],
        match="only allowed change",
    )
    _refused(app_role_conn, "UPDATE fleet_hold SET blocking = false WHERE code = %s", [h])


def test_manifest_and_approvals_are_append_only(w, app_role_conn):
    _refused(
        app_role_conn,
        "UPDATE fleet_configurationmanifest SET autonomy_version = 'forged'",
        match="append-only",
    )
    _refused(app_role_conn, "DELETE FROM fleet_patternapproval", match="append-only")
    _refused(app_role_conn, "DELETE FROM fleet_capabilityapproval", match="append-only")
    _refused(app_role_conn, "TRUNCATE fleet_eligibilitycheck")


def test_robot_lifecycle_identity_and_version_are_guarded(w, app_role_conn):
    _refused(
        app_role_conn,
        "UPDATE fleet_robot SET lifecycle = 'commissioning' WHERE code = 'R-01'",
        match="not allowed",
    )
    _refused(app_role_conn, "UPDATE fleet_robot SET serial = 'other' WHERE code = 'R-01'", match="immutable")
    _refused(app_role_conn, "DELETE FROM fleet_robot WHERE code = 'R-01'", match="cannot be deleted")
    before = Robot.objects.get(code="R-01").version
    with app_role_conn.cursor() as cur:
        cur.execute("UPDATE fleet_robot SET version = 1 WHERE code = 'R-01'")  # attempt to reset the guard
    assert Robot.objects.get(code="R-01").version == before + 1  # the database bumped it instead


def test_release_approval_needs_the_role_and_current_manifest(w, app_role_conn):
    h = Hold.objects.get(
        code=s.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="x")[
            "entity_id"
        ]
    )
    cur_m = Robot.objects.get(code="R-01").current_manifest_id
    insert = (
        "INSERT INTO fleet_releaseapproval (hold_id, slot, approver_id, role_used, manifest_id, "
        "evidence_refs, note, approved_at) VALUES (%s, 'engineering', %s, 'engineering_lead', %s, "
        "'[]', '', now())"
    )
    _refused(app_role_conn, insert, [h.pk, w.ops.pk, cur_m], match="does not hold role")
    _refused(app_role_conn, insert, [h.pk, w.lead.pk, None], match="current configuration")


def test_inspection_result_and_hold_are_enforced(w, app_role_conn):
    r = Robot.objects.get(code="R-01")
    tmpl = r.current_manifest.pattern.templates.get(version="T1").pk
    sql = (
        "INSERT INTO fleet_inspection (code, robot_id, manifest_id, template_id, visit_ref, answers, result, "
        "complete, failed_robot_items, failed_visit_items, performer_id, verifier_id, hold_id, "
        "submitted_by_id, submitted_at) VALUES (%s, %s, %s, %s, '', %s, %s, %s, '[]', '[]', %s, %s, NULL, "
        "%s, now())"
    )
    # forged PASS while an item failed
    _refused(
        app_role_conn,
        sql,
        [
            "X-1",
            r.pk,
            r.current_manifest_id,
            tmpl,
            '{"I1": "fail"}',
            "PASS",
            False,
            w.field.pk,
            w.field2.pk,
            w.field.pk,
        ],
        match="must be FAIL",
    )
    # incomplete reported as PASS
    _refused(
        app_role_conn,
        sql,
        [
            "X-2",
            r.pk,
            r.current_manifest_id,
            tmpl,
            '{"I1": "pass"}',
            "PASS",
            True,
            w.field.pk,
            w.field2.pk,
            w.field.pk,
        ],
        match="must be INCOMPLETE",
    )
    # correct FAIL but no hold for an observed robot-condition failure
    _refused(
        app_role_conn,
        sql,
        [
            "X-3",
            r.pk,
            r.current_manifest_id,
            tmpl,
            '{"I1": "fail"}',
            "FAIL",
            False,
            w.field.pk,
            w.field2.pk,
            w.field.pk,
        ],
        match="must place its own",
    )


def test_runtime_role_cannot_change_deployment_settings(w, app_role_conn):
    _refused(app_role_conn, "UPDATE fleet_deploymentsetting SET value = 'true'", match="permission denied")
    _refused(
        app_role_conn,
        "INSERT INTO fleet_deploymentsetting VALUES ('allow_draft_policies', 'true')",
        match="permission denied",
    )


def test_base_authorization_needs_engineering_lead(w, app_role_conn):
    r = Robot.objects.get(code="R-02")
    _refused(
        app_role_conn,
        "INSERT INTO fleet_baseauthorization (robot_id, pattern_id, granted_by_id, granted_at) "
        "VALUES (%s, %s, %s, now())",
        [r.pk, r.current_manifest.pattern_id, w.ops.pk],
        match="engineering_lead",
    )


def test_failed_inspection_cannot_borrow_an_unrelated_or_earlier_hold(w, app_role_conn):
    """Causality: the hold must be created for THIS inspection in THIS transaction."""
    r = Robot.objects.get(code="R-01")
    tmpl = r.current_manifest.pattern.templates.get(version="T1").pk
    unrelated = Hold.objects.get(
        code=s.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="x")[
            "entity_id"
        ]
    )
    insert = (
        "INSERT INTO fleet_inspection (code, robot_id, manifest_id, template_id, visit_ref, answers, "
        "result, complete, failed_robot_items, failed_visit_items, performer_id, verifier_id, hold_id, "
        "submitted_by_id, submitted_at) VALUES (%s, %s, %s, %s, '', '{\"I1\": \"fail\"}', 'FAIL', false, "
        "'[]', '[]', %s, %s, %s, %s, now())"
    )
    args = [r.pk, r.current_manifest_id, tmpl, w.field.pk, w.field2.pk]
    # Slice 7 change (G-01 binding, 07a 5.2): a raw hold must bind to the active policy, else the newest (here the
    # world's draft v1) with its slots copied exactly; the causality assertions below are unchanged.
    policy = HoldPolicy.objects.order_by("-version").first()
    bound = [policy.pk, Jsonb(policy.rules["inspection"]["slots"])]
    # 1) an unrelated, already-open hold
    _refused(app_role_conn, insert, ["Y-1", *args, unrelated.pk, w.field.pk], match="its own")
    # 2) an inspection-sourced hold with the right reference, but committed in an EARLIER transaction
    with app_role_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fleet_hold (code, scope, robot_id, blocking, source_type, source_ref, reason, "
            "policy_id, required_slots, placed_by_id, placed_at) VALUES ('HD-X', 'robot', %s, true, "
            "'inspection', 'Y-2', 'pre-made', %s, %s, %s, now()) RETURNING id",
            [r.pk, *bound, w.ops.pk],
        )
        premade = cur.fetchone()[0]
    _refused(app_role_conn, insert, ["Y-2", *args, premade, w.field.pk], match="its own")
    # 3) the same transaction, correctly linked, is accepted
    with app_role_conn.transaction(), app_role_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fleet_hold (code, scope, robot_id, blocking, source_type, source_ref, reason, "
            "policy_id, required_slots, placed_by_id, placed_at) VALUES ('HD-Y', 'robot', %s, true, "
            "'inspection', 'Y-3', 'linked', %s, %s, %s, now()) RETURNING id",
            [r.pk, *bound, w.ops.pk],
        )
        linked = cur.fetchone()[0]
        cur.execute(insert, ["Y-3", *args, linked, w.field.pk])
    # 4) a passing inspection may not reference any hold
    _refused(
        app_role_conn,
        insert.replace("'{\"I1\": \"fail\"}', 'FAIL', false", "'{}', 'INCOMPLETE', false"),
        ["Y-4", *args, unrelated.pk, w.field.pk],
        match="may reference a hold",
    )


def test_hold_insert_needs_no_policy_approval_even_in_production_mode(w, app_role_conn):
    with app_role_conn.cursor() as cur:
        cur.execute("SELECT value FROM fleet_deploymentsetting WHERE key = 'allow_draft_policies'")
    Robot.objects.get(code="R-01")
    from tests.fleet_world import set_setting

    set_setting("allow_draft_policies", "false")
    h = s.place_hold(w.ops, scope="robot", target="R-01", source_type="maintenance", reason="prod")[
        "entity_id"
    ]
    assert Hold.objects.get(code=h).released_at is None
