"""
Lead Scoring - Automatic, idempotent lead quality score
=======================================================

Derives a 0-100 quality score for a CRM Lead purely from the lead's CURRENT
row state. Because the score is a pure function of the stored fields, it can be
recomputed at any time, as often as you like, with no double-counting and no
drift -- recomputing always yields the same number for the same lead state.

How the score is built (see DEFAULT_SCORE_WEIGHTS):
    +20  phone    -> the lead has a phone number (reachable on WhatsApp)
    +15  email    -> the lead has an email address
    +10  name     -> the lead has a real name (not just its phone number echoed
                     back as the name)
    +15  engaged  -> the lead replied / moved forward: status is one of
                     contacted / qualified / proposal / closed
    +25  intent   -> the lead showed buying intent: status is one of
                     qualified / proposal / closed
    +15  ad       -> the lead came from a click-to-WhatsApp ad (details JSON has
                     an ad_id / ctwa_clid, or its source / entry_source looks
                     like a CTWA ad)

The total is clamped to the inclusive range [0, 100].

Because higher lifecycle statuses imply the lower ones (a qualified lead has
also engaged), the score naturally rises as the lead progresses -- it "auto
updates" from lead state without any incremental bookkeeping.

Design contract (mirrors lead_ingest.py):
    - compute_lead_score is PURE and NEVER raises. On any unexpected error it
      falls back to the lead's currently stored score (or 0).
    - recompute_and_save_lead_score persists the recomputed score and NEVER
      raises. On any error it rolls back and returns the lead's existing score
      (or 0).
    - Flask is imported lazily (``from flask import current_app``) only where a
      session is actually needed, so the pure scoring path has no Flask / app
      context dependency.
"""

import logging

logger = logging.getLogger(__name__)

# Point values awarded for each positive signal. Tunable in one place; the sum
# of all weights is 100 so a "perfect" lead scores exactly 100.
DEFAULT_SCORE_WEIGHTS = {
    "phone": 20,    # has a phone number
    "email": 15,    # has an email
    "name": 10,     # has a real name (not just the phone number)
    "engaged": 15,  # replied -> status is contacted or further
    "intent": 25,   # showed buying intent -> status qualified or further
    "ad": 15,       # came from a click-to-WhatsApp ad
}

# Statuses that mean the lead has engaged (replied / moved past "new").
_ENGAGED_STATUSES = {"contacted", "qualified", "proposal", "closed"}
# Statuses that mean the lead has shown buying intent.
_INTENT_STATUSES = {"qualified", "proposal", "closed"}

# Source / entry-source values that indicate a click-to-WhatsApp ad origin.
_AD_SOURCE_VALUES = {"ctwa", "ad", "click_to_whatsapp"}


def _came_from_ad(lead) -> bool:
    """
    Best-effort detection of a click-to-WhatsApp ad origin.

    Tolerant of missing / odd data: ``details`` may be None, a dict, or even a
    non-dict. Never raises -- returns False on anything unexpected.
    """
    try:
        details = getattr(lead, "details", None)
        if isinstance(details, dict):
            # Direct ad attribution stored on the lead's details JSON.
            if details.get("ad_id") or details.get("ctwa_clid"):
                return True
            # entry_source captured at ingest time may itself name the ad source.
            entry_source = str(details.get("entry_source") or "").lower()
            if entry_source in _AD_SOURCE_VALUES or "ctwa" in entry_source:
                return True

        # Fall back to the lead's own source column.
        source = str(getattr(lead, "source", "") or "").lower()
        if source in _AD_SOURCE_VALUES or "ctwa" in source:
            return True
    except Exception:
        logger.debug("_came_from_ad: ad detection failed, defaulting to False", exc_info=True)

    return False


def compute_lead_score(lead) -> int:
    """Pure: derive a 0-100 score from the lead's current row. Never raises."""
    try:
        weights = DEFAULT_SCORE_WEIGHTS
        total = 0

        phone = getattr(lead, "phone", None)
        email = getattr(lead, "email", None)
        name = getattr(lead, "name", None)
        status = str(getattr(lead, "status", "") or "")

        # --- Contactability signals ---
        if phone:
            total += weights["phone"]
        if email:
            total += weights["email"]
        # A "real" name is one that isn't just the phone number echoed back.
        if name and str(name).strip() != str(phone or "").strip():
            total += weights["name"]

        # --- Lifecycle signals (intent implies engagement, so both can add) ---
        if status in _ENGAGED_STATUSES:
            total += weights["engaged"]
        if status in _INTENT_STATUSES:
            total += weights["intent"]

        # --- Acquisition signal ---
        if _came_from_ad(lead):
            total += weights["ad"]

        return min(100, max(0, total))
    except Exception:
        logger.exception("compute_lead_score: unexpected error, falling back to stored score")
        try:
            return getattr(lead, "score", 0) or 0
        except Exception:
            return 0


def recompute_and_save_lead_score(lead, db_session=None) -> int:
    """Recompute the lead's score and persist it. Returns the new score. Never raises."""
    from flask import current_app

    try:
        if lead is None:
            return 0

        session = db_session or current_app.db.session
        new_score = compute_lead_score(lead)

        # Only write the column if the model actually has it.
        try:
            has_score = "score" in {c.key for c in lead.__table__.columns}
        except Exception:
            has_score = hasattr(lead, "score")

        if has_score:
            lead.score = new_score
            session.add(lead)
            session.commit()
            logger.debug(
                "recompute_and_save_lead_score: lead %s scored %s",
                getattr(lead, "id", "?"), new_score,
            )
        else:
            logger.debug(
                "recompute_and_save_lead_score: lead %s has no 'score' column, not persisting (score=%s)",
                getattr(lead, "id", "?"), new_score,
            )

        return new_score
    except Exception:
        logger.exception("recompute_and_save_lead_score: failed to persist score")
        try:
            sess = db_session
            if sess is None:
                from flask import current_app as _ca
                sess = _ca.db.session
            sess.rollback()
        except Exception:
            pass
        try:
            return getattr(lead, "score", 0) or 0
        except Exception:
            return 0
