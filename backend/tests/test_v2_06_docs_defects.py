"""v2 Phase 06 walkthrough defects (docs/evidence/06_responsive_review.md, "Defects found").

D-06-1 (low): the visit dossier printed raw UTC ISO times in two lines people read ("expires
2026-11-27T01:01:38.261952+00:00", "occurred 2026-...+00:00"). The design rule is local time with the zone, never
UTC where a person reads. Both now use the same local format as every other page ("Fri 27 Nov, 01:01 PST").

SYNTHETIC data only.
"""

from __future__ import annotations

import re

import pytest

from execution.dossier import visit_dossier
from planning.models import Visit
from platform_core.templatetags.ui import local_dt
from tests.execution_world import build_execution, raw_incident

pytestmark = pytest.mark.django_db(transaction=True)

ISO_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def test_dossier_lines_show_local_times_with_the_zone_never_raw_utc(app_role_conn):
    w = build_execution(prefix="d6_")
    raw_incident(app_role_conn, w)
    visit = Visit.objects.get(code=w.visit)
    entries = visit_dossier(visit)
    auth = next(e for e in entries if e.kind == "authorization")
    incident = next(e for e in entries if e.kind == "incident")
    expires = visit.authorizations.get().expires_at
    assert f"expires {local_dt(expires)}" in auth.text
    assert "(occurred " in incident.text
    for e in entries:
        assert not ISO_UTC.search(e.text), e.text
        assert "+00:00" not in e.text, e.text
