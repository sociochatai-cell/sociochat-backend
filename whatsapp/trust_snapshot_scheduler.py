"""
Scheduler entrypoints for trust / reputation snapshot jobs (advisory).
"""

from __future__ import annotations

import logging

from .trust_snapshot_engine import run_trust_snapshot_batch, trust_snapshots_enabled

logger = logging.getLogger(__name__)


def run_daily_trust_snapshots(
    *,
    limit: int = 500,
    include_inactive: bool = False,
    force: bool = False,
) -> dict:
    """Entry point for Cloud Scheduler (daily)."""
    if not trust_snapshots_enabled():
        return {"status": "disabled"}
    logger.info("trust_daily_snapshot_job starting limit=%s force=%s", limit, force)
    out = run_trust_snapshot_batch(limit=limit, include_inactive=include_inactive, force=force)
    logger.info("trust_daily_snapshot_job finished %s", out)
    return out
