"""Forms for the customer and booking screens. They validate shape only; every rule that matters
(eligibility, consent, feasibility, permissions) is enforced by the planning service command the view calls.

Privacy by construction: the only free text is a short reason. Every other field is a code, a choice, a
number or a date, so there is nowhere to type a customer's name, a household member's name, a medical detail
or a contact detail. Names, addresses and contacts stay in the CRM; here a home is only a code and an opaque
CRM reference.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from django import forms
from django.core.validators import RegexValidator
from django.utils import timezone

from fleet.models import Robot
from planning.models import ConsentRecord, Person, Site, SiteAssessment, Vehicle
from platform_core.templatetags.ui import home_text
from platform_core.timezones import workspace_zone_name

CODE = RegexValidator(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,19}$",
    "Use letters, numbers, dots, dashes or underscores only (no spaces).",
)
REF = RegexValidator(
    r"^[A-Za-z0-9][A-Za-z0-9_.:/#-]{0,59}$",
    "Use the reference as it appears in the CRM: letters, numbers and - _ . : / # only "
    "(no spaces, no names).",
)
PARTICIPANT = RegexValidator(r"^P\d{1,3}$", "Use P1, P2, P3 ... for the adults in the home. Never a name.")
REASON_HELP = "A short reason in your own words. Do not include names or medical details."
SCOPE_CHOICES = [(s, s.replace("_", " ")) for s in ConsentRecord.SCOPES]


def parse_codes(text: str) -> list[str]:
    return [t for t in (x.strip() for x in text.replace(";", ",").split(",")) if t]


TASK_WORDS_MAX = 2


def normalize_task(token: str) -> str:
    """01D J3-3: a task may be typed as words. ``laundry folding`` -> ``laundry_folding`` (lowercase, spaces
    to underscores). Still no sentences: at most two words of letters, and no capitalised second word (a name
    such as "Mrs Smith" is refused). A single token (``T-04``) is kept exactly as typed."""
    words = token.split()
    if len(words) <= 1:
        return token
    if len(words) > TASK_WORDS_MAX:
        raise forms.ValidationError(
            "Use a short task name of one or two words, for example laundry folding. Never a sentence."
        )
    if (
        not all(w.isascii() and w.isalpha() for w in words)
        or any(w[1:] != w[1:].lower() for w in words)
        or (words[1][0].isupper())
    ):
        raise forms.ValidationError("Task words are plain lowercase words, for example laundry folding.")
    return "_".join(w.lower() for w in words)


class CodesField(forms.CharField):
    """Comma-separated task codes such as ``T-04, laundry folding``. Short codes or two-word task names only
    (normalized to ``laundry_folding``), so no sentence can be typed here."""

    def to_python(self, value: Any) -> Any:
        text = super().to_python(value) or ""
        codes = [normalize_task(c) for c in parse_codes(text)]
        for c in codes:
            CODE(c)
        return codes

    def validate(self, value: Any) -> None:
        if self.required and not value:
            raise forms.ValidationError(self.error_messages["required"], code="required")

    def run_validators(self, value: Any) -> None:
        return None


class SiteForm(forms.Form):
    code = forms.CharField(
        label="Home code",
        max_length=12,
        validators=[CODE],
        help_text="Your own short code, for example H-014.",
    )
    zone = forms.CharField(
        label="Service area", max_length=60, help_text="The area or route zone, not a street address."
    )
    tz = forms.CharField(
        label="Time zone",
        max_length=60,
        initial=workspace_zone_name,  # callable: the workspace display zone when the form is shown
        help_text="For example America/Los_Angeles.",
    )
    crm_ref = forms.CharField(
        label="CRM reference",
        max_length=60,
        validators=[REF],
        help_text="The customer's ID in the CRM. Names, addresses and contacts stay in the CRM.",
    )
    expected_adults = forms.IntegerField(
        label="Adults in the home",
        min_value=1,
        max_value=20,
        required=False,
        help_text="Leave blank if not known yet. Consent is checked against this number.",
    )


class AdultsForm(forms.Form):
    expected_adults = forms.IntegerField(
        label="Adults in the home",
        min_value=1,
        max_value=20,
        required=False,
        help_text="Leave blank if not known.",
    )


class AssessmentForm(forms.Form):
    decision = forms.ChoiceField(label="Result", choices=SiteAssessment.DECISIONS)
    valid_until = forms.DateField(label="Valid until", widget=forms.DateInput(attrs={"type": "date"}))
    excluded_tasks = CodesField(
        label="Tasks not allowed at this home",
        required=False,
        help_text="Task codes or short task names separated by commas, for example T-04, laundry folding "
        "(saved as laundry_folding). Leave blank if none.",
    )
    supersedes = forms.ChoiceField(
        label="Replaces",
        required=False,
        help_text="Choose the older assessment this one replaces, so the old one stops counting.",
    )

    def __init__(self, *args: Any, site: Site, **kw: Any) -> None:
        super().__init__(*args, **kw)
        old = list(site.assessments.order_by("-assessed_at"))
        self.valid_old = {str(a.pk) for a in old}
        self.fields["supersedes"].choices = [("", "Nothing (first assessment)")] + [  # type: ignore[attr-defined]
            (str(a.pk), f"{a.get_decision_display()}, assessed {a.assessed_at:%d %b %Y}") for a in old
        ]
        if old and not self.is_bound:
            self.fields["supersedes"].initial = str(old[0].pk)

    def clean_valid_until(self) -> date:
        v: date = self.cleaned_data["valid_until"]
        if v < timezone.localdate():
            raise forms.ValidationError("The assessment must be valid beyond today.")
        return v


class ConsentForm(forms.Form):
    participant_ref = forms.CharField(
        label="Adult", max_length=20, validators=[PARTICIPANT], help_text="P1, P2, P3 ... Never a name."
    )
    scope = forms.MultipleChoiceField(
        label="Consent covers",
        choices=SCOPE_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        initial=["presence"],
    )
    evidence_ref = forms.CharField(
        label="Signed form reference",
        max_length=60,
        validators=[REF],
        help_text="The document ID of the signed form, as filed in the CRM.",
    )


class WithdrawForm(forms.Form):
    reason = forms.CharField(label="Reason", max_length=200, help_text=REASON_HELP)
    confirm = forms.BooleanField(
        label="Yes, withdraw this consent now", error_messages={"required": "Tick the box to confirm."}
    )


class BookForm(forms.Form):
    site = forms.ChoiceField(label="Home")
    day = forms.DateField(label="Day", widget=forms.DateInput(attrs={"type": "date"}))
    start = forms.TimeField(label="On site from", widget=forms.TimeInput(attrs={"type": "time"}))
    end = forms.TimeField(label="On site until", widget=forms.TimeInput(attrs={"type": "time"}))
    travel_out_min = forms.IntegerField(
        label="Travel there (minutes)",
        min_value=1,
        max_value=600,
        required=False,
        help_text="Leave blank if unknown. The plan will say it has to be filled in.",
    )
    travel_back_min = forms.IntegerField(
        label="Travel back (minutes)", min_value=1, max_value=600, required=False
    )
    tasks = CodesField(
        label="Tasks",
        required=False,
        help_text="Task codes or short task names separated by commas, for example T-04, laundry folding "
        "(saved as laundry_folding).",
    )
    scopes = forms.MultipleChoiceField(
        label="Consent the visit needs",
        choices=SCOPE_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        initial=["presence"],
    )

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.fields["site"].choices = [("", "Choose a home")] + [  # type: ignore[attr-defined]
            (s.code, home_text(s)) for s in Site.objects.order_by("code")
        ]

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        if data.get("start") and data.get("end") and data["end"] <= data["start"]:
            self.add_error("end", "The visit must end after it starts.")
        return data

    def local_times(self) -> tuple[datetime, datetime]:
        d = self.cleaned_data
        return datetime.combine(d["day"], d["start"]), datetime.combine(d["day"], d["end"])


class PlanForm(forms.Form):
    robot = forms.ChoiceField(required=False)
    lead = forms.ChoiceField(required=False)
    second = forms.ChoiceField(required=False)
    vehicle = forms.ChoiceField(required=False)
    start = forms.DateTimeField(
        required=False,
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    end = forms.DateTimeField(
        required=False,
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    reason = forms.CharField(required=False, max_length=200)

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        blank = [("", "-")]
        robots = Robot.objects.exclude(lifecycle="retired").order_by("code")  # the service refuses the rest
        people = blank + [(x.code, x.code) for x in Person.objects.order_by("code")]
        self.fields["robot"].choices = blank + [(r.code, r.code) for r in robots]  # type: ignore[attr-defined]
        self.fields["lead"].choices = people  # type: ignore[attr-defined]
        self.fields["second"].choices = people  # type: ignore[attr-defined]
        self.fields["vehicle"].choices = blank + [  # type: ignore[attr-defined]
            (x.code, x.code) for x in Vehicle.objects.order_by("code")
        ]

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        if bool(data.get("start")) != bool(data.get("end")):
            raise forms.ValidationError("Give both a new start and a new end, or neither.")
        return data


class CancelForm(forms.Form):
    reason = forms.CharField(label="Reason", max_length=200, help_text=REASON_HELP)
    confirm = forms.BooleanField(
        label="Yes, cancel this visit", error_messages={"required": "Tick the box to confirm."}
    )
