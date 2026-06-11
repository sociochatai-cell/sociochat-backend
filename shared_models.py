"""
Shared models bridge for WhatsApp cross-cutting tables.
Re-exports core auth models for subscription/billing modules.
"""

from datetime import datetime, timezone

from models import db, User, Workspace, Admin, AIUsage, AIUsageDailySummary  # noqa: F401


class WhatsAppLinkTracking(db.Model):
    """Click-to-chat and bulk link tracking records."""

    __tablename__ = "whatsapp_link_tracking"

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.String(255), nullable=False, index=True)
    account_id = db.Column(db.Integer, nullable=True, index=True)
    source = db.Column(db.String(64), nullable=False, default="bulk", index=True)
    source_type = db.Column(db.String(64), nullable=False, default="click_to_chat")
    tracking_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    phone_number = db.Column(db.String(32), nullable=False, index=True)
    name = db.Column(db.String(255), nullable=True)
    template_name = db.Column(db.String(255), nullable=True)
    campaign_name = db.Column(db.String(255), nullable=True)
    target_url = db.Column(db.Text, nullable=False)
    wamid = db.Column(db.String(128), nullable=True)
    utm_source = db.Column(db.String(128), nullable=True)
    utm_campaign = db.Column(db.String(255), nullable=True)
    click_count = db.Column(db.Integer, default=0, nullable=False)
    first_clicked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_clicked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    def to_tracking_row(self) -> dict:
        return {
            "id": self.id,
            "tracking_id": self.tracking_id,
            "workspace_id": self.workspace_id,
            "account_id": self.account_id,
            "source": self.source,
            "source_type": self.source_type,
            "phone_number": self.phone_number,
            "name": self.name,
            "template_name": self.template_name,
            "campaign_name": self.campaign_name,
            "target_url": self.target_url,
            "click_count": self.click_count or 0,
            "first_clicked_at": self.first_clicked_at.isoformat() if self.first_clicked_at else None,
            "last_clicked_at": self.last_clicked_at.isoformat() if self.last_clicked_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
