# UI components for forms that change state (v2 01C)

Code: `platform_core/interaction.py`, `platform_core/templatetags/ui.py`, `templates/ui/*`. No JavaScript is needed
for any of them (the CSP allows one hashed script; none of these add one).

## A form page in one call

```python
from platform_core.interaction import UIForm, ConfirmForm, command_page

@login_required
def robot_new(request):
    return command_page(
        request,
        permission="fleet.register_robot",          # 403 page for a role with no part in it
        form_class=RobotForm,                        # a UIForm
        template="fleet/setup_robot_new.html",
        call=lambda actor, d: s.register_robot(actor, code=d["code"], serial=d["serial"]),  # ONE existing command
        ok="Robot added.",                           # or a function of the result
        success_url="/fleet/",                       # or a function of the result
    )
```

GET shows the form. POST: role check, one-time token, form validation, the command, then feedback. A refusal
re-renders the form with the plain-words message (HTTP status = the command error's status). The service remains
the final authority; the view never writes models itself (workspace settings and hubs are the only new setup data).
`command_permission` (a permission, or a function of the cleaned data) words a refusal when the command enforces
a different permission than the page gate (for example retiring a robot is the founder's).

## The five components

1. **Confirmation without JavaScript.** Subclass `ConfirmForm` (adds a required "Yes, I understand" box) and show
   `{% include "ui/confirm_box.html" with headline="..." detail="..." %}` above the form. The action's page is the
   confirm page; submitting without the tick is refused with "Tick the box to confirm."
2. **Toast feedback.** `django.contrib.messages` in `base.html` renders `.toasts > .toast.success|error|info|warning`
   (fixed banner, pure CSS; success fades after 12 s; errors stay). Refusal strings that start with
   `Refused (kind):` keep the existing list-with-Details layout.
3. **Double-submit protection.** `{% form_token %}` (inside the form, next to `{% csrf_token %}`; `ui/form.html`
   does both) issues a one-time token stored in `platform_core_formtoken`. The first POST spends it with one atomic
   UPDATE; a replay, a second simultaneous POST or an expired form is answered with "That form was already sent..."
   and runs nothing. Tokens are bound to the signed-in user and expire after 12 hours.
4. **Refusal renderer naming the role.** `who_can "perm"` -> "the ops lead or the engineering lead";
   `{% refusal_note "perm" %}` -> "Only the safety reviewer can do this."; `{% waiting_note "perm" %}` ->
   "Waiting for the founder's approval" (one role) or "Waiting for approval from A or B". Python:
   `who_can`, `refusal_text`, `waiting_text`, `explain(exc, permission)`. `{% can "perm" as ok %}` hides actions
   from roles that may not act. Wording comes from `accounts/roles.py`, never hand-written per page.
5. **Form field with help and inline errors.** `{% include "ui/form_field.html" with field=form.code %}` shows the
   label ("(required)"), the input, help text (linked with `aria-describedby`) and errors (`role="alert"`,
   `aria-invalid` on the input). `{% include "ui/form.html" with form=form submit="Save" cancel_url="/x/" %}` renders
   a whole form: CSRF, token, fields, submit (`danger=True` for destructive buttons).

## Limits

- Tokens are per rendered form. A page that renders several forms issues several tokens.
- A refusal consumes the token; the re-rendered form carries a fresh one.

## Time zones (v2 01D)

- **Display zone = the workspace time zone** (Settings > Company; `platform_core_workspace.time_zone`).
  `platform_core.timezones.DisplayTimeZoneMiddleware` reads it once per request and activates it, so
  `local_dt`, `local_time`, `local_date`, `localize_utc_text`, `{% workspace_tz_label %}`, Today's
  "today/tomorrow" boundary, the planner week and `timezone.localdate()` all use it. Times typed into a form
  labelled with `{% workspace_tz_label %}` are read in it. `settings.TIME_ZONE` is only the fallback when no
  workspace row exists (or its value is unusable).
- **Business-rule zones are separate and unchanged**: daily close-out uses the owner's `ops_closeout_timezone`
  operating setting (its screen names that zone); booking reads wall times in the home's own zone
  (`Site.timezone`, shown next to the booking form); hubs record their own zone. None of them decides how an
  instant is labelled on screen.
- **Storage is UTC.** Audit rows and every stored instant are UTC; the database session time zone stays UTC.

## Temporary passwords (v2 01D)

An account the admin creates (Invite someone) or gives a new temporary password (People and roles > person >
Set a temporary password) must choose its own password at its next sign-in: `accounts.PasswordState.must_change`
is set by `accounts.services.create_user` / `reset_password` and cleared only by the person's own
`change_own_password`. `accounts.middleware.ForcedPasswordChangeMiddleware` sends every page to
`/account/password/` (the API answers 403 `password_change_required`) until then. The bootstrap admin and the
synthetic dev users are never forced. No password appears in an audit row, URL or message.
