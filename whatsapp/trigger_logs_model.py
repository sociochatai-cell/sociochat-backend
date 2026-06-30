"""
Trigger Logs Model - Track all trigger invocations for debugging and analytics.
"""
from datetime import datetime, timezone
from shared_models import db
from sqlalchemy import func, Index


class TriggerLog(db.Model):
    """
    Logs every trigger invocation for debugging, analytics, and auditing.
    Tracks success/failure, variables used, and response from WhatsApp API.
    """
    __tablename__ = "whatsapp_trigger_logs"
    
    id = db.Column(db.Integer, primary_key=True)
    trigger_id = db.Column(db.Integer, db.ForeignKey("whatsapp_triggers.id", ondelete="CASCADE"), nullable=False, index=True)
    workspace_id = db.Column(db.String(255), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey("whatsapp_accounts.id", ondelete="CASCADE"), nullable=False)
    
    # Recipient info
    recipient_phone = db.Column(db.String(32), nullable=False, index=True)
    
    # Variables sent
    variables_json = db.Column(db.Text)  # JSON array of variables
    
    # Request source info
    source_ip = db.Column(db.String(64))
    source_type = db.Column(db.String(32))  # api, webhook, bulk, zapier, shopify, etc.
    source_reference = db.Column(db.String(255))  # External reference (e.g., order_id)
    
    # WhatsApp API response
    success = db.Column(db.Boolean, nullable=False, default=False)
    message_id = db.Column(db.String(128))  # WhatsApp message ID if success
    error_message = db.Column(db.Text)  # Error details if failed
    
    # Delivery status (updated via webhook)
    delivery_status = db.Column(db.String(32), default="sent")  # sent, delivered, read, failed
    delivered_at = db.Column(db.DateTime(timezone=True))
    read_at = db.Column(db.DateTime(timezone=True))
    
    # Timestamps
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())
    
    __table_args__ = (
        Index("ix_trigger_logs_created", "created_at"),
        Index("ix_trigger_logs_trigger_created", "trigger_id", "created_at"),
        Index("ix_trigger_logs_success", "success"),
        {"extend_existing": True},
    )
    
    def to_dict(self):
        import json
        return {
            "id": self.id,
            "trigger_id": self.trigger_id,
            "workspace_id": self.workspace_id,
            "account_id": self.account_id,
            "recipient_phone": self.recipient_phone,
            "variables": json.loads(self.variables_json) if self.variables_json else [],
            "source_ip": self.source_ip,
            "source_type": self.source_type,
            "source_reference": self.source_reference,
            "success": self.success,
            "message_id": self.message_id,
            "error_message": self.error_message,
            "delivery_status": self.delivery_status,
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class TriggerContact(db.Model):
    """
    Contacts database for triggers - stores phone numbers and their variables.
    Allows importing contacts from CSV or CRM for bulk sending.
    """
    __tablename__ = "whatsapp_trigger_contacts"
    
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.String(255), nullable=False, index=True)
    
    # Contact info
    phone = db.Column(db.String(32), nullable=False)
    name = db.Column(db.String(255))
    email = db.Column(db.String(255))
    
    # Custom variables as JSON
    # Example: {"customer_name": "John", "order_id": "12345", "amount": "$99"}
    custom_fields = db.Column(db.Text)  # JSON object
    
    # Grouping/tagging
    tags = db.Column(db.Text)  # JSON array of tags for filtering
    list_name = db.Column(db.String(255), index=True)  # For organizing contacts
    
    # Status
    is_active = db.Column(db.Boolean, default=True)
    opted_out = db.Column(db.Boolean, default=False)
    opted_out_at = db.Column(db.DateTime(timezone=True))
    
    # Source tracking
    source = db.Column(db.String(64))  # csv_import, manual, crm_sync, shopify, etc.
    external_id = db.Column(db.String(255))  # ID from external system
    
    # Timestamps
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=func.now())
    
    __table_args__ = (
        Index("ix_trigger_contacts_phone_workspace", "workspace_id", "phone"),
        Index("ix_trigger_contacts_list", "workspace_id", "list_name"),
        {"extend_existing": True},
    )
    
    def to_dict(self):
        import json
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "phone": self.phone,
            "name": self.name,
            "email": self.email,
            "custom_fields": json.loads(self.custom_fields) if self.custom_fields else {},
            "tags": json.loads(self.tags) if self.tags else [],
            "list_name": self.list_name,
            "is_active": self.is_active,
            "opted_out": self.opted_out,
            "source": self.source,
            "external_id": self.external_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
    
    def get_variable(self, key: str, default=None):
        """Get a custom field value by key."""
        import json
        fields = json.loads(self.custom_fields) if self.custom_fields else {}
        return fields.get(key, default)
