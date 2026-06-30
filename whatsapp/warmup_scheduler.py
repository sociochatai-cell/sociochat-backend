"""
Scheduler-side warmup maintenance (non-destructive).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Tuple

from shared_models import db

from .models import WhatsAppAccount
from .warmup_account_ops import complete_warmup_if_due
from .warmup_types import LIFECYCLE_WARMUP

logger = logging.getLogger(__name__)


def tick_warmup_completions(limit: int = 500) -> Tuple[int, int]:
    """
    Mark accounts whose warmup window ended as active.
    Returns (rows_scanned, rows_updated).
    """
    now = datetime.now(timezone.utc)
    q = (
        WhatsAppAccount.query.filter(
            WhatsAppAccount.onboarding_lifecycle_state == LIFECYCLE_WARMUP,
            WhatsAppAccount.warmup_ends_at.isnot(None),
            WhatsAppAccount.warmup_ends_at < now,
        )
        .order_by(WhatsAppAccount.id.asc())
        .limit(limit)
    )
    rows = q.all()
    n = 0
    for acc in rows:
        if complete_warmup_if_due(acc, force=False):
            n += 1
    if n:
        try:
            db.session.commit()
        except Exception as e:
            logger.warning("warmup_scheduler commit: %s", e)
            db.session.rollback()
            return len(rows), 0
    return len(rows), n
