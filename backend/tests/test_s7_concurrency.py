"""Slice 7 races through the COMMANDS, both commit orders (contract 07a sections 9.3 and 11.3/11.8). The first
transaction pauses at a named checkpoint right after taking its locks; the second is started and must be
observed WAITING ON A LOCK in pg_stat_activity (no sleeps decide anything); then the first commits and both
outcomes are checked. Settings changes run as the schema owner in their own transaction (as in production:
the runtime role cannot write settings). SYNTHETIC data only.
"""

import os
import threading
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from django.db import connection

from execution import escalation_deadlines as ed
from execution import policy as pol
from execution.models import EscalationDeadlineSetApproval, IncidentPolicy
from fleet import services as s
from fleet.management.commands.seed_fleet import DRAFT_POLICY_RULES
from fleet.models import Hold, HoldPolicy, HoldRebaseActivation, InspectionTemplate
from planning import services as ps
from planning.models import Person, Qualification
from platform_core import testhooks
from platform_core.commands import Conflict
from readiness import commands as rc
from readiness import evidence as rx
from readiness.models import DepartureAuthorization, DepartureEvent, ReadinessEvidence
from tests.conftest import make_user
from tests.fleet_world import build_world, set_setting
from tests.test_s7_fleet import floor_rules, procedure_policy
from tests.test_s7_readiness import build_b7, submit

pytestmark = pytest.mark.django_db(transaction=True)
WAIT_S = 20


# ================================================================== machinery
def _conn(owner: bool = True) -> psycopg.Connection:
    who = "OWNER" if owner else "APP"
    default = "ops_owner" if owner else "ops_app"
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5440"),
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get(f"DB_{who}_USER", default),
        password=os.environ.get(f"DB_{who}_PASSWORD", f"{default}_dev"),
        autocommit=False,
    )


def wait_for_lock_wait(timeout: float = WAIT_S) -> None:
    """Block until some backend of THIS test database waits on a lock (row lock or advisory lock)."""
    with _conn() as obs:
        obs.autocommit = True
        with obs.cursor() as cur:
            for _ in range(int(timeout / 0.02)):
                cur.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
                )
                if cur.fetchone()[0]:
                    return
                threading.Event().wait(0.02)
    raise AssertionError("the second transaction never waited on a lock")


def _thread(name: str, fn: Callable[[], Any], out: dict[str, Any]) -> threading.Thread:
    def run() -> None:
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001 - the outcome is asserted by the test
            out[name] = e
        finally:
            connection.close()

    t = threading.Thread(target=run, name=name)
    t.start()
    return t


def race(pause_at: str, first: Callable[[], Any], second: Callable[[], Any]) -> tuple[Any, Any]:
    """`first` pauses at checkpoint `pause_at` holding its locks; `second` must then wait on a lock."""
    paused, go = threading.Event(), threading.Event()

    def hook() -> None:
        if threading.current_thread().name == "first":
            paused.set()
            assert go.wait(WAIT_S)

    testhooks.register(pause_at, hook)
    out: dict[str, Any] = {}
    try:
        t1 = _thread("first", first, out)
        assert paused.wait(WAIT_S), f"first never reached {pause_at}: {out.get('first')!r}"
        t2 = _thread("second", second, out)
        wait_for_lock_wait()
        assert t2.is_alive()
        go.set()
        t1.join(WAIT_S)
        t2.join(WAIT_S)
    finally:
        go.set()
        testhooks.clear()
    return out.get("first"), out.get("second")


def setting_first(key: str, value: str, command: Callable[[], Any]) -> Any:
    """The settings change holds the settings lock X (uncommitted); `command` must wait on it; then commit."""
    out: dict[str, Any] = {}
    with _conn() as owner, owner.cursor() as cur:
        cur.execute("UPDATE fleet_deploymentsetting SET value = %s WHERE key = %s", [value, key])
        t = _thread("second", command, out)
        wait_for_lock_wait()
        assert t.is_alive()
        owner.commit()
        t.join(WAIT_S)
    return out.get("second")


def command_first(pause_at: str, command: Callable[[], Any], key: str, value: str) -> Any:
    """`command` pauses holding the settings lock S; the settings change must wait; then both commit."""

    def change() -> None:
        with _conn() as owner, owner.cursor() as cur:
            cur.execute("UPDATE fleet_deploymentsetting SET value = %s WHERE key = %s", [value, key])
            owner.commit()

    first, second = race(pause_at, command, change)
    assert second is None, second
    return first


def no_wait_while_settings_locked(
    command: Callable[[], Any], key: str = "demo_mode", value: str = "false"
) -> Any:
    """`command` completes while a settings change holds the settings lock X: it never waits on (or reads)
    deployment configuration. A wait would hit the 3 s lock_timeout and raise."""
    out: dict[str, Any] = {}
    with _conn() as owner, owner.cursor() as cur:
        cur.execute("UPDATE fleet_deploymentsetting SET value = %s WHERE key = %s", [value, key])
        t = _thread("cmd", command, out)
        t.join(WAIT_S)
        assert not t.is_alive()
        owner.rollback()
    return out.get("cmd")


@pytest.fixture
def fw():
    world = build_world(robots=2, prefix="c7_")
    world.lead2 = make_user("c7_lead2", "engineering_lead")
    yield world
    testhooks.clear()


def _hold(w: Any, robot: str = "R-01", source: str = "maintenance") -> Hold:
    code = s.place_hold(w.ops, scope="robot", target=robot, source_type=source, reason="SYNTHETIC")[
        "entity_id"
    ]
    return Hold.objects.get(code=code)


def _released(h: Hold) -> bool:
    return Hold.objects.get(pk=h.pk).released_at is not None


# ================================================================== case b: mode switch vs release
def test_release_first_commits_under_demo_while_the_switch_waits(fw):
    h = _hold(fw)  # bound to the synthetic draft; releasable only via allow_draft_policies in demo
    s.approve_release(fw.lead, hold_code=h.code, slot="engineering", role_used="engineering_lead")
    out = command_first(
        "locked:fleet.release_hold", lambda: s.release_hold(fw.ops, hold_code=h.code), "demo_mode", "false"
    )
    assert isinstance(out, dict) and out["released"] and _released(h)  # decided under demo, before the switch


def test_switch_first_release_waits_and_is_refused_under_production(fw):
    h = _hold(fw)
    s.approve_release(fw.lead, hold_code=h.code, slot="engineering", role_used="engineering_lead")
    out = setting_first("demo_mode", "false", lambda: s.release_hold(fw.ops, hold_code=h.code))
    assert isinstance(out, Exception) and "not usable in production" in str(out), out
    assert not _released(h)  # never released in production under a draft


# ================================================================== mode switch vs placement / rebasing
def test_mode_switch_never_blocks_placement_or_rebasing_and_changes_nothing(fw):
    """Placement and rebasing are mode-independent (section 9.1): they complete while a settings change holds
    the settings lock X, and the binding / outcome is identical either way."""
    procedure = procedure_policy(fw)
    h0 = _hold(fw)  # bound to the active approved procedure
    assert h0.policy == procedure
    placed = no_wait_while_settings_locked(lambda: _hold(fw, "R-02"))
    assert isinstance(placed, Hold) and placed.policy == procedure
    placed = no_wait_while_settings_locked(lambda: _hold(fw, "R-02"), "allow_draft_policies", "false")
    assert isinstance(placed, Hold) and placed.policy == procedure
    # a hold under the draft (bound before the procedure policy existed) is re-based while the switch is held
    s.withdraw_hold_policy(fw.lead, version=procedure.version, reason="SYNTHETIC")
    draft_hold = _hold(fw)  # binds to the newest: the withdrawn procedure (no active policy)
    newer = procedure_policy(fw)
    req = no_wait_while_settings_locked(
        lambda: s.request_hold_rebase(fw.lead, hold_code=draft_hold.code, reason="SYNTHETIC")
    )
    assert isinstance(req, dict), req
    assert isinstance(
        no_wait_while_settings_locked(
            lambda: s.approve_hold_rebase(fw.lead2, rebase_id=req["id"], slot="engineering")
        ),
        dict,
    )
    final = no_wait_while_settings_locked(
        lambda: s.approve_hold_rebase(fw.safety, rebase_id=req["id"], slot="safety")
    )
    assert isinstance(final, dict) and final["effective"]
    assert s.hold_effective_rule(Hold.objects.get(pk=draft_hold.pk)).policy == newer


# ================================================================== case c: activation vs placement
def _setup_start(fw: Any, start: str) -> tuple[Callable[[], Any], int | None]:
    """Returns (the activating command, the binding just before it)."""
    if start == "no_policy":
        with connection.cursor() as cur:  # test-only owner rewrite: no policy at all
            cur.execute("ALTER TABLE fleet_holdpolicy DISABLE TRIGGER USER")
            cur.execute("DELETE FROM fleet_holdpolicy")
            cur.execute("ALTER TABLE fleet_holdpolicy ENABLE TRIGGER USER")
        return (lambda: s.create_hold_policy(fw.eng, rules=DRAFT_POLICY_RULES)), None
    if start == "active_older":  # v1 approved (active); a newer procedure policy is one slot from active
        s.approve_hold_policy(fw.lead2, version=1, slot="engineering")
        s.approve_hold_policy(fw.safety, version=1, slot="safety")
    # draft_only: only drafts exist (v1 and the newer procedure policy, not yet fully approved)
    newer = procedure_policy(fw, approve=False)
    s.approve_hold_policy(fw.lead, version=newer.version, slot="engineering")
    s.approve_hold_policy(fw.safety, version=newer.version, slot="safety")
    before = s.binding_policy()
    assert before is not None
    expected = HoldPolicy.objects.get(version=1).pk if start == "active_older" else newer.pk
    assert before.pk == expected
    return (lambda: s.approve_hold_policy(fw.founder, version=newer.version, slot="founder")), before.pk


@pytest.mark.parametrize("start", ["active_older", "draft_only", "no_policy"])
def test_placement_first_keeps_the_older_binding(fw, start):
    activate, before = _setup_start(fw, start)
    first, second = race("locked:fleet.hold_binding", lambda: _hold(fw), activate)
    assert isinstance(first, Hold) and isinstance(second, dict), (first, second)
    assert first.policy_id == before  # fixed for life
    assert Hold.objects.get(pk=first.pk).policy_id == before


@pytest.mark.parametrize("start", ["active_older", "draft_only", "no_policy"])
def test_activation_first_placement_binds_the_new_policy(fw, start):
    activate, _before = _setup_start(fw, start)
    label = "locked:fleet.create_hold_policy" if start == "no_policy" else "locked:fleet.approve_hold_policy"
    first, second = race(label, activate, lambda: _hold(fw))
    assert isinstance(first, dict) and isinstance(second, Hold), (first, second)
    assert second.policy_id == s.binding_policy().pk


# ================================================================== policy withdrawal vs release
def test_withdrawal_first_release_waits_and_is_refused(fw):
    v1 = HoldPolicy.objects.get(version=1)
    s.approve_hold_policy(fw.lead2, version=1, slot="engineering")
    s.approve_hold_policy(fw.safety, version=1, slot="safety")
    h = _hold(fw)
    s.approve_release(fw.lead, hold_code=h.code, slot="engineering", role_used="engineering_lead")
    first, second = race(
        "locked:fleet.withdraw_hold_policy",
        lambda: s.withdraw_hold_policy(fw.safety, version=v1.version, reason="SYNTHETIC"),
        lambda: s.release_hold(fw.ops, hold_code=h.code),
    )
    assert isinstance(first, dict) and isinstance(second, Exception) and "was withdrawn" in str(second)
    assert not _released(h)


def test_release_first_withdrawal_waits(fw):
    s.approve_hold_policy(fw.lead2, version=1, slot="engineering")
    s.approve_hold_policy(fw.safety, version=1, slot="safety")
    h = _hold(fw)
    s.approve_release(fw.lead, hold_code=h.code, slot="engineering", role_used="engineering_lead")
    first, second = race(
        "locked:fleet.release_hold",
        lambda: s.release_hold(fw.ops, hold_code=h.code),
        lambda: s.withdraw_hold_policy(fw.safety, version=1, reason="SYNTHETIC"),
    )
    assert isinstance(first, dict) and isinstance(second, dict) and _released(h)


# ================================================================== case d: withdrawal vs final rebase approval
def _pending_rebase(fw: Any) -> tuple[Hold, HoldPolicy, int]:
    h = _hold(fw)  # under the synthetic draft
    p = procedure_policy(fw)
    rid = s.request_hold_rebase(fw.lead, hold_code=h.code, reason="SYNTHETIC")["id"]
    s.approve_hold_rebase(fw.lead2, rebase_id=rid, slot="engineering")
    return h, p, rid


def test_destination_withdrawn_first_final_rebase_approval_refused(fw):
    h, p, rid = _pending_rebase(fw)
    first, second = race(
        "locked:fleet.withdraw_hold_policy",
        lambda: s.withdraw_hold_policy(fw.lead, version=p.version, reason="SYNTHETIC"),
        lambda: s.approve_hold_rebase(fw.safety, rebase_id=rid, slot="safety"),
    )
    assert isinstance(first, dict)
    assert isinstance(
        second, Exception
    ) and "is no longer the active production policy (active: none)" in str(second)
    assert not HoldRebaseActivation.objects.exists()


def test_final_rebase_approval_first_withdrawal_waits(fw):
    h, p, rid = _pending_rebase(fw)
    first, second = race(
        "locked:fleet.approve_hold_rebase",
        lambda: s.approve_hold_rebase(fw.safety, rebase_id=rid, slot="safety"),
        lambda: s.withdraw_hold_policy(fw.lead, version=p.version, reason="SYNTHETIC"),
    )
    assert isinstance(first, dict) and first["effective"] and isinstance(second, dict)
    # the rebase stands (decided first); its destination is now withdrawn, so the hold needs a new decision
    assert "was withdrawn" in s.hold_release_status(h)["problem"]


# ================================================================== case e: competing rebases
@pytest.mark.parametrize("winner", [0, 1])
def test_competing_rebases_at_most_one_activation(fw, winner):
    h = _hold(fw)
    procedure_policy(fw)
    ids = [s.request_hold_rebase(fw.lead, hold_code=h.code, reason=f"SYNTHETIC {n}")["id"] for n in range(2)]
    for rid in ids:
        s.approve_hold_rebase(fw.lead2, rebase_id=rid, slot="engineering")
    win, lose = ids[winner], ids[1 - winner]
    first, second = race(
        "locked:fleet.approve_hold_rebase",
        lambda: s.approve_hold_rebase(fw.safety, rebase_id=win, slot="safety"),
        lambda: s.approve_hold_rebase(fw.safety, rebase_id=lose, slot="safety"),
    )
    assert isinstance(first, dict) and first["effective"]
    win_code = h.rebases.get(pk=win).code
    assert isinstance(second, Exception) and f"superseded by rebase {win_code}" in str(second)
    assert list(HoldRebaseActivation.objects.values_list("rebase_id", flat=True)) == [win]


# ================================================================== rebase final approval vs release
def test_final_rebase_approval_first_then_release_sees_the_new_rule(fw):
    h, _p, rid = _pending_rebase(fw)
    s.approve_release(
        fw.lead, hold_code=h.code, slot="engineering", role_used="engineering_lead"
    )  # pre-rebase
    first, second = race(
        "locked:fleet.approve_hold_rebase",
        lambda: s.approve_hold_rebase(fw.safety, rebase_id=rid, slot="safety"),
        lambda: s.release_hold(fw.ops, hold_code=h.code),
    )
    assert isinstance(first, dict) and first["effective"]
    assert isinstance(second, Exception) and "missing approvals" in str(second)  # old approvals do not count
    assert not _released(h)


def test_release_first_then_final_rebase_approval_refused(fw):
    h, _p, rid = _pending_rebase(fw)
    s.approve_release(fw.lead, hold_code=h.code, slot="engineering", role_used="engineering_lead")
    first, second = race(
        "locked:fleet.release_hold",
        lambda: s.release_hold(fw.ops, hold_code=h.code),
        lambda: s.approve_hold_rebase(fw.safety, rebase_id=rid, slot="safety"),
    )
    assert isinstance(first, dict) and _released(h)
    assert isinstance(second, Exception) and "already released" in str(second)


# ================================================================== readiness world races
@pytest.fixture
def rw(monkeypatch):
    world = build_b7(monkeypatch, "c7r_")
    yield world
    testhooks.clear()


def _periodic_only_signers(w: Any) -> None:
    """R-01's relied periodic inspection is signed ONLY by P-8 + P-9: not crew, not recovery people, not the
    pre-departure signers. The pre-departure inspection is on a template approved for pre-departure only."""
    from tests.fleet_world import make_engineering_template
    from tests.planning_world import grant_inspector

    w.i8, w.i9 = make_user("c7_i8", "field_operator"), make_user("c7_i9", "field_operator")
    for user, code in ((w.i8, "P-8"), (w.i9, "P-9")):
        ps.register_person(w.ops, username=user.username, code=code)
        grant_inspector(w, code)
    make_engineering_template(w, "P-A", "PD", purposes=("pre_departure",))
    insp = submit(w, template="PD", visit_ref=w.visit)
    rx.record_evidence(
        w.ops, visit_code=w.visit, kind="pre_departure_inspection", result="pass", inspection_code=insp
    )
    submit(w, by=(w.i8, w.i9))  # T1 (periodic in demo): the newest qualifying periodic inspection


def _q(person: str) -> Qualification:
    return Qualification.objects.get(person__code=person, kind="readiness_inspector", revoked_at__isnull=True)


def test_periodic_only_signer_revoked_after_authorization_departure_refused(rw):
    """Case a (command level): the authorization relied on P-8/P-9's periodic inspection; revoking P-8 before
    departure refuses the departure (person version and the dependency re-check)."""
    _periodic_only_signers(rw)
    auth = rc.authorize_departure(rw.ops, visit_code=rw.visit)
    a = DepartureAuthorization.objects.get(code=auth["code"])
    periodic = a.periodic_inspection
    assert periodic is not None and {periodic.performer_id, periodic.verifier_id} == {rw.i8.pk, rw.i9.pk}
    for code in ("P-8", "P-9"):
        assert str(Person.objects.get(code=code).pk) in a.guard_snapshot["person_versions"]
    ps.revoke_qualification(rw.ops, qualification_id=_q("P-8").pk, reason="SYNTHETIC")
    with pytest.raises(Conflict):
        rc.record_departure(rw.ops, authorization_code=a.code)
    assert not DepartureEvent.objects.exists()


def test_revoke_periodic_only_signer_first_authorize_waits_and_is_refused(rw):
    _periodic_only_signers(rw)
    p8 = Person.objects.get(code="P-8")
    first, second = race(
        "locked:planning.revoke_qualification",
        lambda: ps.revoke_qualification(rw.ops, qualification_id=_q("P-8").pk, reason="SYNTHETIC"),
        lambda: rc.authorize_departure(rw.ops, visit_code=rw.visit),
    )
    assert isinstance(first, dict)
    # The authorization waited for the revocation, so it can never rely on P-8: it is either refused, or
    # issued on an OLDER inspection that still qualifies (the world's field/field2 T1 inspections).
    if isinstance(second, dict):
        a = DepartureAuthorization.objects.get(code=second["code"])
        assert a.periodic_inspection is not None and rw.i8.pk not in (
            a.periodic_inspection.performer_id,
            a.periodic_inspection.verifier_id,
        )
        assert str(p8.pk) not in a.guard_snapshot["person_versions"]
    else:
        assert isinstance(second, Conflict), second


def test_authorize_first_revoke_periodic_only_signer_waits_then_departure_refused(rw):
    _periodic_only_signers(rw)
    first, second = race(
        "locked:readiness.authorize_departure",
        lambda: rc.authorize_departure(rw.ops, visit_code=rw.visit),
        lambda: ps.revoke_qualification(rw.ops, qualification_id=_q("P-8").pk, reason="SYNTHETIC"),
    )
    assert isinstance(first, dict) and isinstance(second, dict), (first, second)
    a = DepartureAuthorization.objects.get(code=first["code"])
    assert a.periodic_inspection is not None and a.periodic_inspection.performer_id in (rw.i8.pk, rw.i9.pk)
    with pytest.raises(Conflict):
        rc.record_departure(rw.ops, authorization_code=first["code"])
    assert not DepartureEvent.objects.exists()


def _fresh_visit_with_inspection(w: Any) -> tuple[str, str]:
    visit = w.new_confirmed_visit(robot="R-02", h0=15, h1=16)  # no overlap with the world's visit
    return visit, submit(w, robot="R-02", visit_ref=visit)


def test_revoke_first_record_evidence_waits_and_is_refused(rw):
    visit, insp = _fresh_visit_with_inspection(rw)
    first, second = race(
        "locked:planning.revoke_qualification",
        lambda: ps.revoke_qualification(rw.ops, qualification_id=_q("P-3").pk, reason="SYNTHETIC"),
        lambda: rx.record_evidence(
            rw.ops, visit_code=visit, kind="pre_departure_inspection", result="pass", inspection_code=insp
        ),
    )
    assert isinstance(first, dict) and isinstance(second, Exception) and "P-3" in str(second)


def test_record_evidence_first_revoke_waits(rw):
    visit, insp = _fresh_visit_with_inspection(rw)
    first, second = race(
        "locked:readiness.record_evidence",
        lambda: rx.record_evidence(
            rw.ops, visit_code=visit, kind="pre_departure_inspection", result="pass", inspection_code=insp
        ),
        lambda: ps.revoke_qualification(rw.ops, qualification_id=_q("P-3").pk, reason="SYNTHETIC"),
    )
    assert isinstance(first, dict) and isinstance(second, dict)
    ev = ReadinessEvidence.objects.get(code=first["entity_id"])
    assert ev.result == "pass"  # recorded before the revocation; authorization re-checks the signers
    with pytest.raises(Exception, match="INSPECTOR_NOT_QUALIFIED|not a qualified readiness inspector"):
        rc.authorize_departure(rw.ops, visit_code=visit)


def test_template_withdrawal_first_authorize_waits_and_is_refused(rw):
    t1 = InspectionTemplate.objects.get(pattern__code="P-A", version="T1")
    first, second = race(
        "locked:fleet.withdraw_inspection_template",
        lambda: s.withdraw_inspection_template(rw.safety, template_id=t1.pk, reason="SYNTHETIC"),
        lambda: rc.authorize_departure(rw.ops, visit_code=rw.visit),
    )
    assert isinstance(first, dict)
    assert isinstance(second, Exception) and "PERIODIC_INSPECTION_MISSING" in str(second), second


def test_authorize_first_template_withdrawal_waits_then_departure_refused(rw):
    t1 = InspectionTemplate.objects.get(pattern__code="P-A", version="T1")
    first, second = race(
        "locked:readiness.authorize_departure",
        lambda: rc.authorize_departure(rw.ops, visit_code=rw.visit),
        lambda: s.withdraw_inspection_template(rw.safety, template_id=t1.pk, reason="SYNTHETIC"),
    )
    assert isinstance(first, dict) and isinstance(second, dict), (first, second)
    with pytest.raises(Exception, match="version|periodic"):
        rc.record_departure(rw.ops, authorization_code=first["code"])


# ================================================================== mode switch vs record_evidence / deadline approval
def _production_ready_inspection(w: Any) -> tuple[str, str]:
    """A fresh visit with a T1 (synthetic) PASS inspection: passing evidence is accepted in demo only."""
    return _fresh_visit_with_inspection(w)


def test_record_evidence_first_commits_under_demo_switch_waits(rw):
    visit, insp = _production_ready_inspection(rw)
    out = command_first(
        "locked:readiness.record_evidence",
        lambda: rx.record_evidence(
            rw.ops, visit_code=visit, kind="pre_departure_inspection", result="pass", inspection_code=insp
        ),
        "demo_mode",
        "false",
    )
    assert isinstance(out, dict) and out["result"] == "pass"


def test_switch_first_record_evidence_waits_and_is_refused(rw):
    visit, insp = _production_ready_inspection(rw)
    out = setting_first(
        "demo_mode",
        "false",
        lambda: rx.record_evidence(
            rw.ops, visit_code=visit, kind="pre_departure_inspection", result="pass", inspection_code=insp
        ),
    )
    assert isinstance(out, Exception) and "not approved for pre-departure use in production" in str(out)


def _deadline_set(w: Any) -> int:
    rules = {"severity_scale": ["S3", "S2"], "escalation_required_from": "S2", "downgrade_roles": []}
    v = int(pol.create_incident_policy(w.lead, rules=rules, source="synthetic_demo")["entity_id"])
    pol.approve_incident_policy(w.lead2, version=v, slot="engineering")
    pol.approve_incident_policy(w.safety, version=v, slot="safety")
    policy = IncidentPolicy.objects.get(version=v)
    deadlines = {
        lvl: [{"to_role": "safety_reviewer", "within_min": 600}] for lvl in policy.rules["severity_scale"]
    }
    deadlines["unclassified"] = []
    return int(
        ed.create_deadline_set(
            w.ops, incident_policy_version=v, deadlines=deadlines, source="synthetic_demo"
        )["entity_id"]
    )


def test_deadline_approval_vs_mode_switch_both_orders(fw):
    sid = _deadline_set(fw)
    out = command_first(
        "locked:execution.approve_deadline_set",
        lambda: ed.approve_deadline_set(fw.safety, set_id=sid, slot="safety"),
        "demo_mode",
        "false",
    )
    assert isinstance(out, dict)  # approved under demo, before the switch
    set_setting("demo_mode", "true")
    sid2 = _deadline_set(fw)
    out = setting_first(
        "demo_mode", "false", lambda: ed.approve_deadline_set(fw.safety, set_id=sid2, slot="safety")
    )
    assert isinstance(out, Exception) and "cannot be approved in production" in str(out)
    assert not EscalationDeadlineSetApproval.objects.filter(deadline_set_id=sid2).exists()


def test_floor_rules_fixture_is_floor_valid():
    assert s.hold_policy_floor_problem(floor_rules()) is None
