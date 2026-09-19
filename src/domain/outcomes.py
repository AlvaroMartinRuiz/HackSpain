"""The closed vocabulary of reasons a call can end without a booking."""

from __future__ import annotations

# The first eleven mirror the clinic's own restrictions one-for-one, so a rule
# that bit can always be reported with the id the API already used.
RESTRICTION_REASONS = (
    "not_eligible_age",
    "referral_required",
    "provider_not_in_network",
    "specialty_not_covered",
    "location_not_covered",
    "insurer_referral_required",
    "allowance_exhausted",
    "provider_on_leave",
    "location_hours",
    "type_not_offered",
    "patient_history",
)

OTHER_REASONS = (
    "no_availability",
    "clinic_closed",
    "patient_not_found",
    "provider_not_found",
    "caller_not_authorised",
    "out_of_scope",
    "medical_emergency",
)

ALL_REASONS = RESTRICTION_REASONS + OTHER_REASONS


def is_valid_reason(reason: str) -> bool:
    return reason in ALL_REASONS


def reason_for_restriction(restriction: str) -> str:
    """A blocked entry's restriction is already the reason to report."""
    return restriction if restriction in RESTRICTION_REASONS else "no_availability"


def pick_blocking_reason(blocked: list[dict[str, str]]) -> str | None:
    """The rule to report when availability came back empty.

    Ordered so a rule about the patient outranks one about a single provider:
    a caller whose plan does not cover the specialty is refused for that, not
    told their doctor is on leave.
    """
    if not blocked:
        return None
    priority = {reason: index for index, reason in enumerate(RESTRICTION_REASONS)}
    restrictions = [entry.get("restriction", "") for entry in blocked]
    ranked = sorted(restrictions, key=lambda r: priority.get(r, len(priority)))
    return reason_for_restriction(ranked[0]) if ranked else None
