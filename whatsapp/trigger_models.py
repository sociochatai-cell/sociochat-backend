from datetime import datetime, timezone
from shared_models import db
from sqlalchemy import func

class WhatsAppTrigger(db.Model):
    """
    Triggered Messages configuration.
    Allows defining an API endpoint that sends a specific template.
    External systems call this trigger to send messages (e.g. Order Updates).
    """
    __tablename__ = "whatsapp_triggers"
    
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.String(255), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey("whatsapp_accounts.id"), nullable=False, index=True)
    
    name = db.Column(db.String(255), nullable=False)
    slug = db.Column(db.String(255), nullable=False)  # URL-friendly identifier
    description = db.Column(db.Text)
    
    # Template to send
    template_name = db.Column(db.String(255), nullable=False)
    language = db.Column(db.String(10), default="en_US")
    
    # Security
    secret_key = db.Column(db.String(64), nullable=False) # For verifying external calls
    
    # Configuration
    is_active = db.Column(db.Boolean, default=True)
    allow_custom_variables = db.Column(db.Boolean, default=True) # If true, caller can pass params
    
    # Statistics
    trigger_count = db.Column(db.Integer, default=0)
    last_triggered_at = db.Column(db.DateTime(timezone=True))
    
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=func.now())
    
    __table_args__ = (
        db.UniqueConstraint('account_id', 'slug', name='uq_account_trigger_slug'),
        {"extend_existing": True},
    )
    
    def to_dict(self):
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "account_id": self.account_id,
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "template_name": self.template_name,
            "language": self.language,
            "is_active": self.is_active,
            "trigger_count": self.trigger_count,
            "last_triggered_at": self.last_triggered_at.isoformat() if self.last_triggered_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "secret_key": self.secret_key,
            "webhook_url": f"/api/whatsapp/hooks/{self.id}" # Matches route in trigger_routes.py
        }
