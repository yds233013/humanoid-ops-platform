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
}

# Roles that may sign an inspection as performer or verifier.
INSPECTION_SIGNER_ROLES = {OPS_LEAD, FIELD_OPERATOR}
