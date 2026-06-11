from datetime import datetime, timezone
from models import db
from sqlalchemy import func

class WhatsAppDripCampaign(db.Model):
    """
    Drip Campaign Metadata.
    """
    __tablename__ = "whatsapp_drip_campaigns"
    
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.String(255), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey("whatsapp_accounts.id"), nullable=False, index=True)
    
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    
    # Trigger conditions
    trigger_type = db.Column(db.String(50), default="manual") # manual, new_subscriber, tag_added, google_sheet_row
    trigger_value = db.Column(db.String(255)) # e.g. tag name
    
    # Google Sheets Integration (for trigger_type='google_sheet_row')
    sheet_id = db.Column(db.Text, nullable=True)  # Google Sheet URL or ID
    sheet_name = db.Column(db.String(255), nullable=True, default="Sheet1")  # Tab name
    phone_column = db.Column(db.String(100), nullable=True, default="phone")  # Column name for phone numbers
    column_mapping = db.Column(db.JSON, nullable=True)  # Maps step_X_Y -> sheet column name
    fallback_values = db.Column(db.JSON, nullable=True)  # Default values for unmapped/missing columns
    last_synced_row = db.Column(db.Integer, default=0)  # Track which row we last processed
    
    status = db.Column(db.String(20), default="draft") # draft, active, paused
    
    # Stats
    enrolled_count = db.Column(db.Integer, default=0)
    completed_count = db.Column(db.Integer, default=0)
    
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=func.now())
    
    steps = db.relationship("WhatsAppDripStep", backref="campaign", cascade="all, delete-orphan", order_by="WhatsAppDripStep.step_order")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "trigger_type": self.trigger_type,
            "sheet_id": self.sheet_id,
            "sheet_name": self.sheet_name,
            "phone_column": self.phone_column,
            "column_mapping": self.column_mapping,
            "fallback_values": self.fallback_values,
            "status": self.status,
            "enrolled_count": self.enrolled_count,
            "completed_count": self.completed_count,
            "steps": [s.to_dict() for s in self.steps],
            "created_at": self.created_at.isoformat() if self.created_at else None
        }

class WhatsAppDripStep(db.Model):
    """
    Step in a Drip Campaign.
    """
    __tablename__ = "whatsapp_drip_steps"
    
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("whatsapp_drip_campaigns.id"), nullable=False)
    
    step_order = db.Column(db.Integer, nullable=False) # 1, 2, 3...
    
    # Delay before this step (from previous step or start)
    delay_seconds = db.Column(db.Integer, default=0)
    
    # Message to send
    template_name = db.Column(db.String(255), nullable=False)
    language = db.Column(db.String(10), default="en_US")
    
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())
    
    def to_dict(self):
        return {
            "id": self.id,
            "step_order": self.step_order,
            "delay_seconds": self.delay_seconds,
            "template_name": self.template_name,
            "language": self.language
        }

class WhatsAppDripEnrollment(db.Model):
    """
    Tracks a user's progress through a drip campaign.
    """
    __tablename__ = "whatsapp_drip_enrollments"
    
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("whatsapp_drip_campaigns.id"), nullable=False)
    
    phone_number = db.Column(db.String(50), nullable=False)  # Normalized phone (e.g. 919390094496)
    phone_original = db.Column(db.String(100), nullable=True)  # Original from sheet for debugging
    
    current_step_order = db.Column(db.Integer, default=0)  # 0 = just started, waiting for step 1
    next_run_at = db.Column(db.DateTime(timezone=True))  # When to run the next step
    
    status = db.Column(db.String(20), default="active")  # active, completed, failed, paused, blocked_missing_data
    status_reason = db.Column(db.Text, nullable=True)  # Detailed error reason if failed/blocked
    
    # Row data from Google Sheets for template parameters
    variables = db.Column(db.JSON, default=dict)  # Store entire row data as JSON
    
    # Debug/audit fields
    variables_source = db.Column(db.String(32), default="google_sheet")  # Where data came from
    variables_row_index = db.Column(db.Integer, nullable=True)  # Which row in sheet
    variables_sheet_id = db.Column(db.String(255), nullable=True)  # Which sheet
    variables_last_synced_at = db.Column(db.DateTime(timezone=True), nullable=True)  # When synced
    
    # Template params sent (for auditing what was actually sent)
    last_sent_params = db.Column(db.JSON, default=dict)  # Snapshot of params sent

    # Link click tracking (bulk / drip with tracked URLs)
    tracking_id = db.Column(db.String(64), nullable=True, index=True)
    clicked = db.Column(db.Boolean, default=False)
    click_count = db.Column(db.Integer, default=0)
    clicked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=func.now())
    
    def to_dict(self):
        return {
            "id": self.id,
            "campaign_id": self.campaign_id,
            "phone_number": self.phone_number,
            "phone_original": self.phone_original,
            "current_step_order": self.current_step_order,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "status": self.status,
            "status_reason": self.status_reason,
            "variables": self.variables or {},
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


class WhatsAppDataset(db.Model):
    """
    Reusable dataset containing contacts/data for bulk messaging and drip campaigns.
    """
    __tablename__ = "whatsapp_datasets"
    
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.String(255), nullable=False, index=True)
    
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    
    # Column schema
    columns = db.Column(db.JSON, default=list)  # List of column names
    column_mapping = db.Column(db.JSON, default=dict)  # Display name -> internal mapping
    
    # Source information
    source_type = db.Column(db.String(50), default="manual")  # manual, csv, google_sheets, crm
    source_config = db.Column(db.JSON, default=dict)  # Source-specific config (sheet_id, etc)
    
    # Sync status
    last_sync_at = db.Column(db.DateTime(timezone=True), nullable=True)
    sync_status = db.Column(db.String(20), default="synced")  # synced, syncing, error
    sync_error = db.Column(db.Text, nullable=True)
    
    # Stats
    total_rows = db.Column(db.Integer, default=0)
    
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=func.now())
    
    # Relationship to rows
    rows = db.relationship("WhatsAppDatasetRow", backref="dataset", cascade="all, delete-orphan", lazy="dynamic")
    
    def to_dict(self, include_rows=False):
        result = {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "name": self.name,
            "description": self.description,
            "columns": self.columns or [],
            "column_mapping": self.column_mapping or {},
            "source_type": self.source_type,
            "source_config": self.source_config or {},
            "last_sync_at": self.last_sync_at.isoformat() if self.last_sync_at else None,
            "sync_status": self.sync_status,
            "sync_error": self.sync_error,
            "total_rows": self.total_rows,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }
        if include_rows:
            result["rows"] = [r.to_dict() for r in self.rows.limit(100).all()]
        return result


class WhatsAppDatasetRow(db.Model):
    """
    Single row of data in a dataset.
    """
    __tablename__ = "whatsapp_dataset_rows"
    
    id = db.Column(db.Integer, primary_key=True)
    dataset_id = db.Column(db.Integer, db.ForeignKey("whatsapp_datasets.id"), nullable=False, index=True)
    
    # Row data stored as JSON key-value pairs
    data = db.Column(db.JSON, default=dict)
    
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=func.now())
    
    def to_dict(self):
        return {
            "id": self.id,
            "dataset_id": self.dataset_id,
            "data": self.data or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }
