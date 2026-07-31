"""
Session Manager — multi-turn conversation state.
=================================================

Keeps track of:
- Current action flow (e.g., halfway through creating a drip campaign)
- Collected params so far
- Conversation history (last 10 message pairs for context)
- Per-user session with TTL (30 min)

DB-backed (table: agent_sessions) so multi-turn state is shared across ALL
gunicorn workers. A previous in-memory dict broke every multi-turn flow in
production: turn 1 lands on worker A, turn 2 on worker B, which never saw the
session and answered "I'm not sure what you'd like to do." See session_models.py.

The public interface (get_or_create / get / delete) is unchanged. Callers mutate
the returned AgentSession in place; the executor calls persist() once at the end
of each request to write those mutations back to the DB.
"""

import time
import uuid
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 30 * 60  # 30 minutes
MAX_HISTORY = 10               # Keep last N message pairs
CLEANUP_INTERVAL = 300         # Purge expired rows at most this often (seconds)


@dataclass
class AgentSession:
    """One user's conversation session (in-memory working copy for a request)."""
    session_id: str
    workspace_id: str
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)

    # Multi-turn state
    current_domain: Optional[str] = None
    current_action: Optional[str] = None
    collected_params: Dict[str, Any] = field(default_factory=dict)
    missing_params: List[str] = field(default_factory=list)
    awaiting_confirmation: bool = False

    # Conversation history  [{role: "user"/"agent", text: "..."}]
    history: List[Dict[str, str]] = field(default_factory=list)

    def touch(self):
        self.last_active = time.time()

    def is_expired(self) -> bool:
        return (time.time() - self.last_active) > SESSION_TTL_SECONDS

    def add_message(self, role: str, text: str):
        self.history.append({"role": role, "text": text})
        # Trim to keep last MAX_HISTORY pairs (2 * MAX_HISTORY entries)
        max_entries = MAX_HISTORY * 2
        if len(self.history) > max_entries:
            self.history = self.history[-max_entries:]

    def clear_action_state(self):
        """Reset multi-turn action state (after completion or cancel)."""
        self.current_domain = None
        self.current_action = None
        self.collected_params = {}
        self.missing_params = []
        self.awaiting_confirmation = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "current_domain": self.current_domain,
            "current_action": self.current_action,
            "collected_params": self.collected_params,
            "missing_params": self.missing_params,
            "awaiting_confirmation": self.awaiting_confirmation,
            "history_length": len(self.history),
        }


class SessionManager:
    """DB-backed session store with TTL, shared across gunicorn workers."""

    _instance: Optional["SessionManager"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._last_purge = 0.0
        return cls._instance

    _last_purge: float

    # ---- Hydration helpers -------------------------------------------------

    @staticmethod
    def _hydrate(row) -> AgentSession:
        """Build a working AgentSession from a DB row."""
        s = AgentSession(session_id=row.session_id, workspace_id=row.workspace_id or "")
        s.current_domain = row.current_domain
        s.current_action = row.current_action
        s.collected_params = dict(row.collected_params or {})
        s.missing_params = list(row.missing_params or [])
        s.awaiting_confirmation = bool(row.awaiting_confirmation)
        s.history = list(row.history or [])
        return s

    @staticmethod
    def _row_expired(row) -> bool:
        last = row.last_active or row.created_at or datetime.utcnow()
        return (datetime.utcnow() - last) > timedelta(seconds=SESSION_TTL_SECONDS)

    # ---- Public API --------------------------------------------------------

    def get_or_create(self, session_id: Optional[str], workspace_id: str) -> AgentSession:
        """Return existing session (if valid) or a fresh one.

        A brand-new session is NOT written to the DB here — the executor calls
        persist() at the end of the request, which upserts it. That single write
        is what makes the session visible to the other workers on the next turn.
        """
        from models import db
        from .session_models import AgentSessionState

        self._maybe_purge()

        if session_id:
            try:
                row = AgentSessionState.query.get(session_id)
            except Exception as exc:
                db.session.rollback()
                logger.warning("AgentSession lookup failed for %s: %s", session_id, exc)
                row = None

            if row is not None:
                if not self._row_expired(row):
                    return self._hydrate(row)
                # Expired — drop it and fall through to a fresh session.
                try:
                    db.session.delete(row)
                    db.session.commit()
                except Exception:
                    db.session.rollback()

        new_id = session_id or str(uuid.uuid4())
        return AgentSession(session_id=new_id, workspace_id=workspace_id or "")

    def persist(self, session: AgentSession) -> None:
        """Upsert the working session back to the DB. Called once per request."""
        from models import db
        from .session_models import AgentSessionState

        try:
            row = AgentSessionState.query.get(session.session_id)
            if row is None:
                row = AgentSessionState(session_id=session.session_id)
                db.session.add(row)
            row.workspace_id = str(session.workspace_id) if session.workspace_id else None
            row.current_domain = session.current_domain
            row.current_action = session.current_action
            row.collected_params = session.collected_params or {}
            row.missing_params = session.missing_params or []
            row.awaiting_confirmation = bool(session.awaiting_confirmation)
            row.history = session.history or []
            row.last_active = datetime.utcnow()
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.warning("AgentSession persist failed for %s: %s", session.session_id, exc)

    def get(self, session_id: str) -> Optional[AgentSession]:
        from models import db
        from .session_models import AgentSessionState
        try:
            row = AgentSessionState.query.get(session_id)
        except Exception:
            db.session.rollback()
            return None
        if row and not self._row_expired(row):
            return self._hydrate(row)
        return None

    def delete(self, session_id: str) -> bool:
        from models import db
        from .session_models import AgentSessionState
        try:
            row = AgentSessionState.query.get(session_id)
            if row is not None:
                db.session.delete(row)
                db.session.commit()
                return True
        except Exception as exc:
            db.session.rollback()
            logger.warning("AgentSession delete failed for %s: %s", session_id, exc)
        return False

    # ---- Cleanup -----------------------------------------------------------

    def _maybe_purge(self) -> None:
        """Bulk-delete expired rows, throttled to at most once per CLEANUP_INTERVAL.

        Runs inside a request (app context available), so no background thread is
        needed — that also avoids each of the 3 workers spawning its own thread.
        """
        now = time.time()
        if (now - self._last_purge) < CLEANUP_INTERVAL:
            return
        self._last_purge = now

        from models import db
        from .session_models import AgentSessionState
        try:
            cutoff = datetime.utcnow() - timedelta(seconds=SESSION_TTL_SECONDS)
            deleted = AgentSessionState.query.filter(
                AgentSessionState.last_active < cutoff
            ).delete(synchronize_session=False)
            db.session.commit()
            if deleted:
                logger.debug("Purged %d expired agent sessions", deleted)
        except Exception as exc:
            db.session.rollback()
            logger.debug("Agent session purge skipped: %s", exc)


# Module-level singleton
session_manager = SessionManager()
