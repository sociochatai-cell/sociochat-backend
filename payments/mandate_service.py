"""
PayU SI mandate state machine + scheduling helpers.

Centralising the allowed transitions here prevents a bug elsewhere from, e.g.,
flipping a cancelled mandate back to active and silently charging the user
again. Callers commit atomically with whatever triggered the transition.
"""

import logging
from datetime import datetime, timezone

from .mandate_models import (
    PayuMandate, MANDATE_STATUSES,
    MANDATE_PENDING, MANDATE_ACTIVE, MANDATE_PAUSED,
    MANDATE_CANCELLED, MANDATE_FAILED, MANDATE_EXPIRED,
)

logger = logging.getLogger(__name__)

# from -> allowed next states
ALLOWED_TRANSITIONS = {
    MANDATE_PENDING:   {MANDATE_ACTIVE, MANDATE_FAILED, MANDATE_CANCELLED},
    MANDATE_ACTIVE:    {MANDATE_PAUSED, MANDATE_CANCELLED, MANDATE_FAILED, MANDATE_EXPIRED},
    MANDATE_PAUSED:    {MANDATE_ACTIVE, MANDATE_CANCELLED, MANDATE_FAILED, MANDATE_EXPIRED},
    MANDATE_CANCELLED: set(),   # terminal
    MANDATE_FAILED:    set(),   # terminal
    MANDATE_EXPIRED:   set(),   # terminal
}

# 3 consecutive charge failures retire the mandate.
MAX_CONSECUTIVE_FAILURES = 3


class InvalidMandateTransition(ValueError):
    pass


def transition(mandate: PayuMandate, new_status: str, **fields) -> PayuMandate:
    """Move ``mandate`` to ``new_status`` (enforcing ALLOWED_TRANSITIONS) and set
    any extra column values. Does NOT commit. Raises on an illegal move."""
    if new_status not in MANDATE_STATUSES:
        raise InvalidMandateTransition(f"unknown mandate status '{new_status}'")
    current = getattr(mandate, "status", None)
    if current != new_status:
        allowed = ALLOWED_TRANSITIONS.get(current, set())
        if new_status not in allowed:
            raise InvalidMandateTransition(
                f"mandate {getattr(mandate, 'id', '?')}: illegal transition {current} -> {new_status}"
            )
        mandate.status = new_status
    for field, value in fields.items():
        setattr(mandate, field, value)
    logger.info("mandate %s: %s -> %s", getattr(mandate, "id", "?"), current, new_status)
    return mandate


def add_billing_period(start: datetime, period: str):
    """Next charge date for a billing period ('monthly'|'yearly'|'quarterly')."""
    from dateutil.relativedelta import relativedelta  # available in the venv
    p = (period or "monthly").lower()
    months = {"monthly": 1, "quarterly": 3, "yearly": 12, "annual": 12}.get(p, 1)
    return start + relativedelta(months=months)


def mandate_end_date(start: datetime, years: int = 5):
    """Mandate validity window end (PayU requires an end date; use a long window)."""
    from dateutil.relativedelta import relativedelta
    return start + relativedelta(years=years)
