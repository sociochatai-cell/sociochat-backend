"""
JOM vertical config — cadences, thresholds, status vocabulary. Central so the
follow-up days / qualify thresholds can be tuned in one place during build & QA.
"""
import os

# Follow-up cadences (days after the status is set)
GHOSTED_DAYS = [1, 5, 15]      # stopped replying after quotation
DORMANT_DAYS = [15, 30, 45]    # future / undecided traveller

# Qualification thresholds (Phase 2)
QUALIFY_INTENT = "sales"       # kept for backward-compat / reference
# Buyer intents that qualify a lead. Travel enquiries are often labelled
# "appointment" or "info" by the shared classifier (not "sales"), so we accept
# those too. Env-overridable (comma-separated) without a code change.
QUALIFY_INTENTS = {
    i.strip().lower()
    for i in os.getenv("JOM_QUALIFY_INTENTS", "sales,appointment,info").split(",")
    if i.strip()
}
QUALIFY_CONFIDENCE = 0.60      # min confidence from classify_intent
GHOSTED_AFTER_NO_REPLY = 3     # consecutive no-replies before auto-Ghosted (Phase 3 v2)

# Lifecycle status vocabulary
STATUS_NEW       = "new"
STATUS_CONTACTED = "contacted"
STATUS_QUALIFIED = "qualified"
STATUS_ASSIGNED  = "assigned"
STATUS_GHOSTED   = "ghosted"
STATUS_DORMANT   = "dormant"
STATUS_CLOSED    = "closed"
STATUS_ACTIVE    = "active"

ROUTING_ROUND_ROBIN = "round_robin"
ROUTING_MANUAL      = "manual"

# Follow-up message wording (PLACEHOLDER — client provides final copy; day-5/15 must be
# APPROVED WhatsApp templates in production since they fall outside the 24h session).
GHOSTED_MESSAGES = [
    "Hi! Just following up on your travel enquiry — are you still planning the trip? We'd love to help. 😊",
    "Hello again! We're still holding your options. Shall we share the best packages for your dates?",
    "Last check-in from our side — if you're still interested, reply here and we'll pick right up. Otherwise we'll close this for now.",
]
DORMANT_MESSAGES = [
    "Hi! Hope your travel plans are shaping up. Whenever you're ready, we're here to help plan it. ✈️",
    "Checking in — any update on your travel dates? Happy to re-share options tailored for you.",
    "Final follow-up — if your plan is still on, just reply and we'll continue. Otherwise we'll close this for now.",
]
CLOSER_MESSAGE = ("We're closing this enquiry for now so we don't disturb you. "
                  "Whenever you're ready to travel, just message us — we're always here to help. 🙏")


def followup_text(status, stage):
    """Message text for a follow-up (status = ghosted/dormant, stage = 0/1/2) or the closer (stage < 0)."""
    if stage is not None and stage < 0:
        return CLOSER_MESSAGE
    msgs = GHOSTED_MESSAGES if status == STATUS_GHOSTED else DORMANT_MESSAGES
    try:
        return msgs[int(stage)]
    except (TypeError, ValueError, IndexError):
        return msgs[-1]
