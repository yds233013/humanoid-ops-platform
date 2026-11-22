"""Change password (v2 01D): the page a person with a temporary password is sent to at first sign-in.

Works for every signed-in user (no role needed: an invited person has none yet). POST: one-time form token
(a replayed POST runs nothing), form validation, then accounts.services.change_own_password, then the session
is rotated (update_session_auth_hash) so the old session id stops working. No password is ever placed in a
message, URL or audit row.
"""

from typing import Any

from django import forms
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from accounts import services
from accounts.models import must_change_password
from platform_core.commands import CommandError
from platform_core.interaction import EXPIRED_TEXT, REPLAY_TEXT, UIForm, consume_form_token

TEMPLATE = "settings/password_change.html"


def _password(label: str, autocomplete: str, help_text: str = "") -> forms.CharField:
    return forms.CharField(
        label=label,
        strip=False,
        help_text=help_text,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": autocomplete}),
    )


class PasswordChangeForm(UIForm):
    current_password = _password(
        "Current password",
        "current-password",
        "The temporary password your admin gave you, or your current one.",
    )
    new_password1 = _password(
        "New password", "new-password", "At least 8 characters. Do not reuse the temporary password."
    )
    new_password2 = _password("New password again", "new-password")

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        one, two = data.get("new_password1"), data.get("new_password2")
        if one and two and one != two:
            self.add_error("new_password2", "The two new passwords do not match.")
        return data


@login_required
@require_http_methods(["GET", "HEAD", "POST"])
def password_change(request: HttpRequest) -> HttpResponse:
    forced = must_change_password(request.user)

    def page(form: forms.Form, status: int = 200) -> HttpResponse:
        ctx: dict[str, Any] = {"form": form, "forced": forced}
        if forced:
            ctx["roles"] = []  # the header shows no navigation: nothing else is reachable yet
        return render(request, TEMPLATE, ctx, status=status)

    if request.method != "POST":
        return page(PasswordChangeForm())
    spent = consume_form_token(request)
    if spent != "ok":
        messages.info(request, REPLAY_TEXT if spent == "used" else EXPIRED_TEXT)
        return redirect(request.path)
    form = PasswordChangeForm(request.POST)
    if not form.is_valid():
        return page(form, 400)
    try:
        services.change_own_password(
            request.user,
            current=form.cleaned_data["current_password"],
            new=form.cleaned_data["new_password1"],
        )
    except CommandError as exc:
        form.add_error(None, str(exc) + ". Nothing was changed.")
        return page(form, exc.status)
    update_session_auth_hash(request, request.user)  # type: ignore[arg-type]
    messages.success(request, "Password changed. Use your new password next time you sign in.")
    return redirect("/")
