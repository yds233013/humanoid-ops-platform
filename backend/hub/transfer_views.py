"""Hub > Transfers (v2 03A-2): robots and vehicles on their way between hubs, and stock sent between hubs.

Every write is one command (fleet.transfers / hub.stock), whose database function checks the actor's
grant at the hub of the locked record; a refusal is shown in plain words ending "Nothing was changed".
The pages show only the viewer's hubs (hub_scope)."""

from __future__ import annotations

from typing import Any

from django import forms
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from fleet import transfers as tr
from fleet.location import robot_location
from hub import stock as st
from hub.charging_views import _allowed, _do, _valid
from platform_core.hubscope import hub_scope
from platform_core.interaction import denied
from platform_core.models import Hub

BACK = "/hub/transfers/"


class StartForm(forms.Form):
    kind = forms.ChoiceField(choices=[("robot", "Robot"), ("vehicle", "Vehicle")])
    code = forms.CharField(max_length=12)
    to_hub = forms.CharField(max_length=12)
    reason = forms.CharField(max_length=200)


class ReasonForm(forms.Form):
    reason = forms.CharField(max_length=200)


class StockTransferForm(forms.Form):
    item = forms.CharField(max_length=30)  # CODE@HUB: codes are unique per hub
    quantity = forms.IntegerField(min_value=1, max_value=100000)
    to_hub = forms.CharField(max_length=12)


def _back(request: HttpRequest) -> str:
    """Where to return after a form: a local page the form named, else the transfers list."""
    back = str(request.POST.get("back") or "")
    return back if back.startswith("/") and not back.startswith("//") else BACK


def robot_panel(user: Any, robot: Any) -> dict[str, Any]:
    """The robot page's "Location and hub" block (fleet/_transfer_panel.html): where it is, its transfers and
    the form. A few statements, whatever the fleet size."""
    from accounts.permissions import has_permission

    rows = tr.transfers(asset_kind="robot", asset_id=robot.pk, limit=5)
    return {
        "location": robot_location(robot.pk),
        "transfers": rows,
        "open": next((r for r in rows if r.open), None),
        "hubs": list(Hub.objects.exclude(pk=robot.hub_id).order_by("code")),
        "may_start": has_permission(user, "fleet.transfer", hub=robot.hub_id),
    }


@login_required
@require_GET
def transfers_page(request: HttpRequest) -> HttpResponse:
    scope = hub_scope(request)
    if not _allowed(scope.roles, "hub.view"):
        return denied(request, "hub.view")
    rows = tr.transfers(scope.ids)
    return render(
        request,
        "hub/transfers.html",
        {
            "rows": rows,
            "open_rows": [r for r in rows if r.open],
            "done_rows": [r for r in rows if not r.open][:30],
            "hubs": list(Hub.objects.order_by("code")),
            "can_transfer": _allowed(scope.roles, "fleet.transfer"),
            "can_abandon": _allowed(scope.roles, "fleet.abandon_transfer"),
            "prefill": {"kind": request.GET.get("kind", "robot"), "code": request.GET.get("code", "")},
            "scope": scope,
        },
    )


@login_required
@require_POST
def start_view(request: HttpRequest) -> HttpResponse:
    def go() -> None:
        d = _valid(StartForm(request.POST))
        tr.start_transfer(
            request.user, kind=d["kind"], code=d["code"], to_hub_code=d["to_hub"], reason=d["reason"]
        )

    return _do(
        request, "fleet.transfer", go,
        "On its way. It can be planned at neither hub until it is received at the other hub.",
        back=_back(request),
    )  # fmt: skip


@login_required
@require_POST
def action_view(request: HttpRequest, transfer_id: int, action: str) -> HttpResponse:
    perm = "fleet.abandon_transfer" if action == "abandon" else "fleet.transfer"

    def go() -> None:
        if action == "receive":
            tr.receive_transfer(request.user, transfer_id=transfer_id)
        elif action == "refuse":
            tr.refuse_transfer(
                request.user, transfer_id=transfer_id, reason=_valid(ReasonForm(request.POST))["reason"]
            )
        elif action == "abandon":
            tr.abandon_transfer(
                request.user, transfer_id=transfer_id, reason=_valid(ReasonForm(request.POST))["reason"]
            )
        else:
            raise forms.ValidationError("Unknown action")

    ok = {
        "receive": "Received. It is now verified at this hub, which is its home hub. Holds were not changed.",
        "refuse": (
            "Receipt refused. It stays on its way until it is received or the ops lead abandons the transfer."
        ),
        "abandon": "Transfer abandoned. It must now be received back at the hub it came from.",
    }.get(action, "")
    return _do(request, perm, go, ok, back=_back(request))


@login_required
@require_POST
def stock_transfer_view(request: HttpRequest) -> HttpResponse:
    def go() -> None:
        d = _valid(StockTransferForm(request.POST))
        code, _, hub_code = d["item"].partition("@")
        st.transfer(request.user, item_code=code, quantity=d["quantity"], to_hub_code=d["to_hub"],
                    hub_code=hub_code or None)  # fmt: skip

    return _do(
        request, st.MOVE, go, "Stock sent. It counts at neither hub until it is received there.",
        back="/hub/stock/",
    )  # fmt: skip


@login_required
@require_POST
def stock_receive_view(request: HttpRequest, transfer_id: int) -> HttpResponse:
    return _do(
        request, st.MOVE, lambda: st.receive_transfer(request.user, transfer_id=transfer_id),
        "Stock received from the other hub and added to this hub's ledger.", back="/hub/stock/",
    )  # fmt: skip
