import uuid
from datetime import datetime
from flask import current_app
from sqlalchemy import Enum as SAEnum

def get_db():
    db = getattr(current_app, "db", None)
    if db is None:
        raise RuntimeError("app.db (SQLAlchemy) not found on current_app")
    return db

def init_models():
    """
    Defines models on the app's db.Model namespace and attaches them to current_app.crm_models
    Call this after db.init_app(app) and within app context (or from blueprint factory as above).
    """
    # Avoid re-initializing models if already initialized
    if getattr(current_app, "crm_models_initialized", False):
        return
    db = get_db()

    # Try to reuse existing Workspace model to avoid duplicate mappers
    try:
        from models import Workspace
    except ImportError:
        class Workspace(db.Model):
            __tablename__ = "workspaces2"
            id = db.Column(db.Integer, primary_key=True)
            # business_name is the "name" equivalent in workspaces2, typically
            business_name = db.Column(db.String(255), nullable=True)

            __table_args__ = ({"extend_existing": True},) 


    class Lead(db.Model):
        __tablename__ = "leads"
        id = db.Column(db.String, primary_key=True, default=lambda: str(uuid.uuid4()))
        workspace_id = db.Column(
            db.Integer,
            db.ForeignKey("workspaces2.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
        name = db.Column(db.String(255), nullable=False)
        email = db.Column(db.String(255), nullable=True)
        phone = db.Column(db.String(64), nullable=True)
        company = db.Column(db.String(255), nullable=True)
        job_title = db.Column(db.String(255), nullable=True)
        status = db.Column(
            SAEnum("new", "contacted", "qualified", "proposal", "closed", name="lead_status"),
            default="new",
            nullable=False,
        )
        source = db.Column(db.String(128), nullable=True)
        score = db.Column(db.Integer, default=0)
        value = db.Column(db.Numeric(12, 2), default=0)
        owner_id = db.Column(db.String(64), nullable=True)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
        last_interaction_at = db.Column(db.DateTime, nullable=True)

        # 🔽 NEW: details for unstructured data
        details = db.Column(db.JSON, nullable=True)

        # 🔽 NEW: sync + external metadata (matches your ALTER TABLE)
        external_source = db.Column(db.String(128), nullable=True)       # e.g. "meta_leadgen", "zapier"
        external_id     = db.Column(db.String, nullable=True, index=True)
        sync_status     = db.Column(db.String(64), nullable=True, default="in_sync")
        last_sync_at    = db.Column(db.DateTime, nullable=True)
        sync_error      = db.Column(db.Text, nullable=True)

        __table_args__ = ({"extend_existing": True},)

    class Contact(db.Model):
        __tablename__ = "contacts"

        id = db.Column(db.String, primary_key=True, default=lambda: str(uuid.uuid4()))
        workspace_id = db.Column(
            db.Integer,
            db.ForeignKey("workspaces2.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )

        # ownership / external mapping
        user_id = db.Column(
            db.Integer,
            db.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        )
        external_id = db.Column(db.String, nullable=True, index=True)

        # 🔽 NEW: sync metadata for contacts too (optional but you ran the ALTER)
        external_source = db.Column(db.String(128), nullable=True)
        sync_status     = db.Column(db.String(64), nullable=True, default="in_sync")
        last_sync_at    = db.Column(db.DateTime, nullable=True)
        sync_error      = db.Column(db.Text, nullable=True)

        # basic details
        name = db.Column(db.String(255), nullable=False)
        email = db.Column(db.String(255), nullable=True, index=True)
        role = db.Column(db.String(128), nullable=True)
        company = db.Column(db.String(255), nullable=True)
        phone = db.Column(db.String(64), nullable=True)

        # avatar URL
        avatar = db.Column(db.String, nullable=True)

        # notes & status
        notes = db.Column(db.Text, nullable=True)
        status = db.Column(db.String(50), nullable=False, server_default="active", index=True)

        # last contacted timestamp (nullable)
        last_contacted = db.Column(db.DateTime, nullable=True)

        # flexible JSON columns for tags, socials and future fields
        tags = db.Column(db.JSON, nullable=True, default=list)    # array of strings
        socials = db.Column(db.JSON, nullable=True, default=dict) # { linkedin, twitter, website, instagram, ... }
        extras = db.Column(db.JSON, nullable=True, default=dict)  # extensible metadata

        # timestamps
        created_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())
        updated_at = db.Column(
            db.DateTime,
            nullable=False,
            server_default=db.func.now(),
            onupdate=db.func.now(),
        )

        def __repr__(self):
            return f"<Contact id={self.id} name={self.name!r} email={self.email!r}>"

        def to_dict(self):
            return {
                "id": self.id,
                "workspace_id": self.workspace_id,
                "user_id": self.user_id,
                "name": self.name,
                "email": self.email,
                "phone": self.phone,
                "role": self.role,
                "company": self.company,
                "avatar": self.avatar,
                "notes": self.notes,
                "status": self.status,
                "tags": self.tags or [],
                "socials": self.socials or {},
                "last_contacted": self.last_contacted.isoformat() if self.last_contacted else None,
                "created_at": self.created_at.isoformat() if self.created_at else None,
                "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            }

        __table_args__ = ({"extend_existing": True},)

    class Task(db.Model):
        __tablename__ = "tasks"
        id = db.Column(db.String, primary_key=True, default=lambda: str(uuid.uuid4()))
        workspace_id = db.Column(
            db.Integer,
            db.ForeignKey("workspaces2.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
        title = db.Column(db.String(255), nullable=False)
        description = db.Column(db.Text, nullable=True)
        due_date = db.Column(db.DateTime, nullable=True)
        priority = db.Column(
            SAEnum("low", "medium", "high", name="task_priority"),
            default="medium",
            nullable=False,
        )
        completed = db.Column(db.Boolean, default=False)
        related_to_type = db.Column(
            SAEnum("lead", "campaign", "general", name="related_type"),
            default="general",
        )
        related_to_id = db.Column(db.String, nullable=True)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)

        __table_args__ = ({"extend_existing": True},)

    class Activity(db.Model):
        __tablename__ = "activities"

        id = db.Column(db.String, primary_key=True, default=lambda: str(uuid.uuid4()))

        workspace_id = db.Column(
            db.Integer,
            db.ForeignKey("workspaces2.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )

        # ✅ UPDATED: added deal + task
        entity_type = db.Column(
            SAEnum(
                "lead",
                "contact",
                "deal",
                "task",
                name="activity_entity",
            ),
            nullable=False,
        )

        entity_id = db.Column(db.String, nullable=False)

        type = db.Column(
            SAEnum(
                "email_opened",
                "page_visit",
                "call",
                "note_created",
                "status_change",
                "stage_change",
                "deal_created",   # ✅ ADD THIS
                "meeting",
                name="activity_type",
            ),
            nullable=False,
        )


        title = db.Column(db.String(255), nullable=True)
        description = db.Column(db.Text, nullable=True)
        timestamp = db.Column(db.DateTime, default=datetime.utcnow)

        __table_args__ = ({"extend_existing": True},)

    class Campaign(db.Model):
        __tablename__ = "campaigns"
        id = db.Column(db.String, primary_key=True, default=lambda: str(uuid.uuid4()))
        workspace_id = db.Column(
            db.Integer,
            db.ForeignKey("workspaces2.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
        # ✅ NEW: link to user + external meta ID
        user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
        external_id = db.Column(db.String, nullable=True, index=True)  # e.g. Meta campaign ID

        name = db.Column(db.String(255), nullable=False)
        status = db.Column(db.String(64), nullable=True)
        objective = db.Column(db.String(128), nullable=True)
        
        # metrics
        spend = db.Column(db.Numeric(14, 2), default=0)
        impressions = db.Column(db.Integer, default=0)
        clicks = db.Column(db.Integer, default=0)
        leads = db.Column(db.Integer, default=0)
        revenue = db.Column(db.Numeric(14, 2), default=0)

        # ✅ NEW: store full payload & details
        payload = db.Column(db.JSON, nullable=True)
        details = db.Column(db.JSON, nullable=True)

        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

        __table_args__ = ({"extend_existing": True},)

    class Setting(db.Model):
        __tablename__ = "settings"
        id = db.Column(db.Integer, primary_key=True)
        workspace_id = db.Column(
            db.Integer,
            db.ForeignKey("workspaces2.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
        name = db.Column(db.String(128), unique=False, nullable=False)
        value = db.Column(db.Text, nullable=True)
        masked = db.Column(db.Boolean, default=True)

        __table_args__ = (
            db.UniqueConstraint("workspace_id", "name", name="uq_workspace_setting_name"),
            {"extend_existing": True},
        )
    

    class CapiAccount(db.Model):
        __tablename__ = "capi_accounts"
        __table_args__ = {"extend_existing": True}

        id = db.Column(db.Integer, primary_key=True)
        workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces2.id", ondelete="CASCADE"), nullable=False, index=True)
        user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
        ad_account_id = db.Column(db.String(128), nullable=True)
        client_business_id = db.Column(db.String(128), nullable=True)
        system_user_token = db.Column(db.Text, nullable=True)
        pixel_id = db.Column(db.String(128), nullable=True)
        created_at = db.Column(db.DateTime, default=db.func.now())
        updated_at = db.Column(db.DateTime, default=db.func.now(), onupdate=db.func.now())

    class CapiEvent(db.Model):
        __tablename__ = "capi_events"

        id = db.Column(db.Integer, primary_key=True)
        workspace_id = db.Column(
            db.Integer,
            db.ForeignKey("workspaces2.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
        event_id = db.Column(db.String(128), unique=True, nullable=False, index=True)
        event_name = db.Column(db.String(64), nullable=False)
        pixel_id = db.Column(db.String(64), nullable=True)
        ad_id = db.Column(db.String(64), nullable=True)
        
        # Foreign keys matching migration
        campaign_id = db.Column(db.Integer, nullable=True) # Assuming manual FK handling or lazy loading if not strictly defined in migration as relationship to model class yet
        conversation_id = db.Column(db.Integer, nullable=True)

        event_time = db.Column(db.DateTime, nullable=False)
        event_source_url = db.Column(db.String(1024), nullable=True)
        action_source = db.Column(db.String(64), server_default="business_messaging")
        
        user_data_json = db.Column(db.JSON, nullable=True) # Postgres JSONB in migration, db.JSON here usually maps fine
        custom_data_json = db.Column(db.JSON, nullable=True)
        
        status = db.Column(db.String(32), server_default="pending")
        api_response = db.Column(db.JSON, nullable=True)
        error_message = db.Column(db.Text, nullable=True)
        
        sent_at = db.Column(db.DateTime, nullable=True)
        retry_count = db.Column(db.Integer, server_default="0")
        created_at = db.Column(db.DateTime, default=datetime.utcnow)

        __table_args__ = ({"extend_existing": True},)

    class Deal(db.Model):
        __tablename__ = "deals"

        id = db.Column(db.String, primary_key=True, default=lambda: str(uuid.uuid4()))
        workspace_id = db.Column(
            db.Integer,
            db.ForeignKey("workspaces2.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )

        # basic deal info
        name = db.Column(db.String(255), nullable=False)
        value = db.Column(db.Numeric(14, 2), default=0)   # monetary value
        currency = db.Column(db.String(8), nullable=False, server_default="USD")

        # lifecycle — include 'discovery' here
        stage = db.Column(
            SAEnum(
                "prospect",
                "discovery",
                "qualified",
                "proposal",
                "negotiation",
                "won",
                "lost",
                name="deal_stage",
            ),
            nullable=False,
            default="prospect",
        )
        status = db.Column(
            SAEnum("open", "closed", "archived", name="deal_status"),
            nullable=False,
            default="open",
        )

        probability = db.Column(db.Integer, nullable=True)   # 0-100
        owner_id = db.Column(db.String(64), nullable=True, index=True)   # user who owns the deal

        contact_id = db.Column(db.String, nullable=True, index=True)
        company = db.Column(db.String(255), nullable=True)

        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
        close_date = db.Column(db.DateTime, nullable=True)

        lost_reason = db.Column(db.Text, nullable=True)
        notes = db.Column(db.Text, nullable=True)

        external_source = db.Column(db.String(128), nullable=True)
        external_id = db.Column(db.String, nullable=True, index=True)
        sync_status = db.Column(db.String(64), nullable=True, default="in_sync")
        last_sync_at = db.Column(db.DateTime, nullable=True)
        sync_error = db.Column(db.Text, nullable=True)

        def __repr__(self):
            return f"<Deal id={self.id} name={self.name!r} stage={self.stage!r} value={self.value}>"

        __table_args__ = ({"extend_existing": True},)

    current_app.crm_models = {
        "Workspace": Workspace,
        "Lead": Lead,
        "Contact": Contact,
        "Task": Task,
        "Activity": Activity,
        "Campaign": Campaign,
        "Deal": Deal,
        "CapiAccount": CapiAccount,
        "CapiEvent": CapiEvent,
        "Setting": Setting,
    }
    current_app.crm_models_initialized = True

