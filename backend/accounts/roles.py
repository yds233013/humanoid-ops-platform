"""Roles and the permissions each one carries. Server-side only; the UI just mirrors these."""

ADMIN = "admin"  # manages users and roles; approves nothing operational
OPS_LEAD = "ops_lead"
FIELD_OPERATOR = "field_operator"
ENGINEER = "engineer"
ENGINEERING_LEAD = "engineering_lead"
SAFETY_REVIEWER = "safety_reviewer"
FOUNDER = "founder"

ALL_ROLES = [ADMIN, OPS_LEAD, FIELD_OPERATOR, ENGINEER, ENGINEERING_LEAD, SAFETY_REVIEWER, FOUNDER]

# permission -> roles allowed. Slice 1+ add domain permissions here.
PERMISSIONS: dict[str, set[str]] = {
    "accounts.grant_role": {ADMIN},
    "accounts.revoke_role": {ADMIN},
    # v2 01C: the workspace admin invites people and sets up the workspace (records facts, approves nothing)
    "accounts.create_user": {ADMIN},
    "platform_core.manage_workspace": {ADMIN},
    "platform_core.manage_hubs": {ADMIN},
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
}

# Roles that may sign an inspection as performer or verifier.
INSPECTION_SIGNER_ROLES = {OPS_LEAD, FIELD_OPERATOR}
