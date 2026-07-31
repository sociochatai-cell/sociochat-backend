"""
Agent session persistence model.
=================================

DB-backed store for multi-turn agent conversation state.

Why this exists: production runs gunicorn with multiple workers (see
deploy/sociochat-backend.service, --workers 3). A per-process in-memory dict
cannot survive a request being round-robined to a different worker mid-flow —
turn 1 ("create a template") lands on worker A, turn 2 ("order_confirmation")
on worker B, which never saw the session and treats it as a brand-new message.

This mirrors the existing DB-backed pattern already used for OTP / auto-login
tokens (see models.py) so multi-turn state is shared across all workers.
"""

from datetime import datetime

from models import db


class AgentSessionState(db.Model):
    __tablename__ = "agent_sessions"

    # uuid4 string (36 chars) or a client-supplied id.
    session_id = db.Column(db.String(64), primary_key=True)
    workspace_id = db.Column(db.String(64), nullable=True, index=True)

    # Multi-turn action flow state
    current_domain = db.Column(db.String(64), nullable=True)
    current_action = db.Column(db.String(64), nullable=True)
    collected_params = db.Column(db.JSON, nullable=True)   # dict
    missing_params = db.Column(db.JSON, nullable=True)      # list[str]
    awaiting_confirmation = db.Column(db.Boolean, nullable=False, default=False)

    # Conversation history: [{"role": "user"/"agent", "text": "..."}]
    history = db.Column(db.JSON, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_active = db.Column(db.DateTime, default=datetime.utcnow, index=True)
