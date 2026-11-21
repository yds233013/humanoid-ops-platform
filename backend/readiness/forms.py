"""Forms for the readiness screens (01C.5). They only collect input; the guarded commands decide."""

from __future__ import annotations

from typing import Any

from django import forms
from django.contrib.auth import get_user_model

from accounts.permissions import active_roles
from accounts.roles import INSPECTION_SIGNER_ROLES
from fleet.models import InspectionTemplate
from platform_core.interaction import UIForm

ANSWER_CHOICES = [("", "Not checked yet"), ("pass", "Pass"), ("fail", "Fail")]


def signer_choices() -> list[tuple[str, str]]:
    """People who could sign an inspection. Someone who is not a qualified inspector is still listed (and
    marked) so that the refusal is plain and server-side, never a silently shorter list."""
    out: list[tuple[str, str]] = [("", "Nobody yet")]
    for u in get_user_model().objects.filter(is_active=True).order_by("username")[:200]:
        roles = active_roles(u)
        if not roles:
            continue
        qualified = bool(roles & INSPECTION_SIGNER_ROLES)
        out.append(
            (u.get_username(), u.get_username() + ("" if qualified else " (not a qualified inspector)"))
        )
    return out


class InspectionForm(UIForm):
    """One answer per checklist item of the chosen template, plus who did and who checked it."""

    template_version = forms.CharField(widget=forms.HiddenInput)
    performer = forms.ChoiceField(
        required=False,
        label="Done by",
        help_text=(
            "Both people must be qualified inspectors for a passing result; "
            "with fewer it is recorded as incomplete."
        ),
    )
    verifier = forms.ChoiceField(required=False, label="Checked by (a different person)")

    def __init__(self, *args: Any, template: InspectionTemplate | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.item_ids: list[str] = []
        items = list(template.items) if template is not None else []
        for item in items:
            name = f"item_{item['id']}"
            self.item_ids.append(item["id"])
            self.fields[name] = forms.ChoiceField(
                required=False,
                choices=ANSWER_CHOICES,
                label=str(item["label"]) + (" (robot condition)" if item.get("robot_condition") else ""),
            )
        choices = signer_choices()
        for name in ("performer", "verifier"):
            field = self.fields[name]
            assert isinstance(field, forms.ChoiceField)
            field.choices = choices
        # item fields first, then people (the declared order puts the hidden version and people first)
        order = ["template_version", *[f"item_{i}" for i in self.item_ids], "performer", "verifier"]
        self.order_fields(order)
