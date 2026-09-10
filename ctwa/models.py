# ctwa/models.py
# CTWA / WhatsApp Status Ads — persistence
# ========================================
#
# One row per ad campaign created from the ad wizards (Click-to-WhatsApp OR
# WhatsApp Status). To keep this self-contained we store the whole campaign
# (campaign + ad set + creative) as a single row with JSON columns, plus the
# Meta object ids captured at publish time. `ad_type` distinguishes a normal
# CTWA campaign ('ctwa') from a WhatsApp Status campaign ('status'), and
# `placement` carries the Meta placement spec (pinned to whatsapp/status for
# Status ads).

from datetime import datetime, timezone

from shared_models import db


def _utcnow():
    return datetime.now(timezone.utc)


class CTWACampaign(db.Model):
    __tablename__ = "ctwa_campaigns"

    id = db.Column(db.Integer, primary_key=True)

    # Ownership / tenancy
    workspace_id = db.Column(db.String(255), nullable=False, index=True)
    user_id = db.Column(db.Integer, nullable=True, index=True)

    # Meta targets
    ad_account_id = db.Column(db.String(64), nullable=True)   # act_<id>
    page_id = db.Column(db.String(64), nullable=True)
    whatsapp_phone_number_id = db.Column(db.String(64), nullable=True)

    # Campaign
    name = db.Column(db.String(255), nullable=False)
    objective = db.Column(db.String(64), nullable=False, default="OUTCOME_ENGAGEMENT")
    ad_type = db.Column(db.String(16), nullable=False, default="ctwa")  # 'ctwa' | 'status'
    cta_type = db.Column(db.String(32), nullable=False, default="WHATSAPP_MESSAGE")  # creative call-to-action
    status = db.Column(db.String(16), nullable=False, default="DRAFT")  # DRAFT/PAUSED/ACTIVE/ARCHIVED
    create_leads = db.Column(db.Boolean, nullable=False, default=True)  # auto-create CRM leads from this ad

    # Budget / schedule
    budget_type = db.Column(db.String(16), nullable=False, default="daily")   # daily | lifetime
    daily_budget = db.Column(db.Integer, nullable=True)        # major units (e.g. rupees)
    lifetime_budget = db.Column(db.Integer, nullable=True)     # major units
    budget_currency = db.Column(db.String(8), nullable=True, default="INR")
    start_time = db.Column(db.DateTime, nullable=True)
    end_time = db.Column(db.DateTime, nullable=True)

    # JSON blobs
    placement = db.Column(db.JSON, nullable=True)   # {publisher_platforms, whatsapp_positions}
    targeting = db.Column(db.JSON, nullable=True)   # {geo_locations, age_min, age_max, genders}
    creative = db.Column(db.JSON, nullable=True)    # {primary_text, headline, media_*, ice_breakers, prefilled_message}

    # Meta object ids (populated on publish)
    meta_campaign_id = db.Column(db.String(64), nullable=True)
    meta_adset_id = db.Column(db.String(64), nullable=True)
    meta_ad_id = db.Column(db.String(64), nullable=True)
    meta_creative_id = db.Column(db.String(64), nullable=True)

    sync_status = db.Column(db.String(16), nullable=False, default="pending")  # pending/synced/error
    sync_error = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "meta_campaign_id": self.meta_campaign_id,
            "ad_account_id": self.ad_account_id or "",
            "name": self.name,
            "objective": self.objective,
            "ad_type": self.ad_type,
            "cta_type": self.cta_type,
            "placement": self.placement,
            "status": self.status,
            "create_leads": self.create_leads,
            "budget_type": self.budget_type,
            "daily_budget": self.daily_budget,
            "lifetime_budget": self.lifetime_budget,
            "budget_currency": self.budget_currency or "INR",
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "page_id": self.page_id,
            "whatsapp_phone_number_id": self.whatsapp_phone_number_id,
            "targeting": self.targeting,
            "creative": self.creative,
            "sync_status": self.sync_status,
            "sync_error": self.sync_error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return f"<CTWACampaign {self.id} {self.ad_type} {self.name!r} ws={self.workspace_id}>"


class CTWAWorkspaceSettings(db.Model):
    __tablename__ = "ctwa_workspace_settings"

    id = db.Column(db.Integer, primary_key=True)

    # One saved ad-account setup per workspace (pick once in Settings).
    workspace_id = db.Column(db.String(255), nullable=False, unique=True, index=True)

    ad_account_id = db.Column(db.String(64), nullable=True)
    ad_account_name = db.Column(db.String(255), nullable=True)
    page_id = db.Column(db.String(64), nullable=True)
    page_name = db.Column(db.String(255), nullable=True)
    whatsapp_phone_number_id = db.Column(db.String(64), nullable=True)
    whatsapp_display_number = db.Column(db.String(32), nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    def to_dict(self):
        return {
            "workspace_id": self.workspace_id,
            "ad_account_id": self.ad_account_id,
            "ad_account_name": self.ad_account_name,
            "page_id": self.page_id,
            "page_name": self.page_name,
            "whatsapp_phone_number_id": self.whatsapp_phone_number_id,
            "whatsapp_display_number": self.whatsapp_display_number,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


def ensure_ctwa_schema(engine):
    """Add newly-introduced columns/tables for CTWA feature."""
    from sqlalchemy import text
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE ctwa_campaigns ADD COLUMN IF NOT EXISTS create_leads BOOLEAN NOT NULL DEFAULT TRUE"))
            conn.execute(text("ALTER TABLE ctwa_campaigns ADD COLUMN IF NOT EXISTS cta_type VARCHAR(32) NOT NULL DEFAULT 'WHATSAPP_MESSAGE'"))
            conn.execute(text("ALTER TABLE ctwa_campaigns ADD COLUMN IF NOT EXISTS budget_type VARCHAR(16) NOT NULL DEFAULT 'daily'"))
            conn.execute(text("ALTER TABLE ctwa_campaigns ADD COLUMN IF NOT EXISTS lifetime_budget INTEGER"))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS ctwa_workspace_settings (
                    id SERIAL PRIMARY KEY,
                    workspace_id VARCHAR(255) NOT NULL UNIQUE,
                    ad_account_id VARCHAR(64),
                    ad_account_name VARCHAR(255),
                    page_id VARCHAR(64),
                    page_name VARCHAR(255),
                    whatsapp_phone_number_id VARCHAR(64),
                    whatsapp_display_number VARCHAR(32),
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """))
    except Exception:
        import logging
        logging.getLogger("sociovia.ctwa").warning("ensure_ctwa_schema skipped", exc_info=True)
