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
    "fleet.manage_hold_policy": {ENGINEERING_LEAD},
    "fleet.approve_hold_policy": {ENGINEERING_LEAD, SAFETY_REVIEWER},
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
}

# Roles that may sign an inspection as performer or verifier.
INSPECTION_SIGNER_ROLES = {OPS_LEAD, FIELD_OPERATOR}
