"""
Phase 5 — Cross-campaign junk filter (JOM vertical).

A contact is "junk" when we've messaged them repeatedly (across campaigns) and they
have NEVER replied. We read the per-contact behaviour score (total_outbound vs
total_inbound / consecutive_no_reply) and expose helpers to list + filter them out of
the next bulk send, protecting the number's quality rating.
All workspace-scoped; call behind is_jom_workspace().
"""
import logging

from .models import JomBehaviorScore
from . import config as cfg

logger = logging.getLogger(__name__)

DEFAULT_MIN_ATTEMPTS = 3   # messaged at least this many times ...
# ... with zero inbound replies → junk.


def is_junk(workspace_id, customer_phone, min_attempts=DEFAULT_MIN_ATTEMPTS):
    s = JomBehaviorScore.query.filter_by(
        workspace_id=int(workspace_id), customer_phone=str(customer_phone)).first()
    if not s:
        return False
    return bool((s.total_outbound or 0) >= min_attempts and (s.total_inbound or 0) == 0)


def list_junk(workspace_id, min_attempts=DEFAULT_MIN_ATTEMPTS):
    rows = JomBehaviorScore.query.filter(
        JomBehaviorScore.workspace_id == int(workspace_id),
        JomBehaviorScore.total_outbound >= min_attempts,
        JomBehaviorScore.total_inbound == 0,
    ).all()
    return [r.customer_phone for r in rows]


def filter_recipients(workspace_id, phones, min_attempts=DEFAULT_MIN_ATTEMPTS):
    """Given a candidate bulk-send list, return (keep, dropped) — dropping junk phones."""
    junk = set(list_junk(workspace_id, min_attempts))
    keep = [p for p in phones if str(p) not in junk]
    dropped = [p for p in phones if str(p) in junk]
    logger.info("[jom] junk filter ws=%s kept=%d dropped=%d", workspace_id, len(keep), len(dropped))
    return keep, dropped
