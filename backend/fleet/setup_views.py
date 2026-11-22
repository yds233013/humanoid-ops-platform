"""01C.2 Fleet screens: add a robot, record its configuration, change its lifecycle, configurations (patterns)
and capability profiles.

Every state change goes through ``platform_core.interaction.command_page`` and calls ONE existing service
command in ``fleet.services`` (register_robot, record_manifest, transition_lifecycle, create_pattern,
publish_capability_profile). Registering a robot or publishing a profile never makes anything eligible or
approved.

01C approval screens (each calls ONE existing guarded command, never auto-approves, and shows its
button only to the roles the command requires):
decide_pattern, approve_capability_profile, grant_base_authorization and revoke_base_authorization.
"""

from typing import Any

from django import forms
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render

from accounts.permissions import active_roles, has_permission
from fleet import services as s
from fleet.models import (
    BaseAuthorization,
    CapabilityProfile,
    ConfigurationManifest,
    ManifestPattern,
    PatternApproval,
    Robot,
)
from platform_core.commands import Conflict
from platform_core.interaction import ROLE_WORDS, ConfirmForm, UIForm, command_page, denied

FLEET_READ = "fleet.read"


# ------------------------------------------------------------------ forms
class RobotForm(UIForm):
    code = forms.CharField(
        max_length=10, label="Robot code", help_text="Your short name for the robot, for example R-21."
    )
    serial = forms.CharField(max_length=60, label="Serial number", help_text="From the robot's label.")

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        for k in ("code", "serial"):
            if k in data:
                data[k] = data[k].strip()
        return data


class ManifestForm(UIForm):
    pattern = forms.ChoiceField(label="Configuration", help_text="Hardware, release and firmware set.")
    autonomy_version = forms.CharField(max_length=40, label="Autonomy software version")
    safety_config_hash = forms.CharField(
        max_length=64,
        label="Safety configuration hash",
        help_text="The fingerprint of the safety settings, copied from the robot.",
    )
    calibration_ref = forms.CharField(max_length=60, required=False, label="Calibration reference")
    provenance = forms.ChoiceField(
        label="Where this came from", choices=ConfigurationManifest.PROVENANCE, initial="manual"
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        field: Any = self.fields["pattern"]
        field.choices = [(p.code, p.code) for p in ManifestPattern.objects.order_by("code")]


class LifecycleForm(ConfirmForm):
    to = forms.ChoiceField(
        label="Move to",
        choices=[
            ("validation", "Engineering validation"),
            ("in_service", "In service"),
            ("retired", "Retired"),
        ],
        help_text="Going into service needs a configuration and an approved capability profile. "
        "Retiring cannot be undone.",
    )
    reason = forms.CharField(max_length=300, label="Reason", help_text="Kept in the audit log.")


class PatternForm(UIForm):
    code = forms.CharField(max_length=30, label="Configuration code", help_text="For example P-A.")
    hardware_rev = forms.CharField(max_length=40, label="Hardware revision")
    release = forms.CharField(max_length=40, label="Software release")
    firmware_set = forms.CharField(max_length=80, label="Firmware set")


def _optional_int(label: str, help_text: str = "") -> forms.IntegerField:
    return forms.IntegerField(
        required=False,
        min_value=1,
        label=label,
        help_text=(help_text + " " if help_text else "") + "Leave empty if unknown.",
    )


class ProfileForm(UIForm):
    stop_behaviour = forms.CharField(
        required=False,
        label="Stop behaviour",
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="What the robot does when it stops. Leave empty if unknown.",
    )
    lift_crew = _optional_int("People needed to lift")
    transport_posture = forms.CharField(
        required=False, label="Transport posture", help_text="Leave empty if unknown."
    )
    max_runtime_min = _optional_int("Maximum runtime (minutes)")
    min_departure_soc_pct = _optional_int("Minimum battery at departure (%)")
    charge_turnaround_min = _optional_int("Charge turnaround (minutes)")
    load_unload_min = _optional_int("Load or unload time (minutes)")
    source = forms.ChoiceField(
        label="Source",
        choices=[("engineering_doc", "Engineering document"), ("synthetic_demo", "Synthetic demo value")],
        initial="engineering_doc",
    )

    def clean_min_departure_soc_pct(self) -> int | None:
        v = self.cleaned_data.get("min_departure_soc_pct")
        if v is not None and v > 100:
            raise forms.ValidationError("A percentage cannot be above 100.")
        return v  # type: ignore[no-any-return]


class PatternDecisionForm(ConfirmForm):
    status = forms.ChoiceField(
        label="Decision",
        choices=PatternApproval.STATUS,
        help_text="Approved for home use lets robots on this configuration be considered for customer homes. "
        "Withdrawn takes that away again.",
    )
    reason = forms.CharField(max_length=300, label="Reason", help_text="Kept in the audit log.")


SLOT_ROLES = {"engineering": "engineering_lead", "safety": "safety_reviewer"}
SLOT_LABELS = {"engineering": "Engineering lead", "safety": "Safety reviewer"}


class ProfileApprovalForm(ConfirmForm):
    slot = forms.ChoiceField(
        label="Approve as",
        choices=[(k, SLOT_LABELS[k]) for k in SLOT_ROLES],
        help_text="Each slot is approved by its own role, and one person cannot fill both.",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        offered = kwargs.pop("offered", None)
        super().__init__(*args, **kwargs)
        if offered is not None:
            # only the slots this person may fill are offered; the command still decides on a posted value
            self.fields["slot"].widget.choices = [(k, SLOT_LABELS[k]) for k in SLOT_ROLES if k in offered]  # type: ignore[attr-defined]


class GrantAuthorizationForm(ConfirmForm):
    pass


class RevokeAuthorizationForm(ConfirmForm):
    reason = forms.CharField(max_length=300, label="Reason", help_text="Kept in the audit log.")


# ------------------------------------------------------------------ pages
@login_required
def robot_new(request: HttpRequest) -> HttpResponse:
    return command_page(
        request,
        permission="fleet.register_robot",
        form_class=RobotForm,
        template="fleet/setup_robot_new.html",
        call=lambda actor, d: s.register_robot(actor, code=d["code"], serial=d["serial"]),
        ok=lambda r: (
            f"Robot {r['entity_id']} added. It is not ready for anything yet: record its configuration next."
        ),
        success_url=lambda r: f"/fleet/{r['entity_id']}/",
    )


@login_required
def robot_manifest(request: HttpRequest, code: str) -> HttpResponse:
    robot = get_object_or_404(Robot, code=code)
    return command_page(
        request,
        permission="fleet.record_manifest",
        form_class=ManifestForm,
        template="fleet/setup_manifest.html",
        call=lambda actor, d: s.record_manifest(
            actor,
            robot_code=code,
            pattern_code=d["pattern"],
            autonomy_version=d["autonomy_version"].strip(),
            safety_config_hash=d["safety_config_hash"].strip(),
            calibration_ref=d.get("calibration_ref", "").strip(),
            provenance=d["provenance"],
        ),
        ok="Configuration recorded as a proposal. Engineering still has to approve it before the robot "
        "can be used.",
        success_url=f"/fleet/{code}/",
        context={"robot": robot, "robot_url": f"/fleet/{code}/", "patterns": ManifestPattern.objects.count()},
    )


@login_required
def robot_lifecycle(request: HttpRequest, code: str) -> HttpResponse:
    robot = get_object_or_404(Robot, code=code)
    perms = ("fleet.transition_lifecycle", "fleet.retire_robot")
    return command_page(
        request,
        permission=perms,
        form_class=LifecycleForm,
        template="fleet/setup_lifecycle.html",
        call=lambda actor, d: s.transition_lifecycle(
            actor, robot_code=code, to=d["to"], reason=d["reason"].strip()
        ),
        ok=lambda r: f"Robot {code} moved to {r['to'].replace('_', ' ')}.",
        success_url=f"/fleet/{code}/",
        context={"robot": robot, "robot_url": f"/fleet/{code}/"},
        command_permission=lambda d: (
            "fleet.retire_robot" if d["to"] == "retired" else "fleet.transition_lifecycle"
        ),
    )


@login_required
def pattern_list(request: HttpRequest) -> HttpResponse:
    if not has_permission(request.user, FLEET_READ):
        return denied(request, FLEET_READ)
    rows = []
    for p in ManifestPattern.objects.order_by("code"):
        latest = p.profiles.order_by("-version_no").first()
        approved = s.approved_profile(p)
        rows.append(
            {
                "pattern": p,
                "robots": Robot.objects.filter(current_manifest__pattern=p).count(),
                "latest": latest,
                "profile_approved": approved is not None,
                "decision": s.pattern_status(p),
            }
        )
    return render(request, "fleet/setup_patterns.html", {"rows": rows})


@login_required
def pattern_new(request: HttpRequest) -> HttpResponse:
    return command_page(
        request,
        permission="fleet.manage_patterns",
        form_class=PatternForm,
        template="fleet/setup_pattern_new.html",
        call=lambda actor, d: s.create_pattern(
            actor,
            code=d["code"].strip(),
            hardware_rev=d["hardware_rev"].strip(),
            release=d["release"].strip(),
            firmware_set=d["firmware_set"].strip(),
        ),
        ok=lambda r: f"Configuration {r['entity_id']} added. Next: publish its capability profile.",
        success_url="/fleet/patterns/",
    )


@login_required
def profile_new(request: HttpRequest, code: str) -> HttpResponse:
    pattern = get_object_or_404(ManifestPattern, code=code)

    def call(actor: Any, d: dict[str, Any]) -> dict[str, Any]:
        fields = {
            k: v
            for k, v in d.items()
            if k not in ("source",) and v not in (None, "")  # empty means "unknown": stays NULL
        }
        return s.publish_capability_profile(actor, pattern_code=code, source=d["source"], **fields)

    profiles = list(
        CapabilityProfile.objects.filter(pattern=pattern)
        .prefetch_related("approvals")
        .order_by("-version_no")
    )
    return command_page(
        request,
        permission="fleet.publish_capability",
        form_class=ProfileForm,
        template="fleet/setup_profile.html",
        call=call,
        ok=lambda r: (
            f"Capability profile v{r['version']} published as a draft. It still needs approval from "
            "the engineering lead and the safety reviewer."
        ),
        success_url=f"/fleet/patterns/{code}/",
        context={
            "pattern": pattern,
            "profiles": [{"p": p, "slots": sorted(a.slot for a in p.approvals.all())} for p in profiles],
        },
    )


@login_required
def pattern_decision(request: HttpRequest, code: str) -> HttpResponse:
    pattern = get_object_or_404(ManifestPattern, code=code)
    return command_page(
        request,
        permission="fleet.decide_pattern",
        form_class=PatternDecisionForm,
        template="fleet/setup_pattern_decision.html",
        call=lambda actor, d: s.decide_pattern(
            actor, pattern_code=code, status=d["status"], reason=d["reason"].strip()
        ),
        ok=lambda r: (
            f"Decision recorded for configuration {code}: "
            f"{dict(PatternApproval.STATUS)[r['status']].lower()}."
        ),
        success_url=f"/fleet/patterns/{code}/",
        context={
            "pattern": pattern,
            "pattern_url": f"/fleet/patterns/{code}/",
            "decision": s.pattern_status(pattern),
            "decision_label": dict(PatternApproval.STATUS).get(s.pattern_status(pattern), "none yet"),
            "history": list(pattern.approvals.select_related("decided_by").order_by("-id")[:5]),
        },
    )


def _slot_roles_of(request: HttpRequest) -> set[str]:
    roles = set(active_roles(request.user))
    return {slot for slot, role in SLOT_ROLES.items() if role in roles}


@login_required
def profile_approve(request: HttpRequest, code: str, profile_id: int) -> HttpResponse:
    pattern = get_object_or_404(ManifestPattern, code=code)
    profile = get_object_or_404(
        CapabilityProfile.objects.prefetch_related("approvals__approver"), pk=profile_id
    )
    if profile.pattern_id != pattern.pk:
        return render(request, "404.html", status=404)
    done = {a.slot: a for a in profile.approvals.all()}
    mine = _slot_roles_of(request)

    def call(actor: Any, d: dict[str, Any]) -> dict[str, Any]:
        slot = d["slot"]
        try:
            return s.approve_capability_profile(actor, profile_id=profile_id, slot=slot)
        except Conflict as exc:
            if "requires role" in str(exc):  # the database's slot-role guard, in plain words
                raise Conflict(
                    f"Only {ROLE_WORDS[SLOT_ROLES[slot]]} can approve the {SLOT_LABELS[slot].lower()} slot. "
                    "Nothing was changed."
                ) from exc
            raise

    return command_page(
        request,
        permission="fleet.approve_capability",
        form_class=ProfileApprovalForm,
        template="fleet/setup_profile_approve.html",
        call=call,
        ok=lambda r: f"Approval recorded for capability profile v{profile.version_no} ({r['slot']} slot).",
        success_url=f"/fleet/patterns/{code}/profile/approve/{profile_id}/",
        context={
            "pattern": pattern,
            "profile": profile,
            "slot_rows": [
                {
                    "slot": k,
                    "label": SLOT_LABELS[k],
                    "role_words": ROLE_WORDS[SLOT_ROLES[k]],
                    "approval": done.get(k),
                    "mine": k in mine,
                }
                for k in SLOT_ROLES
            ],
            "profile_url": f"/fleet/patterns/{code}/profile/",
            "pending": [k for k in SLOT_ROLES if k not in done],
            "mine_pending": [k for k in SLOT_ROLES if k not in done and k in mine],
        },
        form_kwargs={"offered": [k for k in mine if k not in done]},
    )


@login_required
def robot_authorize(request: HttpRequest, code: str) -> HttpResponse:
    robot = get_object_or_404(Robot, code=code)
    return command_page(
        request,
        permission="fleet.base_authorization",
        form_class=GrantAuthorizationForm,
        template="fleet/setup_authorize.html",
        call=lambda actor, d: s.grant_base_authorization(actor, robot_code=code),
        ok=f"Home-use authorization granted for {code}. The robot still needs everything else on its "
        "readiness list.",
        success_url=f"/fleet/{code}/",
        context=_auth_context(robot, revoking=False),
    )


@login_required
def robot_deauthorize(request: HttpRequest, code: str) -> HttpResponse:
    robot = get_object_or_404(Robot, code=code)
    return command_page(
        request,
        permission="fleet.base_authorization",
        form_class=RevokeAuthorizationForm,
        template="fleet/setup_authorize.html",
        call=lambda actor, d: s.revoke_base_authorization(actor, robot_code=code, reason=d["reason"].strip()),
        ok=f"Home-use authorization revoked for {code}.",
        success_url=f"/fleet/{code}/",
        context=_auth_context(robot, revoking=True),
    )


def _auth_context(robot: Robot, *, revoking: bool) -> dict[str, Any]:
    manifest = robot.current_manifest
    pattern = manifest.pattern if manifest is not None else None
    active = (
        BaseAuthorization.objects.filter(robot=robot, revoked_at__isnull=True)
        .select_related("granted_by", "pattern")
        .first()
    )
    return {
        "robot": robot,
        "robot_url": f"/fleet/{robot.code}/",
        "pattern": pattern,
        "active": active,
        "revoking": revoking,
    }
