"""Session events, interruptions and recovery dispositions (Slice 4, Engineer C). Contract sections 2, 3, 4.

Session: not_started -> active <-> paused -> stopped (from SessionEvent). `stop` from active or paused,
`stopped` is terminal, `observation` in any state while the run is open. Gates for start and resume,
checked here under locks with an explained refusal AND by the insert trigger:
  - the referenced site check is the run's latest and 'go' (for resume: newer than the latest pause and every
    interruption of the run);
  - no open interruption on the run;
  - no unreleased blocking hold on the robot or its current configuration pattern;
  - custody = site of this visit, verified;
  - valid consents covering the revision's scopes >= the check's adults present (withdrawal after the check
    blocks);
  - recorder: a field user who is field crew of the run, or an ops_lead.

Locks (U = FOR UPDATE, S = FOR SHARE), after run_command's role-grant S:
  record_session_event   site S, pattern S, robot S, persons S (run crew), visit S, run U
  open_interruption      robot S, run U
  resolve_interruption   robot S, run U
  record_disposition     pattern S, robot S, run U
report_incident takes the robot U, so it and a start/resume are totally ordered; consent withdrawal takes the
site U, so it and a start/resume are totally ordered. SYNTHETIC data only; nothing here operates a robot.
"""

from typing import Any

from django.db import connection

from execution.custody import current_custody
from execution.incidents import next_code, next_session_seq, session_state
from execution.models import (
    DISPOSITIONS,
    CustodyEvent,
    ExecutionRun,
    Incident,
    Interruption,
    InterruptionResolution,
    RecoveryDisposition,
    SessionEvent,
    SiteCheck,
    TripClosure,
)
from execution.policy import has_role_now
from fleet.models import ConfigurationManifest, Hold, Robot
from planning.models import Person
from platform_core.commands import Conflict, Invalid, Outcome, PermissionDenied, RetryTransaction, run_command
from platform_core.hubscope import run_hub
from platform_core.locks import lock_guards
from platform_core.testhooks import checkpoint

KINDS = ["start", "pause", "resume", "stop", "observation"]
DECISIONS = [k for k, _ in DISPOSITIONS]
ALLOWED_FROM = {
    "start": {"not_started"},
    "pause": {"active"},
    "resume": {"paused"},
    "stop": {"active", "paused"},
}


# ------------------------------------------------------------------ helpers
def _run(code: str) -> ExecutionRun:
    try:
        return ExecutionRun.objects.select_related("revision").get(code=code)
    except ExecutionRun.DoesNotExist:
        raise Invalid(f"No execution run {code}") from None


def run_closed(run: ExecutionRun) -> bool:
    return TripClosure.objects.filter(run=run).exists()


def _pattern_id(robot_id: int) -> int | None:
    manifest_id = Robot.objects.filter(pk=robot_id).values_list("current_manifest_id", flat=True).first()
    if manifest_id is None:
        return None
    return ConfigurationManifest.objects.filter(pk=manifest_id).values_list("pattern_id", flat=True).first()


def blocking_holds(robot_id: int) -> list[str]:
    """Codes of unreleased blocking holds on the robot or its current pattern (same rule as SQL
    execution_robot_held)."""
    pattern_id = _pattern_id(robot_id)
    robot_holds = Hold.objects.filter(
        blocking=True, released_at__isnull=True, scope="robot", robot_id=robot_id
    )
    codes = list(robot_holds.order_by("pk").values_list("code", flat=True))
    if pattern_id is not None:
        codes += list(
            Hold.objects.filter(
                blocking=True, released_at__isnull=True, scope="manifest_pattern", pattern_id=pattern_id
            )
            .order_by("pk")
            .values_list("code", flat=True)
        )
    return codes


def open_interruptions(run: ExecutionRun) -> list[str]:
    return list(
        Interruption.objects.filter(run=run, resolution__isnull=True)
        .order_by("pk")
        .values_list("code", flat=True)
    )


def valid_consents(run: ExecutionRun) -> int:
    with connection.cursor() as cur:
        cur.execute("SELECT execution_valid_consents(%s)", [run.pk])
        return int(cur.fetchone()[0])


def _is_field_user(user_id: int) -> bool:
    return has_role_now(user_id, "ops_lead") or has_role_now(user_id, "field_operator")


def _is_crew_user(run: ExecutionRun, user_id: int) -> bool:
    rev = run.revision
    return Person.objects.filter(user_id=user_id, pk__in=[rev.lead_id, rev.second_id]).exists()


def _start_resume_problems(run: ExecutionRun, kind: str, check: SiteCheck) -> str | None:
    """The first unmet gate for start/resume (contract section 3), in plain words; None if all pass."""
    latest = SiteCheck.objects.filter(run=run).order_by("-recorded_at", "-id").first()
    if latest is None or latest.pk != check.pk:
        return "Stale site check: it is not the run's latest; use the latest site check or record a new one"
    if check.result != "go":
        return "The latest site check is No-Go; record a new site check that is Go first"
    if kind == "resume":
        last_pause = (
            SessionEvent.objects.filter(run=run, kind="pause")
            .order_by("-recorded_at")
            .values_list("recorded_at", flat=True)
        ).first()
        last_interruption = (
            Interruption.objects.filter(run=run)
            .order_by("-recorded_at")
            .values_list("recorded_at", flat=True)
        ).first()
        if (last_pause is not None and check.recorded_at <= last_pause) or (
            last_interruption is not None and check.recorded_at <= last_interruption
        ):
            return (
                "Stale site check: resuming needs a new Go site check recorded after the pause and after "
                "every interruption"
            )
    open_codes = open_interruptions(run)
    if open_codes:
        return f"The run has an open interruption ({', '.join(open_codes)}); it must be resolved first"
    holds = blocking_holds(run.robot_id)
    if holds:
        return (
            f"The robot or its configuration pattern has an unreleased blocking hold ({', '.join(holds)}); "
            "it must be released through the hold-release workflow"
        )
    c = current_custody(run.robot_id)
    ev = CustodyEvent.objects.filter(pk=c.custody_event_id).first() if c.custody_event_id else None
    if c.kind != "site" or ev is None or ev.visit_id != run.visit_id:
        return f"The robot is not in site custody for this visit (custody: {c.label})"
    if not c.verified:
        return "Site custody is not verified"
    n = valid_consents(run)
    if n < check.adults_present:
        return (
            f"Only {n} valid consent(s) cover this visit but the site check recorded {check.adults_present} "
            "adult(s) present"
        )
    return None


# ------------------------------------------------------------------ session events
def record_session_event(
    actor: Any, *, run_code: str, kind: str, site_check_id: int | None = None, note: str = ""
) -> dict[str, Any]:
    """Record start / pause / resume / stop / observation on the run's session. start and resume need
    `site_check_id` (the run's latest Go check) and pass every gate above; observation needs a note."""
    payload = {"run": run_code, "kind": kind, "site_check_id": site_check_id, "note": note}

    def handler() -> Outcome:
        if kind not in KINDS:
            raise Invalid(f"kind must be one of {KINDS}")
        needs_check = kind in ("start", "resume")
        if needs_check and site_check_id is None:
            raise Invalid(f"{kind} needs the site check it relies on")
        if not needs_check and site_check_id is not None:
            raise Invalid(f"{kind} does not take a site check")
        if kind == "observation" and not note.strip():
            raise Invalid("An observation needs a note")
        run = _run(run_code)
        rev = run.revision
        read_pattern = _pattern_id(run.robot_id)
        checkpoint("read:execution.record_session_event")
        lock_guards(
            share_sites=[run.visit.site_id],
            share_patterns=[read_pattern] if read_pattern else [],
            share_robots=[run.robot_id],
            share_persons=[p for p in (rev.lead_id, rev.second_id) if p],
            share_visits=[run.visit_id],
            update_runs=[run.pk],
            label="execution.record_session_event",
        )
        if _pattern_id(run.robot_id) != read_pattern:
            raise RetryTransaction()  # the robot's configuration changed between our read and our locks
        if run_closed(run):
            raise Conflict(f"Run {run.code} is closed")
        state = session_state(run.pk)
        if kind in ALLOWED_FROM and state not in ALLOWED_FROM[kind]:
            if state == "stopped":
                raise Conflict(f"Cannot {kind}: the session is stopped, and a stopped session never restarts")
            raise Conflict(f"Cannot {kind}: the session is {state.replace('_', ' ')}")
        if needs_check:
            if not (
                has_role_now(actor.pk, "ops_lead")
                or (_is_field_user(actor.pk) and _is_crew_user(run, actor.pk))
            ):
                raise PermissionDenied(
                    f"{kind} must be recorded by field crew of run {run.code} holding a field role, "
                    "or an ops_lead"
                )
            check = SiteCheck.objects.filter(pk=int(site_check_id or 0), run=run).first()
            if check is None:
                raise Invalid(f"Site check {site_check_id} is not a site check of run {run.code}")
            problem = _start_resume_problems(run, kind, check)
            if problem:
                raise Conflict(f"Cannot {kind}: {problem}")
        ev = SessionEvent.objects.create(
            run=run,
            seq=next_session_seq(run.pk),
            kind=kind,
            site_check_id=site_check_id if needs_check else None,
            note=note,
            recorded_by=actor,
        )
        return Outcome(
            "session_event",
            f"{run.code}#{ev.seq}",
            {"run": run.code, "seq": ev.seq, "kind": kind, "state": session_state(run.pk)},
        )

    return run_command(
        actor,
        "execution.record_session_event",
        permission="execution.record_field_event",
        payload=payload,
        handler=handler,
        hub=run_hub(run_code),
    )


# ------------------------------------------------------------------ interruptions
def open_interruption(actor: Any, *, run_code: str, reason: str) -> dict[str, Any]:
    """An operator interruption. If the session is active it is paused in the same transaction (linked to the
    interruption), so the session never stays active while an interruption is open. Locks: robot S, run U."""

    def handler() -> Outcome:
        if not reason.strip():
            raise Invalid("An interruption needs a reason")
        run = _run(run_code)
        checkpoint("read:execution.open_interruption")
        lock_guards(share_robots=[run.robot_id], update_runs=[run.pk], label="execution.open_interruption")
        if run_closed(run):
            raise Conflict(f"Run {run.code} is closed")
        it = Interruption.objects.create(
            code=next_code("execution_interruption_code_seq", "IN"),
            run=run,
            source="operator",
            reason=reason,
            recorded_by=actor,
        )
        paused = False
        if session_state(run.pk) == "active":
            SessionEvent.objects.create(
                run=run,
                seq=next_session_seq(run.pk),
                kind="pause",
                interruption=it,
                note=f"Paused by interruption {it.code}",
                recorded_by=actor,
            )
            paused = True
        return Outcome(
            "interruption",
            it.code,
            {
                "code": it.code,
                "run": run.code,
                "source": "operator",
                "session_paused": paused,
                "resolved": False,
            },
        )

    return run_command(
        actor,
        "execution.open_interruption",
        permission="execution.record_field_event",
        payload={"run": run_code, "reason": reason},
        handler=handler,
        hub=run_hub(run_code),
    )


def resolve_interruption(actor: Any, *, code: str, note: str) -> dict[str, Any]:
    """Resolve one interruption. An incident interruption cannot be resolved while its incident hold is
    unreleased (release goes through the fleet hold-release workflow; nothing here releases it). Resolving
    does not resume the session: resume needs a new Go site check. Locks: robot S, run U."""

    def handler() -> Outcome:
        if not note.strip():
            raise Invalid("A resolution needs a note")
        try:
            it = Interruption.objects.select_related("run").get(code=code)
        except Interruption.DoesNotExist:
            raise Invalid(f"No interruption {code}") from None
        run = it.run
        checkpoint("read:execution.resolve_interruption")
        lock_guards(share_robots=[run.robot_id], update_runs=[run.pk], label="execution.resolve_interruption")
        if InterruptionResolution.objects.filter(interruption=it).exists():
            raise Conflict(f"Interruption {code} is already resolved")
        if it.incident_id is not None:
            hold = Hold.objects.get(incident__pk=it.incident_id)
            if hold.released_at is None:
                inc_code = it.incident.code if it.incident else ""
                raise Conflict(
                    f"Interruption {code} comes from incident {inc_code}, whose hold "
                    f"{hold.code} is not released; release it through the hold-release workflow first"
                )
        InterruptionResolution.objects.create(interruption=it, note=note, recorded_by=actor)
        return Outcome(
            "interruption",
            it.code,
            {
                "code": it.code,
                "run": run.code,
                "resolved": True,
                "open_interruptions": open_interruptions(run),
            },
        )

    return run_command(
        actor,
        "execution.resolve_interruption",
        permission="execution.triage_incident",
        payload={"code": code, "note": note},
        handler=handler,
    )


# ------------------------------------------------------------------ recovery dispositions
def record_disposition(
    actor: Any, *, run_code: str, decision: str, reason: str, incident_code: str | None = None
) -> dict[str, Any]:
    """continue_visit / end_visit_return / robot_left_on_site / robot_unaccounted (the last two need an
    incident of this run; robot_unaccounted a robot_unaccounted incident). continue_visit is refused while the
    robot or its pattern has an unreleased blocking hold or the run has an open interruption; nothing
    overrides that. Locks: pattern S, robot S, run U."""
    payload = {"run": run_code, "decision": decision, "reason": reason, "incident": incident_code}

    def handler() -> Outcome:
        if decision not in DECISIONS:
            raise Invalid(f"decision must be one of {DECISIONS}")
        if not reason.strip():
            raise Invalid("A disposition needs a reason")
        if decision in ("robot_left_on_site", "robot_unaccounted") and not incident_code:
            raise Invalid(f"{decision} needs the incident it follows from")
        run = _run(run_code)
        incident = None
        if incident_code:
            try:
                incident = Incident.objects.get(code=incident_code)
            except Incident.DoesNotExist:
                raise Invalid(f"No incident {incident_code}") from None
        read_pattern = _pattern_id(run.robot_id)
        checkpoint("read:execution.record_disposition")
        lock_guards(
            share_patterns=[read_pattern] if read_pattern else [],
            share_robots=[run.robot_id],
            update_runs=[run.pk],
            label="execution.record_disposition",
        )
        if _pattern_id(run.robot_id) != read_pattern:
            raise RetryTransaction()
        if run_closed(run):
            raise Conflict(f"Run {run.code} is closed")
        if incident is not None:
            if incident.run_id != run.pk:
                raise Conflict(f"Incident {incident.code} is not an incident of run {run.code}")
            if decision == "robot_unaccounted" and incident.event_type != "robot_unaccounted":
                raise Conflict(
                    f"robot_unaccounted needs a robot_unaccounted incident; {incident.code} is not one"
                )
        if decision == "continue_visit":
            holds = blocking_holds(run.robot_id)
            if holds:
                raise Conflict(
                    "continue_visit refused: the robot or its configuration pattern has an unreleased "
                    f"blocking hold ({', '.join(holds)}). Nothing overrides this; release goes through the "
                    "hold-release workflow"
                )
            open_codes = open_interruptions(run)
            if open_codes:
                raise Conflict(
                    f"continue_visit refused: the run has an open interruption ({', '.join(open_codes)})"
                )
        d = RecoveryDisposition.objects.create(
            run=run, decision=decision, reason=reason, incident=incident, recorded_by=actor
        )
        return Outcome(
            "recovery_disposition",
            str(d.pk),
            {"run": run.code, "decision": decision, "incident": incident.code if incident else None},
        )

    return run_command(
        actor,
        "execution.record_disposition",
        permission="execution.record_disposition",
        payload=payload,
        handler=handler,
    )
