"""Visit dossier (contract 7.10 / R-17): every Slice 2-4 row of a visit in time order, each with the revision
and manifest that applied; stable when rebuilt later."""

import pytest

from execution.dossier import visit_dossier
from execution.models import CustodyEvent
from planning.models import Visit
from tests.execution_world import build_execution, close_run, go_check, to_hub, to_site

pytestmark = pytest.mark.django_db(transaction=True)


def test_dossier_rebuilds_the_full_visit_in_time_order(app_role_conn):
    w = build_execution(prefix="dos_")
    visit = Visit.objects.get(code=w.visit)
    with app_role_conn.cursor() as cur:
        to_site(cur, w)
        go_check(cur, w)
        to_hub(cur, w)
        close_run(cur, w)
    entries = visit_dossier(visit)
    kinds = [e.kind for e in entries]
    for k in (
        "plan_revision",
        "evidence",
        "recovery_plan",
        "authorization",
        "departure",
        "run_opened",
        "custody",
        "custody_verified",
        "trip",
        "site_check",
        "inspection",
        "reconciliation",
        "trip_closed",
    ):
        assert k in kinds, (k, kinds)
    assert [e.at for e in entries] == sorted(e.at for e in entries)
    assert kinds.index("authorization") < kinds.index("departure") < kinds.index("trip_closed")
    first_custody = next(e for e in entries if e.kind == "custody")
    assert "administrative assignment, not verified" not in first_custody.text  # it was verified later
    assert "administrative" in first_custody.text
    run_rows = [e for e in entries if e.kind in ("custody", "trip", "site_check", "reconciliation")]
    assert {e.manifest_id for e in run_rows} == {w.run.manifest_id}
    assert {e.revision_no for e in run_rows} == {w.run.revision.revision_no}
    # stable: rebuilding later gives the same entries
    assert [e.as_dict() for e in visit_dossier(visit)] == [e.as_dict() for e in entries]


def test_unverified_departure_assignment_is_labelled_as_such():
    w = build_execution(prefix="dos2_")
    entries = visit_dossier(Visit.objects.get(code=w.visit))
    custody = [e for e in entries if e.kind == "custody"]
    assert len(custody) == CustodyEvent.objects.filter(run=w.run).count() == 1
    assert "administrative assignment, not verified" in custody[0].text
