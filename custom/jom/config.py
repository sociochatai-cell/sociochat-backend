"""
JOM vertical config — cadences, thresholds, status vocabulary. Central so the
follow-up days / qualify thresholds can be tuned in one place during build & QA.
"""
import os

# Follow-up cadences (days after the status is set) — CLIENT-CONFIRMED 2026-09-24.
# Ghosted = stopped replying after a quotation. Dormant = future / undecided traveller.
GHOSTED_DAYS = [7, 15]              # Day 7 check-in, Day 15 exit
DORMANT_DAYS = [7, 14, 24, 31, 45]  # tip / tip / budget / soft check-in / close

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

# Qualify-by-engagement: by this many customer messages in the CURRENT episode we
# should know if they're interested. Past it with no buyer signal + low engagement
# → treat as a stall (Dormant) rather than keep interrogating.
QUALIFY_MAX_TURNS = 5
STALL_ENGAGEMENT_MAX = 2       # engagement_score at/below this counts as "not really engaging"

# Clear disinterest in free text → CLOSE STRAIGHT AWAY (client: don't finish the cycle).
NEGATIVE_KEYWORDS = [
    "not interested", "no interest", "not planning", "no longer planning",
    "already booked", "booked already", "booked with", "cancel", "cancelled",
    "too expensive", "out of budget", "stop", "unsubscribe", "don't message",
    "dont message", "do not message", "not required", "no need", "leave me",
]

# Interactive follow-up buttons → lifecycle action. WhatsApp templates allow max 3
# quick-reply buttons, so the client's 5 options collapse onto the 3 real outcomes.
ACTION_AGENT = "agent"       # back to the (sticky) human agent
ACTION_DORMANT = "dormant"   # keep nurturing, drip continues
ACTION_CLOSE = "close"       # stop everything now
BUTTON_ACTIONS = {
    # payload/id           -> action
    "jom_rework": ACTION_AGENT,
    "jom_budget": ACTION_AGENT,
    "jom_more_time": ACTION_DORMANT,
    "jom_still_planning": ACTION_DORMANT,
    "jom_booked": ACTION_CLOSE,
    "jom_not_planning": ACTION_CLOSE,
    "jom_cancelled": ACTION_CLOSE,
}
# Fallback matching on the visible button TEXT (in case payload ids differ once the
# templates come back approved from Meta).
BUTTON_TEXT_ACTIONS = [
    ("rework", ACTION_AGENT), ("budget", ACTION_AGENT), ("price", ACTION_AGENT),
    ("more time", ACTION_DORMANT), ("need time", ACTION_DORMANT),
    ("still planning", ACTION_DORMANT), ("postpone", ACTION_DORMANT),
    ("booked", ACTION_CLOSE), ("not planning", ACTION_CLOSE), ("cancel", ACTION_CLOSE),
]

# Close reasons (stored on the lifecycle row for CRM filtering/reporting)
CLOSE_REASON_NOT_INTERESTED = "not_interested"
CLOSE_REASON_BOOKED_ELSEWHERE = "booked_elsewhere"
CLOSE_REASON_DRIP_EXHAUSTED = "drip_exhausted"

# Meta template names (created on JOM's WhatsApp account; used outside the 24h window)
TPL_OPENING = "jom_opening_enquiry"
TPL_CONTINUE = "jom_continue_conversation"   # dynamic {{2}} = AI-written context line
TPL_GHOSTED_CHECKIN = "jom_ghosted_checkin"
TPL_GHOSTED_EXIT = "jom_ghosted_exit"
TPL_DORMANT_CHECKIN = "jom_dormant_checkin"  # reused Day 7/14/24/31 via {{3}} tip
TPL_CLOSE = "jom_enquiry_close"

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
# Client-approved copy (2026-09-24). Used verbatim INSIDE the 24h window; outside it the
# same content goes out as the Meta-approved TEMPLATES above. Lengths MUST match the
# cadences: GHOSTED_MESSAGES == len(GHOSTED_DAYS), DORMANT_MESSAGES == len(DORMANT_DAYS).
GHOSTED_MESSAGES = [
    # Day 7 — personal check-in + the 3 outcome buttons
    ("We were genuinely hoping to make your trip something you'd really look forward to, "
     "so I wanted to check in personally. If something didn't feel right with the plan we "
     "shared, please tell us — we can rework the itinerary, adjust hotels, or work around "
     "your budget. Where do things stand?"),
    # Day 15 — exit
    ("We completely understand that holiday plans take time — no pressure at all from our side. "
     "We'll leave your plans with you for now. Whenever you're ready, just message us and we'll "
     "pick up where we left off. 🌴 Journey Of Miles is always just a message away. ✈️"),
]
# The per-step "tip" that varies across the Dormant drip (goes into template var {{3}}).
DORMANT_MESSAGES = [
    "Just a small tip — shifting your travel dates by a few days can make a noticeable difference in hotel prices. 🌴",
    "One thing we always recommend — don't try to cover too much. A balanced itinerary makes the holiday far more enjoyable. 😊",
    "If your dates, budget or requirements have changed, we can always rework the plan — you don't have to start from scratch.",
    "Just keeping your enquiry open from our side — no pressure at all. 🌴",
    "Since we haven't heard back, we'll close your enquiry for now — absolutely no pressure. Whenever you're ready, just message us. ✈️",
]
CLOSER_MESSAGE = ("We're closing this enquiry for now so we don't disturb you. "
                  "Whenever you're ready to travel, just message us — we're always here to help. 🙏")


def followup_text(status, stage):
    """Free-text body for a follow-up (used inside the 24h window).
    status = ghosted/dormant, stage = 0-based step; stage < 0 = the closer."""
    if stage is not None and stage < 0:
        return CLOSER_MESSAGE
    msgs = GHOSTED_MESSAGES if status == STATUS_GHOSTED else DORMANT_MESSAGES
    try:
        return msgs[int(stage)]
    except (TypeError, ValueError, IndexError):
        return msgs[-1]


def followup_template(status, stage):
    """Which Meta template to use for a follow-up step (used OUTSIDE the 24h window).
    Returns (template_name, uses_tip_variable)."""
    if stage is not None and stage < 0:
        return TPL_CLOSE, False
    if status == STATUS_GHOSTED:
        # last ghosted step is the exit message, earlier steps are the check-in
        last = len(GHOSTED_DAYS) - 1
        return (TPL_GHOSTED_EXIT, False) if int(stage or 0) >= last else (TPL_GHOSTED_CHECKIN, False)
    # dormant: final step closes, the rest reuse ONE template with a varying tip
    last = len(DORMANT_DAYS) - 1
    return (TPL_CLOSE, False) if int(stage or 0) >= last else (TPL_DORMANT_CHECKIN, True)
