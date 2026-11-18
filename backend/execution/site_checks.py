"""On-site check before (and between) sessions (Slice 4, Engineer B). Contract section 2 (SiteCheck) and C4.

SYNTHETIC data only: the demo template's items are placeholders, not a real procedure (B-05 is unresolved).

record_site_check records what the crew observed. The result is never chosen by the caller: consent_count and
go / no_go are computed here, under the locks, exactly as the insert trigger recomputes them (the trigger
refuses any mismatch). A no_go is recorded, not refused, and comes back with every reason in plain words.

Locks (after run_command's role-grant S): settings S (advisory, demo_mode), site S, robot S, persons S
(verifier), visit S, run U. The settings lock is taken first and demo_mode is read only after it is held, so a
demo -> production switch either waits for this check to commit or is seen by it.
"""

import json
from typing import Any

from django.db import connection

from execution.custody import current_custody
from execution.logistics import (
    crew_ids,
    custody_text,
    get_person,
    get_run,
    latest_event,
    require_open,
    trip_kinds,
)
from execution.models import SiteCheck, SiteCheckTemplate
from fleet import services as fleet_services
from planning.models import Site
from platform_core.commands import Conflict, Invalid, Outcome, run_command
from platform_core.locks import lock_guards
from platform_core.testhooks import checkpoint

CONDITION_VALUES = ("ok", "not_ok")


def _template(version: str) -> SiteCheckTemplate:
    try:
        return SiteCheckTemplate.objects.get(version=version)
    except SiteCheckTemplate.DoesNotExist:
        raise Invalid(f"No site check template {version}") from None


def _validate(template: SiteCheckTemplate, adults_present: Any, conditions: Any) -> None:
    if isinstance(adults_present, bool) or not isinstance(adults_present, int) or adults_present < 0:
        raise Invalid("adults_present must be a whole number, 0 or more (count the adults present)")
    if not isinstance(conditions, dict):
        raise Invalid("conditions must map each template item id to 'ok' or 'not_ok'")
    ids = [i["id"] for i in template.items]
    missing = [i for i in ids if i not in conditions]
    extra = sorted(k for k in conditions if k not in ids)
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing {missing}")
        if extra:
            parts.append(f"not in template {template.version}: {extra}")
        raise Invalid("conditions must answer exactly the template's items (" + "; ".join(parts) + ")")
    bad = sorted(k for k, v in conditions.items() if v not in CONDITION_VALUES)
    if bad:
        raise Invalid(f"each condition must be 'ok' or 'not_ok' (check {bad})")


def _sql_one(sql: str, params: list[Any]) -> Any:
    with connection.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row[0] if row else None


def record_site_check(
    actor: Any,
    *,
    run_code: str,
    template_version: str,
    adults_present: int,
    conditions: dict[str, str],
    verifier_code: str,
) -> dict[str, Any]:
    name = "execution.record_site_check"
    payload = {
        "run": run_code,
        "template": template_version,
        "adults_present": adults_present,
        "conditions": conditions,
        "verifier": verifier_code,
    }

    def handler() -> Outcome:
        template = _template(template_version)
        _validate(template, adults_present, conditions)
        run = get_run(run_code)
        verifier = get_person(verifier_code, "verifier")
        site_id = run.visit.site_id
        checkpoint(f"read:{name}")
        lock_guards(
            share_settings=True,
            share_sites=[site_id],
            share_robots=[run.robot_id],
            share_persons=[verifier.pk],
            share_visits=[run.visit_id],
            update_runs=[run.pk],
            label=name,
        )

        require_open(run, "site checks")
        if verifier.pk not in crew_ids(run):
            raise Conflict(f"The site check verifier {verifier.code} must be field crew of run {run.code}")

        demo = fleet_services.demo_mode()  # read only after the settings lock is held
        consents = int(_sql_one("SELECT execution_valid_consents(%s)", [run.pk]))
        reasons: list[str] = []
        if "arrived_site" not in trip_kinds(run):
            reasons.append("the trip has not arrived at the site")
        custody = current_custody(run.robot_id)
        ev = latest_event(run.robot_id)
        if custody.kind != "site" or ev is None or ev.visit_id != run.visit_id:
            reasons.append(f"the robot is not in site custody for this visit (it is {custody_text(custody)})")
        elif not custody.verified:
            reasons.append("site custody is not verified")
        labels = {i["id"]: i["label"] for i in template.items}
        for item_id in labels:
            if conditions[item_id] != "ok":
                reasons.append(f"condition not ok: {labels[item_id]} ({item_id})")
        expected = Site.objects.get(pk=site_id).expected_adults
        if expected is None:
            reasons.append("the expected number of adults at the site is unknown")
        if adults_present > consents:
            reasons.append(
                f"{adults_present} adults present but only {consents} valid consents cover this visit"
            )
        if template.source != "approved_procedure" and not demo:
            reasons.append("a synthetic_demo template cannot produce go outside demo mode")

        # The database's own definition (the trigger uses it): never record a result it would refuse.
        problem = _sql_one(
            "SELECT execution_site_check_no_go(%s::bigint, %s::bigint, %s::jsonb, %s::int, %s::int)",
            [run.pk, template.pk, json.dumps(conditions), adults_present, consents],
        )
        if (problem is None) != (not reasons):
            raise Conflict(
                "Site check result could not be computed consistently; nothing was recorded "
                f"(database: {problem or 'go'}; checks: {'; '.join(reasons) or 'go'})"
            )
        result = "go" if not reasons else "no_go"

        sc = SiteCheck.objects.create(
            run=run,
            template=template,
            adults_present=adults_present,
            conditions=conditions,
            consent_count=consents,
            result=result,
            verifier=verifier,
            recorded_by=actor,
        )
        return Outcome(
            "site_check",
            str(sc.pk),
            {
                "id": sc.pk,
                "run": run.code,
                "template": template.version,
                "template_source": template.source,
                "mode": "demo" if demo else "production",
                "adults_present": adults_present,
                "expected_adults": expected,
                "consent_count": consents,
                "result": result,
                "reasons": reasons,
                "verifier": verifier.code,
            },
        )

    return run_command(
        actor, name, permission="execution.record_field_event", payload=payload, handler=handler
    )
