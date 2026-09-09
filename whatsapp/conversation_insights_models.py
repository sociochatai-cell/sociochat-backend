"""Models for AI Conversation Insights — per-chat summary of what the
Advanced AI Agent learned and did during a conversation."""

from datetime import datetime, timezone
from typing import Any, Dict

from models import db


class ConversationInsight(db.Model):
    __tablename__ = "conversation_insights"
    __table_args__ = {"extend_existing": True}

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, nullable=False, index=True)
    conversation_id = db.Column(
        db.Integer,
        db.ForeignKey("whatsapp_conversations.id"),
        nullable=False,
        index=True,
    )
    customer_name = db.Column(db.String(128), nullable=True)
    customer_email = db.Column(db.String(255), nullable=True)
    customer_phone = db.Column(db.String(32), nullable=True)
    customer_company = db.Column(db.String(128), nullable=True)
    customer_language = db.Column(db.String(32), nullable=True)
    interests = db.Column(db.Text, nullable=True)
    summary = db.Column(db.Text, nullable=True)
    actions_taken = db.Column(db.JSON, nullable=True)
    tool_calls = db.Column(db.JSON, nullable=True)
    key_topics = db.Column(db.Text, nullable=True)
    sentiment = db.Column(db.String(32), nullable=True)
    interaction_count = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "conversation_id": self.conversation_id,
            "customer_name": self.customer_name,
            "customer_email": self.customer_email,
            "customer_phone": self.customer_phone,
            "customer_company": self.customer_company,
            "customer_language": self.customer_language,
            "interests": self.interests,
            "summary": self.summary,
            "actions_taken": self.actions_taken,
            "tool_calls": self.tool_calls,
            "key_topics": self.key_topics,
            "sentiment": self.sentiment,
            "interaction_count": self.interaction_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
