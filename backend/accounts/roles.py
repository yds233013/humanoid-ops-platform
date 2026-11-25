"""Roles and the permissions each one carries. Server-side only; the UI just mirrors these."""

ADMIN = "admin"  # manages users and roles; approves nothing operational
OPS_LEAD = "ops_lead"
FIELD_OPERATOR = "field_operator"
ENGINEER = "engineer"
ENGINEERING_LEAD = "engineering_lead"
SAFETY_REVIEWER = "safety_reviewer"
FOUNDER = "founder"
HUB_TECHNICIAN = "hub_technician"  # v2 02A: the hub work queue only (contract 11, 8.6)

ALL_ROLES = [
    ADMIN,
    OPS_LEAD,
    FIELD_OPERATOR,
    ENGINEER,
    ENGINEERING_LEAD,
    SAFETY_REVIEWER,
    FOUNDER,
    HUB_TECHNICIAN,
]

# v2 03A-1 (docs/13_v2_phase03_contract.md 11.2): roles held AT ONE HUB. A grant of one of these roles must
# name its hub (check constraint role_grant_hub_scope); every other role is regional (hub NULL). A person's
# home
# hub never confers a grant.
HUB_SCOPED_ROLES = (HUB_TECHNICIAN, FIELD_OPERATOR)

# permission -> roles allowed. Slice 1+ add domain permissions here.
PERMISSIONS: dict[str, set[str]] = {
    "accounts.grant_role": {ADMIN},
    "accounts.revoke_role": {ADMIN},
    # v2 01C: the workspace admin invites people and sets up the workspace (records facts, approves nothing)
    "accounts.create_user": {ADMIN},
    "accounts.reset_password": {ADMIN},  # v2 01D: sets a temporary password the person must change
    "platform_core.manage_workspace": {ADMIN},
    "platform_core.manage_hubs": {ADMIN},
    "platform_core.read_operating_settings": {ADMIN, OPS_LEAD},
    "audit.read": {ADMIN, OPS_LEAD, ENGINEERING_LEAD, SAFETY_REVIEWER, FOUNDER},
    # Slice 1: fleet core
    "fleet.read": {OPS_LEAD, FIELD_OPERATOR, ENGINEER, ENGINEERING_LEAD, SAFETY_REVIEWER, FOUNDER},
    "fleet.register_robot": {OPS_LEAD, ENGINEERING_LEAD},
    "fleet.transition_lifecycle": {ENGINEERING_LEAD},
    "fleet.retire_robot": {FOUNDER},
    "fleet.manage_patterns": {ENGINEERING_LEAD},
    "fleet.decide_pattern": {ENGINEERING_LEAD},
    "fleet.record_manifest": {ENGINEER, ENGINEERING_LEAD},
    "fleet.publish_capability": {ENGINEER, ENGINEERING_LEAD},
    "fleet.approve_capability": {ENGINEERING_LEAD, SAFETY_REVIEWER},
    "fleet.base_authorization": {ENGINEERING_LEAD},
    # Slice 7 (G-01, EM decision 10): engineers may author hold-policy versions; the founder fills the third
    # slot of an approved_procedure policy.
    "fleet.manage_hold_policy": {ENGINEER, ENGINEERING_LEAD},
    "fleet.approve_hold_policy": {ENGINEERING_LEAD, SAFETY_REVIEWER, FOUNDER},
    "fleet.withdraw_hold_policy": {ENGINEERING_LEAD, SAFETY_REVIEWER},
    "fleet.rebase_hold": {ENGINEERING_LEAD, SAFETY_REVIEWER},
    # Slice 7 (X-1): inspection templates
    "fleet.manage_inspection_template": {ENGINEER, ENGINEERING_LEAD},
    "fleet.approve_inspection_template": {ENGINEERING_LEAD, SAFETY_REVIEWER},
    "fleet.withdraw_inspection_template": {ENGINEERING_LEAD, SAFETY_REVIEWER},
    # Slice 7 (X-3): periodic inspection interval rules
    "fleet.manage_periodic_rule": {ENGINEER, ENGINEERING_LEAD},
    "fleet.approve_periodic_rule": {ENGINEERING_LEAD, SAFETY_REVIEWER},
    "fleet.withdraw_periodic_rule": {ENGINEERING_LEAD, SAFETY_REVIEWER},
    # v2 02C (contract 11, 8.6): maintenance rules and records. The database functions re-check the role,
    # and a record also needs a valid maintenance_technician qualification (a role never stands in for it).
    "fleet.propose_maintenance_rule": {ENGINEER, ENGINEERING_LEAD},
    "fleet.approve_maintenance_rule": {ENGINEERING_LEAD, SAFETY_REVIEWER},
    "fleet.withdraw_maintenance_rule": {ENGINEERING_LEAD, SAFETY_REVIEWER},
    "fleet.record_maintenance": {HUB_TECHNICIAN, ENGINEER, ENGINEERING_LEAD},
    "fleet.place_hold": {OPS_LEAD, FIELD_OPERATOR, ENGINEER, ENGINEERING_LEAD, SAFETY_REVIEWER, FOUNDER},
    "fleet.approve_release": {OPS_LEAD, ENGINEER, ENGINEERING_LEAD, SAFETY_REVIEWER, FOUNDER},
    "fleet.release_hold": {OPS_LEAD, ENGINEERING_LEAD},
    "fleet.submit_inspection": {OPS_LEAD, FIELD_OPERATOR},
    "fleet.check_eligibility": {OPS_LEAD, FIELD_OPERATOR, ENGINEERING_LEAD},
    # Slice 2: planning
    "planning.read": {OPS_LEAD, FIELD_OPERATOR, ENGINEER, ENGINEERING_LEAD, SAFETY_REVIEWER, FOUNDER},
    "planning.manage_sites": {OPS_LEAD},
    "planning.assess": {OPS_LEAD, FIELD_OPERATOR},
    "planning.approve_assessment": {OPS_LEAD, ENGINEERING_LEAD},
    "planning.consent": {OPS_LEAD, FIELD_OPERATOR},
    "planning.manage_people": {OPS_LEAD},
    # Slice 7 (X-8, EM decision 18): readiness_inspector is granted by a safety reviewer only (never to
    # themselves, enforced in the database); revoking it (the safe direction) is open to three roles.
    "planning.grant_inspector_qualification": {SAFETY_REVIEWER},
    "planning.revoke_inspector_qualification": {OPS_LEAD, SAFETY_REVIEWER, ENGINEERING_LEAD},
    "planning.manage_vehicles": {OPS_LEAD},
    "planning.plan": {OPS_LEAD},
    "planning.confirm": {OPS_LEAD},
    # Slice 3: deployment readiness and departure authorization
    "readiness.read": {OPS_LEAD, FIELD_OPERATOR, ENGINEER, ENGINEERING_LEAD, SAFETY_REVIEWER, FOUNDER},
    "readiness.manage_policy": {ENGINEERING_LEAD},
    "readiness.approve_policy": {ENGINEERING_LEAD, SAFETY_REVIEWER},
    "readiness.withdraw_policy": {ENGINEERING_LEAD, SAFETY_REVIEWER},
    "readiness.record_evidence": {OPS_LEAD, FIELD_OPERATOR, ENGINEER},
    "readiness.record_recovery_plan": {OPS_LEAD, ENGINEERING_LEAD},
    "readiness.authorize_departure": {OPS_LEAD},
    "readiness.revoke_authorization": {OPS_LEAD, ENGINEERING_LEAD, SAFETY_REVIEWER, FOUNDER},
    "readiness.record_departure": {OPS_LEAD, FIELD_OPERATOR},
    # Slice 4: field execution and incident response
    "execution.read": {OPS_LEAD, FIELD_OPERATOR, ENGINEER, ENGINEERING_LEAD, SAFETY_REVIEWER, FOUNDER},
    "execution.record_field_event": {OPS_LEAD, FIELD_OPERATOR},
    "execution.report_incident": {
        OPS_LEAD,
        FIELD_OPERATOR,
        ENGINEER,
        ENGINEERING_LEAD,
        SAFETY_REVIEWER,
        FOUNDER,
    },
    "execution.triage_incident": {OPS_LEAD, ENGINEERING_LEAD, SAFETY_REVIEWER},
    "execution.change_severity": {ENGINEERING_LEAD, SAFETY_REVIEWER},
    "execution.close_incident": {ENGINEERING_LEAD, SAFETY_REVIEWER},
    "execution.record_disposition": {OPS_LEAD},
    "execution.reconcile": {OPS_LEAD},
    "execution.close_trip": {OPS_LEAD},
    "execution.manage_incident_policy": {ENGINEERING_LEAD},
    "execution.approve_incident_policy": {ENGINEERING_LEAD, SAFETY_REVIEWER},
    # Slice 7 (X-4, EM decision 16): owner-approved escalation deadline sets
    "execution.manage_escalation_deadlines": {OPS_LEAD, SAFETY_REVIEWER},
    "execution.approve_escalation_deadlines": {SAFETY_REVIEWER, FOUNDER},
    "execution.withdraw_escalation_deadlines": {SAFETY_REVIEWER, FOUNDER},
    # Slice 5: operations command center (read-only)
    "operations.read": {OPS_LEAD, FIELD_OPERATOR, ENGINEER, ENGINEERING_LEAD, SAFETY_REVIEWER, FOUNDER},
    "operations.metrics": {OPS_LEAD, ENGINEERING_LEAD, SAFETY_REVIEWER, FOUNDER},
    # v2 02A: hub work queue (docs/11_v2_phase02_contract.md 8.6). Work orders are operational, not evidence.
    # Open the Hub page: everyone in the matrix's "View Hub queue" row (a field operator sees own tasks only).
    "hub.view": {HUB_TECHNICIAN, FIELD_OPERATOR, OPS_LEAD, ENGINEER, ENGINEERING_LEAD, SAFETY_REVIEWER},
    "hub.view_all": {HUB_TECHNICIAN, OPS_LEAD, ENGINEER, ENGINEERING_LEAD, SAFETY_REVIEWER},
    # Any work-order write; the command then checks the SPECIFIC task (owner, transition) for non-managers.
    "hub.work": {HUB_TECHNICIAN, FIELD_OPERATOR, OPS_LEAD, ENGINEER, ENGINEERING_LEAD},
    # Manage every task: assign to anyone, start / complete / cancel any.
    "hub.manage_all": {OPS_LEAD, ENGINEER, ENGINEERING_LEAD},
    # Take an unowned task (assign it to yourself) and work tasks assigned to you.
    "hub.claim": {HUB_TECHNICIAN},
    # v2 02B charging (8.6): record a charging session or battery reading; reserve a charger; set up chargers.
    "hub.record_charging": {HUB_TECHNICIAN, FIELD_OPERATOR, OPS_LEAD, ENGINEER},
    "hub.reserve_charger": {HUB_TECHNICIAN, OPS_LEAD},
    "hub.manage_chargers": {ADMIN, OPS_LEAD},  # configuration (hub setup records chargers), not evidence
    # v2 02D kit inventory (8.6): stock receive, issue, return and consume; adjustment with a reason
    # (audited); reserving stock for a visit. The SECURITY DEFINER functions of hub/0008 re-check the same
    # roles. Items are hub configuration (like chargers): setting one up records a fact, never stock.
    "hub.stock_move": {HUB_TECHNICIAN, OPS_LEAD},
    "hub.stock_adjust": {HUB_TECHNICIAN, OPS_LEAD},
    "hub.stock_reserve": {HUB_TECHNICIAN, OPS_LEAD},
    "hub.manage_stock_items": {ADMIN, OPS_LEAD},
    # The Hub navigation tab: the queue roles plus everyone who reads the daily close-out.
    "hub.nav": {
        HUB_TECHNICIAN,
        FIELD_OPERATOR,
        OPS_LEAD,
        ENGINEER,
        ENGINEERING_LEAD,
        SAFETY_REVIEWER,
        FOUNDER,
    },
}

# Roles that may sign an inspection as performer or verifier.
INSPECTION_SIGNER_ROLES = {OPS_LEAD, FIELD_OPERATOR}
