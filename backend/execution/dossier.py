"""Visit dossier (Slice 4, integration lead; contract section 7.10, R-17).

Rebuilds the whole history of one visit from the append-only rows of Slices 2-4, in time order, each entry
carrying the plan revision and robot configuration manifest that applied when it was recorded. Read-only: it
writes nothing and derives nothing that is not already stored. History is never rewritten, so the dossier of a
visit is stable: re-running it later yields the same entries plus anything recorded since.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from execution.models import (
    CustodyEvent,
    ExecutionRun,
    Incident,
)
from fleet.models import Inspection
from planning.models import Visit


@dataclass(frozen=True)
class DossierEntry:
    at: datetime
    kind: str  # stable machine label, e.g. "custody", "incident", "severity"
    text: str  # plain words for people
    revision_no: int | None
    manifest_id: int | None
    ref: str = ""  # a code or id to look the row up

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "kind": self.kind,
            "text": self.text,
            "revision_no": self.revision_no,
            "manifest_id": self.manifest_id,
            "ref": self.ref,
        }


def _who(user: Any) -> str:
    return getattr(user, "username", "") or "?"


def visit_dossier(visit: Visit) -> list[DossierEntry]:
    out: list[DossierEntry] = []

    def add(at: datetime, kind: str, text: str, rev: int | None, manifest: int | None, ref: str = "") -> None:
        out.append(DossierEntry(at, kind, text, rev, manifest, ref))

    # ---- Slice 2: planning revisions
    for r in visit.revisions.select_related("created_by").order_by("revision_no"):
        add(
            r.created_at,
            "plan_revision",
            f"Plan revision r{r.revision_no} ({r.feasibility}) by {_who(r.created_by)}"
            + (f": {r.reason}" if r.reason else ""),
            r.revision_no,
            None,
            f"r{r.revision_no}",
        )

    # ---- Slice 3: readiness evidence, recovery plans, authorizations, departure
    for ev in visit.readiness_evidence.select_related("revision", "recorded_by").order_by("recorded_at"):
        add(
            ev.recorded_at,
            "evidence",
            f"Evidence {ev.code}: {ev.kind} {ev.result} by {_who(ev.recorded_by)}",
            ev.revision.revision_no,
            ev.manifest_id,
            ev.code,
        )
    for rp in visit.recovery_plans.select_related("revision", "recorded_by").order_by("recorded_at"):
        add(
            rp.recorded_at,
            "recovery_plan",
            f"Recovery plan recorded by {_who(rp.recorded_by)}",
            rp.revision.revision_no,
            rp.manifest_id,
            str(rp.pk),
        )
    for a in visit.authorizations.select_related("revision", "issued_by").order_by("issued_at"):
        add(
            a.issued_at,
            "authorization",
            f"Departure authorization {a.code} ({a.mode}) issued by {_who(a.issued_by)}, "
            f"expires {a.expires_at.isoformat()}",
            a.revision.revision_no,
            a.manifest_id,
            a.code,
        )
        rev = getattr(a, "revocation", None)
        if rev is not None:
            add(
                rev.revoked_at,
                "authorization_revoked",
                f"Authorization {a.code} revoked by {_who(rev.revoked_by)}: {rev.reason}",
                a.revision.revision_no,
                a.manifest_id,
                a.code,
            )
    for d in visit.departures.select_related("authorization__revision", "recorded_by"):
        add(
            d.departed_at,
            "departure",
            f"Departure recorded on {d.authorization.code} by {_who(d.recorded_by)}",
            d.authorization.revision.revision_no,
            d.authorization.manifest_id,
            d.authorization.code,
        )

    # ---- inspections that name this visit (pre- and post-visit)
    for i in Inspection.objects.filter(visit_ref=visit.code).order_by("submitted_at"):
        add(
            i.submitted_at,
            "inspection",
            f"Inspection {i.code}: {i.result}"
            + (f" (robot hold {i.hold.code})" if i.hold is not None else ""),
            None,
            i.manifest_id,
            i.code,
        )

    # ---- Slice 4: execution
    run = ExecutionRun.objects.filter(visit=visit).select_related("revision").first()
    if run is not None:
        rno, mid = run.revision.revision_no, run.manifest_id
        add(
            run.recorded_at, "run_opened", f"Execution run {run.code} opened at departure", rno, mid, run.code
        )
        for c in (
            CustodyEvent.objects.filter(run=run).select_related("custodian", "received_by").order_by("seq")
        ):
            ver = getattr(c, "verification", None)
            basis = (
                "administrative assignment, not verified"
                if c.basis == "administrative" and ver is None
                else c.basis
            )
            add(
                c.recorded_at,
                "custody",
                f"Custody #{c.seq}: {c.from_kind} -> {c.to_kind} ({basis})"
                + (f", custodian {c.custodian.code}" if c.custodian is not None else "")
                + (f", received by {c.received_by.code}" if c.received_by is not None else ""),
                rno,
                mid,
                str(c.seq),
            )
            if ver is not None:
                add(
                    ver.recorded_at,
                    "custody_verified",
                    f"Custody #{c.seq} verified by {ver.verified_by.code}",
                    rno,
                    mid,
                    str(c.seq),
                )
        for t in run.trip_events.order_by("seq"):
            add(t.recorded_at, "trip", f"Trip: {t.kind.replace('_', ' ')}", rno, mid)
        for sc in run.site_checks.order_by("recorded_at"):
            add(
                sc.recorded_at,
                "site_check",
                f"Site check: {sc.result} (adults present {sc.adults_present}, valid consents "
                f"{sc.consent_count})",
                rno,
                mid,
                str(sc.pk),
            )
        for s in run.session_events.order_by("seq"):
            add(s.recorded_at, "session", f"Session {s.kind}" + (f": {s.note}" if s.note else ""), rno, mid)
        for it in run.interruptions.order_by("recorded_at"):
            add(
                it.recorded_at, "interruption", f"Interruption {it.code} ({it.source}): {it.reason}", rno, mid
            )
            res = getattr(it, "resolution", None)
            if res is not None:
                add(res.recorded_at, "interruption_resolved", f"Interruption {it.code} resolved", rno, mid)
        for disp in run.dispositions.order_by("recorded_at"):
            add(
                disp.recorded_at,
                "disposition",
                f"Recovery disposition: {disp.decision} - {disp.reason}",
                rno,
                mid,
            )
        for rc in run.reconciliations.order_by("recorded_at"):
            add(
                rc.recorded_at, "reconciliation", f"Return reconciliation: {rc.outcome}", rno, mid, str(rc.pk)
            )
        closure = getattr(run, "closure", None)
        if closure is not None:
            add(closure.recorded_at, "trip_closed", "Trip closed after reconciliation", rno, mid)

    # ---- incidents tied to this visit (each carries its own manifest at report time)
    for inc in Incident.objects.filter(visit=visit).select_related("hold").order_by("reported_at"):
        inc_rev = run.revision.revision_no if run is not None else None
        add(
            inc.reported_at,
            "incident",
            f"Incident {inc.code}: {inc.event_type} (occurred {inc.occurred_at.isoformat()}, "
            f"{inc.knowledge_source}); robot hold {inc.hold.code}",
            inc_rev,
            inc.manifest_id,
            inc.code,
        )
        for sd in inc.severity_decisions.order_by("recorded_at"):
            add(
                sd.recorded_at,
                "severity",
                f"{inc.code} severity {sd.level}: {sd.reason}",
                rno,
                inc.manifest_id,
            )
        for st in inc.status_changes.order_by("recorded_at"):
            add(st.recorded_at, "incident_status", f"{inc.code} -> {st.to_status}", inc_rev, inc.manifest_id)
        for e in inc.escalations.order_by("recorded_at"):
            add(e.recorded_at, "escalation", f"{inc.code} escalated to {e.to_role}", inc_rev, inc.manifest_id)
            ack = getattr(e, "ack", None)
            if ack is not None:
                add(
                    ack.recorded_at,
                    "escalation_ack",
                    f"{inc.code} escalation acknowledged",
                    rno,
                    inc.manifest_id,
                )
        if inc.hold.released_at is not None:
            add(
                inc.hold.released_at,
                "hold_released",
                f"Robot hold {inc.hold.code} released (fleet hold-release workflow)",
                rno,
                inc.manifest_id,
            )

    out.sort(key=lambda e: e.at)
    return out
