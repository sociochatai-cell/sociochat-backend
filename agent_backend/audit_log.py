"""
Agent Audit Log — permanent record of every AI agent interaction.
================================================================

Stores every user message, AI response, action taken, and result
per workspace. Never auto-deleted. Used for client accountability
and debugging.
"""

from datetime import datetime

from models import db


class AgentAuditLog(db.Model):
    __tablename__ = "agent_audit_logs"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    workspace_id = db.Column(db.String(64), nullable=False, index=True)
    account_id = db.Column(db.Integer, nullable=True)
    user_id = db.Column(db.String(64), nullable=True, index=True)
    session_id = db.Column(db.String(64), nullable=True, index=True)

    user_message = db.Column(db.Text, nullable=False)
    agent_response = db.Column(db.Text, nullable=True)

    intent_domain = db.Column(db.String(64), nullable=True)
    intent_action = db.Column(db.String(64), nullable=True)
    intent_confidence = db.Column(db.Float, nullable=True)
    params = db.Column(db.JSON, nullable=True)

    result_status = db.Column(db.String(32), nullable=True)
    error_message = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "user_message": self.user_message,
            "agent_response": self.agent_response,
            "intent_domain": self.intent_domain,
            "intent_action": self.intent_action,
            "intent_confidence": self.intent_confidence,
            "params": self.params,
            "result_status": self.result_status,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def log_interaction(
    workspace_id, user_message, agent_response=None,
    intent_domain=None, intent_action=None, intent_confidence=None,
    params=None, result_status=None, error_message=None,
    account_id=None, user_id=None, session_id=None,
):
    try:
        entry = AgentAuditLog(
            workspace_id=str(workspace_id),
            account_id=account_id,
            user_id=str(user_id) if user_id else None,
            session_id=session_id,
            user_message=user_message[:4000] if user_message else "",
            agent_response=agent_response[:4000] if agent_response else None,
            intent_domain=intent_domain,
            intent_action=intent_action,
            intent_confidence=intent_confidence,
            params=params,
            result_status=result_status,
            error_message=str(error_message)[:2000] if error_message else None,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()
