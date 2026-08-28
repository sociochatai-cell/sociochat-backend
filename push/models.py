"""
Push notification device registry (mobile app only).

A brand-new, self-contained table. It records which mobile device (identified by
its Expo push token) belongs to which user + workspace, so the server can send a
push when a new WhatsApp message arrives.

Nothing else in the schema is touched. The web app never writes here.
"""

from datetime import datetime
from models import db


class PushDevice(db.Model):
    __tablename__ = "push_devices"

    id = db.Column(db.Integer, primary_key=True)
    # Who owns this device (the logged-in mobile user)
    user_id = db.Column(db.Integer, index=True, nullable=False)
    # Which workspace this registration is scoped to (nullable = all/unknown)
    workspace_id = db.Column(db.Integer, index=True, nullable=True)
    # The Expo push token for this physical device (unique per device)
    expo_token = db.Column(db.String(255), unique=True, nullable=False)
    # 'android' | 'ios'
    platform = db.Column(db.String(20), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "platform": self.platform,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
