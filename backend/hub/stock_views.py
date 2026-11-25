"""Hub > Stock (v2 02D): items with on hand / reserved / available, the minimum level and the below-minimum
flag, visits of the next two days that still need stock, active reservations, recent movements, and the forms
to receive, adjust, issue, return, consume and reserve stock for a visit. Every write is one guarded function
call (hub.stock); a refusal is shown in plain words ending "Nothing was changed". Stock is never the departure
equipment check."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django import forms
from django.contrib.auth.decorators import login_required
from django.db.models import Case, F, Subquery, Sum, When
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from accounts.permissions import active_roles
from hub import stock as st
from hub.charging_views import _allowed, _do, _valid
from hub.models import STOCK_KINDS, StockMovement, StockReservation
from planning.models import Visit
from platform_core.interaction import UIForm, command_page, denied

BACK = "/hub/stock/"
RECENT = 30
VISIT_WINDOW = timedelta(days=14)


class QuantityForm(forms.Form):
    item = forms.CharField(max_length=12)
    quantity = forms.IntegerField(min_value=1, max_value=100000)
    visit = forms.CharField(max_length=12, required=False)
    note = forms.CharField(max_length=200, required=False)


class AdjustForm(forms.Form):
    item = forms.CharField(max_length=12)
    delta = forms.IntegerField(min_value=-100000, max_value=100000)
    reason = forms.CharField(max_length=200)


class ReserveForm(forms.Form):
    item = forms.CharField(max_length=12)
    visit = forms.CharField(max_length=12)
    quantity = forms.IntegerField(min_value=1, max_value=100000)


def _visit_options() -> list[Visit]:
    """Visits stock can be reserved for, issued to or used for: upcoming only (not cancelled, not over, run
    not closed), up to two weeks ahead. The SQL functions refuse anything else in any case."""
    now = timezone.now()
    return list(
        Visit.objects.filter(
            current_revision__on_site_end__gt=now,
            current_revision__on_site_start__lt=now + VISIT_WINDOW,
        )
        .exclude(status="cancelled")
        .exclude(execution_run__closure__isnull=False)
        .select_related("current_revision", "site")
        .order_by("current_revision__on_site_start", "code")[:200]
    )


def _return_visits() -> list[Visit]:
    """Visits that have kits out with them now (issued - returned > 0), whatever their state: a kit that came
    back from a finished visit can always be recorded."""
    out = (
        StockMovement.objects.filter(kind__in=["issue", "return"], visit__isnull=False, item__kind="reusable")
        .values("visit")
        .annotate(n=Sum(Case(When(kind="issue", then="quantity"), default=-F("quantity"))))
        .filter(n__gt=0)
        .values("visit")
    )
    return list(
        Visit.objects.filter(pk__in=Subquery(out))
        .select_related("current_revision")
        .order_by("current_revision__on_site_start", "code")
    )


@login_required
@require_GET
def stock_page(request: HttpRequest) -> HttpResponse:
    roles = active_roles(request.user)
    if not _allowed(roles, "hub.view"):
        return denied(request, "hub.view")
    items = st.levels()
    needs = st.visit_needs()
    reservations = list(
        StockReservation.objects.filter(released_at__isnull=True)
        .select_related("item", "visit__current_revision")
        .order_by("visit__current_revision__on_site_start", "visit__code", "item__code")
    )
    movements = list(StockMovement.objects.select_related("item", "visit", "actor").order_by("-id")[:RECENT])
    return render(
        request,
        "hub/stock.html",
        {
            "items": items,
            "needs": needs,
            "short": sum(1 for n in needs if n.short),
            "below": sum(1 for i in items if i.below_minimum),
            "any_minimum": any(i.minimum is not None for i in items),
            "any_per_visit": any(i.per_visit is not None for i in items),
            "reservations": reservations,
            "cancelled_rows": sum(1 for r in reservations if r.visit.status == "cancelled"),
            "movements": movements,
            "visits": _visit_options(),
            "return_visits": _return_visits(),
            "reusable": [i for i in items if i.reusable],
            "consumable": [i for i in items if not i.reusable],
            "can_move": _allowed(roles, st.MOVE),
            "can_adjust": _allowed(roles, st.ADJUST),
            "can_reserve": _allowed(roles, st.RESERVE),
            "can_manage": _allowed(roles, st.MANAGE_ITEMS),
            "now": timezone.now(),
        },
    )


def _quantity_post(request: HttpRequest, perm: str, fn: Any, ok: str) -> HttpResponse:
    def go() -> None:
        d = _valid(QuantityForm(request.POST))
        fn(request.user, d)

    return _do(request, perm, go, ok, back=BACK)


@login_required
@require_POST
def receive_view(request: HttpRequest) -> HttpResponse:
    return _quantity_post(
        request, st.MOVE,
        lambda u, d: st.receive(u, item_code=d["item"], quantity=d["quantity"], note=d["note"]),
        "Stock received and added to the ledger.",
    )  # fmt: skip


@login_required
@require_POST
def issue_view(request: HttpRequest) -> HttpResponse:
    return _quantity_post(
        request, st.MOVE,
        lambda u, d: st.issue(u, item_code=d["item"], quantity=d["quantity"], visit_code=d["visit"] or None),
        "Stock issued. The departure equipment check is still required.",
    )  # fmt: skip


@login_required
@require_POST
def return_view(request: HttpRequest) -> HttpResponse:
    return _quantity_post(
        request, st.MOVE,
        lambda u, d: st.return_stock(u, item_code=d["item"], quantity=d["quantity"],
                                     visit_code=d["visit"] or None),
        "Return recorded.",
    )  # fmt: skip


@login_required
@require_POST
def consume_view(request: HttpRequest) -> HttpResponse:
    return _quantity_post(
        request, st.MOVE,
        lambda u, d: st.consume(u, item_code=d["item"], quantity=d["quantity"],
                                visit_code=d["visit"] or None),
        "Use recorded.",
    )  # fmt: skip


@login_required
@require_POST
def adjust_view(request: HttpRequest) -> HttpResponse:
    def go() -> None:
        d = _valid(AdjustForm(request.POST))
        st.adjust(request.user, item_code=d["item"], delta=d["delta"], reason=d["reason"])

    return _do(request, st.ADJUST, go, "Adjustment recorded with its reason.", back=BACK)


@login_required
@require_POST
def reserve_view(request: HttpRequest) -> HttpResponse:
    def go() -> None:
        d = _valid(ReserveForm(request.POST))
        st.reserve(request.user, item_code=d["item"], visit_code=d["visit"], quantity=d["quantity"])

    return _do(
        request, st.RESERVE, go,
        "Stock reserved for the visit. The departure equipment check is still required.", back=BACK,
    )  # fmt: skip


@login_required
@require_POST
def release_cancelled_view(request: HttpRequest) -> HttpResponse:
    """02E: one click on a "Visit cancelled: release this" row (hub_release_stock, reason filled in)."""

    def go() -> None:
        try:
            rid = int(request.POST.get("reservation", ""))
        except ValueError:
            raise forms.ValidationError("reservation: choose a reservation") from None
        st.release_for_cancelled_visit(request.user, reservation_id=rid)

    return _do(request, st.RESERVE, go, "Reservation released: its visit was cancelled.", back=BACK)


class ReleaseForm(UIForm):
    reason = forms.CharField(max_length=200, label="Reason", help_text="Kept in the audit log.")


@login_required
def release_view(request: HttpRequest, reservation_id: int) -> HttpResponse:
    r = get_object_or_404(StockReservation.objects.select_related("item", "visit"), pk=reservation_id)

    def call(actor: Any, d: dict[str, Any]) -> dict[str, Any]:
        st.release(actor, reservation_id=r.pk, reason=d["reason"])
        return {"entity_id": str(r.pk)}

    return command_page(
        request,
        permission=st.RESERVE,
        form_class=ReleaseForm,
        template="hub/stock_release.html",
        call=call,
        ok=f"Reservation of {r.item.code} for {r.visit.code} released.",
        success_url=BACK,
        context={"r": r},
    )


class ItemForm(UIForm):
    code = forms.CharField(max_length=12, label="Item code", help_text="Short and unique, for example RK.")
    name = forms.CharField(max_length=80, label="Name", help_text="For example Restraint kit.")
    kind = forms.ChoiceField(label="Kind", choices=STOCK_KINDS)
    unit = forms.CharField(max_length=20, label="Counted in", initial="kits", help_text="For example kits.")


@login_required
def items_page(request: HttpRequest) -> HttpResponse:
    def call(actor: Any, d: dict[str, Any]) -> dict[str, Any]:
        st.create_item(actor, code=d["code"], name=d["name"], kind=d["kind"], unit=d["unit"])
        return {"entity_id": d["code"].strip().upper()}

    return command_page(
        request,
        permission=st.MANAGE_ITEMS,
        form_class=ItemForm,
        template="hub/stock_items.html",
        call=call,
        ok=lambda r: f"Item {r['entity_id']} added with nothing in stock. Record what arrives as received.",
        success_url="/hub/stock/items/",
        context={"items": st.levels()},
    )
