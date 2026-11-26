"""Owner provisioning of the operating settings (v2 01C). Run as the schema owner; NOT an HTTP endpoint.

Allowlist only (``fleet.provisioning.SPECS``): ``demo_mode`` and ``allow_draft_policies`` are never touched.
Values come from options (``--consent-form-version``) or the environment
(``PROVISION_CONSENT_FORM_VERSION``); an option wins. Takes the settings advisory lock EXCLUSIVE first,
validates everything before writing anything, is idempotent (same value: unchanged), refuses to change an
existing different value without ``--allow-change``, and writes one audit event for what it changed.

v2 02C: ``--maintenance-gate-active true`` activates the maintenance gate (no --allow-change needed: the
safe direction); ``false`` over an existing ``true`` deactivates it and needs ``--allow-change``. Both are
audited and run under the settings lock, so no plan, authorization or departure straddles the switch.

v2 03A-1 (docs/13_v2_phase03_contract.md 11.3): ``--hub CODE`` writes that hub's OWN row of a per-hub setting,
which overrides the global row for that hub. Only ``stock_minimums``, ``stock_per_visit`` and (v2 03B-3)
``hub_operating_hours`` can be set per hub; every other key (the maintenance gate and every safety setting)
stays global and is refused with ``--hub``. The same lock, validation, idempotency, ``--allow-change`` rule
and audit apply; the audit names the hub.
"""

import os
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from fleet import provisioning as pv
from fleet.models import PER_HUB_SETTING_KEYS, DeploymentSetting
from platform_core.locks import SETTINGS_LOCK_KEY
from platform_core.models import AuditEvent, Hub

COMMAND = "fleet.provision_settings"
GATE = "maintenance_gate_active"


def _activation(key: str, value: str) -> bool:
    """v2 02C: turning the maintenance gate ON is the safe direction and needs no --allow-change."""
    return key == GATE and value == "true"


def _keys_text() -> str:
    """``stock_minimums, stock_per_visit and hub_operating_hours``"""
    keys = list(PER_HUB_SETTING_KEYS)
    return ", ".join(keys[:-1]) + " and " + keys[-1]


def _opt(key: str) -> str:
    return key.replace("_", "-")


class Command(BaseCommand):
    help = (
        "Provision deployment operating settings (owner only). Allowlisted keys only. "
        "Values from options or PROVISION_<KEY> environment variables."
    )

    def add_arguments(self, parser: Any) -> None:
        for key in pv.ALLOWED_KEYS:
            parser.add_argument(
                f"--{_opt(key)}", dest=key, default=None, help=f"or env PROVISION_{key.upper()}"
            )
        parser.add_argument(
            "--allow-change", action="store_true", help="allow replacing an existing different value"
        )
        parser.add_argument(
            "--hub",
            default=None,
            help="write this hub's own value (overrides the global value at that hub); only " + _keys_text(),
        )
        parser.add_argument(
            "--with-defaults",
            action="store_true",
            help="use the offered default for a required value that is neither given nor already set "
            "(ops_readiness_lookahead_min = 2880, which covers today and tomorrow)",
        )

    def handle(self, *args: Any, **opts: Any) -> None:
        wanted: dict[str, str] = {}
        for key in pv.ALLOWED_KEYS:
            raw = opts.get(key)
            if raw is None:
                raw = os.environ.get(f"PROVISION_{key.upper()}")
            if raw is not None and raw != "":
                wanted[key] = raw
        hub = None
        if opts.get("hub"):
            code = str(opts["hub"]).strip().upper()
            hub = Hub.objects.filter(code=code).first()
            if hub is None:
                raise CommandError(f"Nothing was changed. No hub {code}.")
            other = sorted(k for k in wanted if k not in PER_HUB_SETTING_KEYS)
            if other:
                raise CommandError(
                    "Nothing was changed. Only "
                    + _keys_text()
                    + " can be set per hub; "
                    + ", ".join(other)
                    + " stay global (run without --hub)."
                )
            if opts["with_defaults"]:
                raise CommandError(
                    "Nothing was changed. --with-defaults applies to the global settings only."
                )
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", [SETTINGS_LOCK_KEY])  # EXCLUSIVE, first
            have = pv.current_values(hub)
            if opts["with_defaults"]:
                for spec in pv.SPECS:
                    if spec.default and spec.key not in wanted and spec.key not in have:
                        wanted[spec.key] = spec.default

            errors: list[str] = []
            clean: dict[str, str] = {}
            for key, raw in wanted.items():
                try:
                    clean[key] = pv.validate(key, raw)
                except ValueError as exc:
                    errors.append(str(exc))
            if errors:
                raise CommandError("Nothing was changed. " + "; ".join(errors))

            missing = [k for k in pv.REQUIRED_KEYS if k not in clean and k not in have] if hub is None else []
            if missing:
                raise CommandError(
                    "Nothing was changed. Required values are missing: "
                    + ", ".join(missing)
                    + ". Give them as options or PROVISION_<KEY> environment variables."
                )

            blocked = [
                k
                for k, v in clean.items()
                if k in have and have[k] != v and not opts["allow_change"] and not _activation(k, v)
            ]
            # v2 02C: turning the maintenance gate ON needs no --allow-change; turning it OFF (true -> false)
            # does, like any other change of an existing value.
            if blocked:
                raise CommandError(
                    "Nothing was changed. These already have a different value: "
                    + ", ".join(f"{k} (now {have[k]!r})" for k in blocked)
                    + ". Run again with --allow-change to replace them."
                )

            changed: dict[str, dict[str, str | None]] = {}
            unchanged: list[str] = []
            for key, value in clean.items():
                if have.get(key) == value:
                    unchanged.append(key)
                    continue
                DeploymentSetting.objects.update_or_create(key=key, hub=hub, defaults={"value": value})
                changed[key] = {"from": have.get(key), "to": value}
            if changed:
                AuditEvent.objects.create(
                    actor=None,
                    command=COMMAND,
                    outcome=AuditEvent.OUTCOME_COMMITTED,
                    entity_type="deployment_setting",
                    entity_id="provision_settings" if hub is None else f"provision_settings:{hub.code}",
                    payload={
                        "keys": sorted(clean),
                        "allow_change": bool(opts["allow_change"]),
                        "hub": hub.code if hub is not None else None,
                    },  # fmt: skip
                    result={"changed": changed, "unchanged": sorted(unchanged)},
                )
        where = f" at hub {hub.code}" if hub is not None else ""
        for key, c in changed.items():
            self.stdout.write(
                f"set {key}{where} = {c['to']}" + (f" (was {c['from']})" if c["from"] is not None else "")
            )
        for key in unchanged:
            self.stdout.write(f"unchanged {key}")
        if not clean:
            self.stdout.write("nothing to provision")
