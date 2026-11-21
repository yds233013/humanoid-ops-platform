"""Shared interaction helpers for forms that change state (v2 01C.6 foundation).

* One-time form tokens (double-submit protection): ``{% form_token %}`` in the template,
  ``consume_form_token`` on POST. A replayed POST is refused with a friendly message and nothing runs.
* ``command_page``: the one way a 01C form page works. GET shows the form; POST checks the role, consumes the
  token, validates the input, then calls exactly ONE existing service command. A refusal is shown in plain
  words, naming the role that can do it.
* ``who_can`` / ``refusal_text`` / ``waiting_text``: plain-language role wording from the permission table.
* ``UIForm``: Django form base class that wires help text and errors for screen readers.

No JavaScript is needed anywhere. Nothing here approves anything.
"""

from __future__ import annotations

import functools
import secrets
from collections.abc import Callable, Iterable
from datetime import timedelta
from typing import Any

from django import forms
from django.contrib import messages
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.utils import timezone

from accounts.permissions import has_permission
from accounts.roles import ALL_ROLES, PERMISSIONS
from platform_core.commands import CommandError, PermissionDenied
from platform_core.models import FormToken

TOKEN_FIELD = "_form_token"
TOKEN_TTL = timedelta(hours=12)

REPLAY_TEXT = (
    "That form was already sent, so nothing was done a second time. Check the list to see the result."
)
EXPIRED_TEXT = "That form had expired, so nothing was done. Please fill it in again."

ROLE_WORDS = {
    "admin": "the workspace admin",
    "ops_lead": "the ops lead",
    "field_operator": "the field operator",
    "engineer": "the engineer",
    "engineering_lead": "the engineering lead",
    "safety_reviewer": "the safety reviewer",
    "founder": "the founder",
}


# ------------------------------------------------------------------ role wording
def _perm_list(permission: str | Iterable[str]) -> list[str]:
    return [permission] if isinstance(permission, str) else list(permission)


def role_names(permission: str | Iterable[str]) -> list[str]:
    roles: set[str] = set()
    for perm in _perm_list(permission):
        roles |= PERMISSIONS.get(perm, set())
    return [r for r in ALL_ROLES if r in roles]


def who_can(permission: str | Iterable[str]) -> str:
    """``the ops lead or the engineering lead`` for the roles that hold the permission."""
    words = [ROLE_WORDS.get(r, r.replace("_", " ")) for r in role_names(permission)]
    if not words:
        return "nobody yet"
    if len(words) == 1:
        return words[0]
    return ", ".join(words[:-1]) + " or " + words[-1]


def refusal_text(permission: str | Iterable[str]) -> str:
    """``Only the safety reviewer can do this.``"""
    return f"Only {who_can(permission)} can do this."


def waiting_text(permission: str | Iterable[str], what: str = "approval") -> str:
    """``Waiting for the founder's approval`` (one role) or ``Waiting for approval from A or B``."""
    names = role_names(permission)
    if len(names) == 1:
        return f"Waiting for {ROLE_WORDS.get(names[0], names[0])}'s {what}"
    return f"Waiting for {what} from {who_can(permission)}"


def explain(exc: CommandError, permission: str | Iterable[str] | None = None) -> str:
    """The sentence shown for a refused command. A permission refusal names the role that can act."""
    if isinstance(exc, PermissionDenied) and permission is not None:
        return refusal_text(permission)
    text = str(exc)
    if "duplicate key value" in text or "violates unique constraint" in text:
        # two people saved the same thing at the same moment; the database kept one
        return "That already exists: someone saved the same code or serial a moment ago."
    return text


# ------------------------------------------------------------------ one-time form tokens
def issue_form_token(request: HttpRequest) -> str:
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return ""
    token = secrets.token_urlsafe(32)
    FormToken.objects.create(token=token, user=user)
    if secrets.randbelow(25) == 0:  # opportunistic clean-up of old rows
        FormToken.objects.filter(created_at__lt=timezone.now() - 2 * TOKEN_TTL).delete()
    return token


def consume_form_token(request: HttpRequest) -> str:
    """Spend the token posted with the form. Returns ``ok``, ``used`` (a replay) or ``invalid``.

    One atomic UPDATE decides the winner, so two simultaneous submits cannot both pass."""
    token = request.POST.get(TOKEN_FIELD, "")
    if not token:
        return "invalid"
    now = timezone.now()
    user: Any = request.user
    spent = FormToken.objects.filter(
        token=token, user=user, consumed_at__isnull=True, created_at__gte=now - TOKEN_TTL
    ).update(consumed_at=now)
    if spent == 1:
        return "ok"
    if FormToken.objects.filter(token=token, user=user, consumed_at__isnull=False).exists():
        return "used"
    return "invalid"


def spend_form_token(request: HttpRequest) -> bool:
    """Spend the posted token; if it was a replay or expired, queue the friendly toast and return False.
    The caller must then do nothing and redirect."""
    spent = consume_form_token(request)
    if spent == "ok":
        return True
    messages.info(request, REPLAY_TEXT if spent == "used" else EXPIRED_TEXT)
    return False


def one_time_form(
    back: Callable[..., HttpResponse],
    permission: str | None = None,
) -> Callable[[Callable[..., HttpResponse]], Callable[..., HttpResponse]]:
    """View decorator for POST-redirect-GET views: spends the one-time token first. A replayed, expired or
    missing token runs nothing and sends the person back (``back(request, *args, **kwargs)``) with a toast.
    Put it under ``@login_required`` and ``@require_POST`` so the token is checked for a known user. Pass the
    view's ``permission`` so a person without it is refused (403) before the token is looked at."""

    def decorate(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
        @functools.wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            # The role check comes BEFORE the token: a person without the permission gets the same 403 as
            # before the token existed, whatever they posted (and spends nothing).
            if permission is not None and not has_permission(request.user, permission):
                return HttpResponseForbidden("You do not have permission for this action.")
            if not spend_form_token(request):
                return back(request, *args, **kwargs)
            return view(request, *args, **kwargs)

        return wrapper

    return decorate


def refused_message(exc: CommandError, kinds: dict[str, str] | None = None) -> str:
    """``Refused (permission denied): Only the ops lead can do this. Nothing was changed.`` for a permission
    refusal (the role comes from the permission table); other refusals keep their own text. The prefix keeps
    the list-with-Details layout of the toast."""
    kind = (kinds or {}).get(exc.code, exc.code.replace("_", " "))
    text = str(exc)
    if isinstance(exc, PermissionDenied) and text.startswith("Not allowed:"):
        perm = text.split(":", 1)[1].strip()
        return f"Refused ({kind}): {refusal_text(perm)} Nothing was changed."
    return f"Refused ({kind}): {explain(exc)}"


# ------------------------------------------------------------------ forms
class UIForm(forms.Form):
    """Base form: help text and errors are tied to their inputs for assistive technology."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            attrs = field.widget.attrs
            if field.help_text:
                attrs["aria-describedby"] = f"id_{name}-help"
            if field.required and not isinstance(field.widget, forms.CheckboxInput):
                attrs.setdefault("required", "required")

    def is_valid(self) -> bool:
        valid = super().is_valid()
        for name in self.errors:
            if name in self.fields:
                self.fields[name].widget.attrs["aria-invalid"] = "true"
        return valid


class ConfirmForm(UIForm):
    """Add to a form for a destructive or important action: the box must be ticked (works without scripts)."""

    confirm = forms.BooleanField(
        required=True,
        label="Yes, I understand and want to do this",
        error_messages={"required": "Tick the box to confirm."},
    )


# ------------------------------------------------------------------ pages
def denied(request: HttpRequest, permission: str | Iterable[str]) -> HttpResponse:
    """403 page that names who can do this. Used when the viewer's role has no part in the action at all."""
    return render(request, "ui/denied.html", {"who": refusal_text(permission)}, status=403)


def holds_any(request: HttpRequest, permissions: str | Iterable[str]) -> bool:
    return any(has_permission(request.user, p) for p in _perm_list(permissions))


Result = dict[str, Any]


def command_page(
    request: HttpRequest,
    *,
    permission: str | Iterable[str],
    form_class: type[forms.Form],
    template: str,
    call: Callable[[Any, dict[str, Any]], Result],
    ok: str | Callable[[Result], str],
    success_url: str | Callable[[Result], str],
    context: dict[str, Any] | None = None,
    initial: dict[str, Any] | None = None,
    form_kwargs: dict[str, Any] | None = None,
    command_permission: str | Iterable[str] | Callable[[dict[str, Any]], str] | None = None,
) -> HttpResponse:
    """GET: the form. POST: role check, one-time token, validation, ONE service command, feedback.

    ``permission`` gates the page (403 for a role with no part in it). ``command_permission`` (default the
    same) is what the service enforces, used to word a refusal. The service is always the final authority."""
    if not holds_any(request, permission):
        return denied(request, permission)
    ctx = dict(context or {})
    fkw = dict(form_kwargs or {})
    static_perm = (
        permission if command_permission is None or callable(command_permission) else command_permission
    )
    ctx.setdefault("who_note", refusal_text(static_perm))

    def page(form: forms.Form, status: int = 200) -> HttpResponse:
        return render(request, template, {**ctx, "form": form}, status=status)

    if request.method != "POST":
        return page(form_class(initial=initial, **fkw))

    target = success_url if isinstance(success_url, str) else None
    spent = consume_form_token(request)
    if spent != "ok":
        messages.info(request, REPLAY_TEXT if spent == "used" else EXPIRED_TEXT)
        return redirect(target or request.path)
    form = form_class(request.POST, **fkw)
    if not form.is_valid():
        return page(form, 400)
    try:
        result = call(request.user, form.cleaned_data)
    except CommandError as exc:
        enforced = command_permission(form.cleaned_data) if callable(command_permission) else static_perm
        messages.error(request, explain(exc, enforced))
        return page(form, exc.status)
    messages.success(request, ok(result) if callable(ok) else ok)
    return redirect(success_url(result) if callable(success_url) else success_url)
