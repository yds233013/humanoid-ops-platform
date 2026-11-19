"""Read-only /ops/capacity/ page (Slice 6 contract docs/06 sections 1 and 9; D5 approved).

GET only, permission `operations.metrics`, built inside ONE `operations.snapshot` snapshot via the operations
`ops_view` decorator (which wraps `snapshot_view`: login and permission checked inside the snapshot,
outside ATOMIC_REQUESTS, fail closed with 503 if the snapshot cannot be the outermost transaction). The page
shows the same content as the founder report (capacity.report). Everything is SYNTHETIC: a forecast from
stated assumptions, not a company operating plan.
"""

from datetime import datetime, timedelta

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from accounts.permissions import active_roles
from accounts.roles import PERMISSIONS
from capacity import report as R
from capacity.inputs import ASSUMPTIONS
from fleet.services import demo_mode
from operations.views import as_of_text, iso, ops_view


def _base(request: HttpRequest, at: datetime, assumption: str) -> dict[str, object]:
    roles = active_roles(request.user)
    return {
        "roles": sorted(roles),
        "demo_mode": demo_mode(),
        "banner": R.BANNER,
        "can_metrics": bool(roles & PERMISSIONS["operations.metrics"]),
        "as_of_text": as_of_text(at),
        "as_of_iso": iso(at),
        "assumptions": ASSUMPTIONS,
        "assumption": assumption,
    }


@ops_view("operations.metrics")
def capacity_page(request: HttpRequest, at: datetime) -> HttpResponse:
    assumption = request.GET.get("assumption", "base")
    if assumption not in ASSUMPTIONS:
        ctx = {
            **_base(request, at, "base"),
            "error": f"assumption must be one of {', '.join(ASSUMPTIONS)}",
        }
        return render(request, "capacity/report.html", ctx, status=400)
    start = at - timedelta(days=R.BASELINE_WINDOW_DAYS)
    values, baseline = R.load_baseline(assumption, at, start=start, end=at)  # type: ignore[arg-type]
    report = R.founder_report(assumption, values=values, baseline=baseline)
    ctx = {
        **_base(request, at, assumption),
        "report": report,
        "comparison": R.comparison_table(report),
        "constraint_words": R.CONSTRAINT_WORDS,
    }
    return render(request, "capacity/report.html", ctx)
