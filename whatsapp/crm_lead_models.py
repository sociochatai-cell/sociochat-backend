"""
CRM Lead Models Bridge (WhatsApp service)
=========================================

Minimal SQLAlchemy models on the shared `db` that map the EXISTING Sociovia CRM
tables — leads, pipelines, pipeline_stages, lead_types — so the WhatsApp flow
engine can read/write real CRM leads from this service.

Everything shares ONE Postgres DB, so these models read/write the same rows the
monolith CRM uses. We only declare the columns the flow→lead engine needs and use
`__table_args__ = {"extend_existing": True}` on every model so we never clash with
mappers the monolith (or crm_bootstrap.py) may already have registered for the
same tables.

IMPORTANT TYPE NOTE: `leads.workspace_id` is INTEGER (FK workspaces2). The WhatsApp
service carries workspace_id as a STRING — always `int(workspace_id)` before
touching these models.

These classes use distinct names (CrmLead, CrmPipeline, CrmPipelineStage,
CrmLeadType) to avoid colliding with the minimal Lead model in crm_bootstrap.py,
while pointing at the same physical tables.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from shared_models import db


class CrmLead(db.Model):
    """Maps the real CRM `leads` table (shared DB). Only flow-engine columns."""

    __tablename__ = "leads"
    __table_args__ = {"extend_existing": True}

    id = db.Column(db.String, primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = db.Column(db.Integer, nullable=False, index=True)  # FK workspaces2 (INTEGER)
    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(64), nullable=True)
    company = db.Column(db.String(255), nullable=True)
    job_title = db.Column(db.String(255), nullable=True)

    # legacy enum-backed status: new/contacted/qualified/proposal/closed.
    # Physical column is NOT NULL — default 'new' so a freshly-flushed row is valid
    # even if the caller forgets to set it.
    status = db.Column(db.String(64), nullable=True, default="new")
    source = db.Column(db.String(128), nullable=True)

    # configurable pipeline stage + lead type + WhatsApp linkage (P0 columns)
    pipeline_id = db.Column(db.Integer, nullable=True, index=True)
    stage_id = db.Column(db.Integer, nullable=True, index=True)
    lead_type = db.Column(db.String(64), nullable=True)
    conversation_id = db.Column(db.BigInteger, nullable=True, index=True)  # whatsapp_conversations.id
    wa_account_id = db.Column(db.Integer, nullable=True)  # whatsapp_accounts.id

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_interaction_at = db.Column(db.DateTime, nullable=True)


class CrmPipeline(db.Model):
    """Maps the real CRM `pipelines` table (shared DB)."""

    __tablename__ = "pipelines"
    __table_args__ = {"extend_existing": True}

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False, default="Default")
    is_default = db.Column(db.Boolean, nullable=False, default=False)


class CrmPipelineStage(db.Model):
    """Maps the real CRM `pipeline_stages` table (shared DB)."""

    __tablename__ = "pipeline_stages"
    __table_args__ = {"extend_existing": True}

    id = db.Column(db.Integer, primary_key=True)
    pipeline_id = db.Column(db.Integer, nullable=False, index=True)
    key = db.Column(db.String(64), nullable=False)
    label = db.Column(db.String(128), nullable=False)
    order_index = db.Column(db.Integer, nullable=False, default=0)
    is_won = db.Column(db.Boolean, nullable=False, default=False)
    is_lost = db.Column(db.Boolean, nullable=False, default=False)
    color = db.Column(db.String(16), nullable=True)


class CrmLeadType(db.Model):
    """Maps the real CRM `lead_types` table (shared DB)."""

    __tablename__ = "lead_types"
    __table_args__ = {"extend_existing": True}

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, nullable=False, index=True)
    key = db.Column(db.String(64), nullable=False)
    label = db.Column(db.String(128), nullable=False)
    color = db.Column(db.String(16), nullable=True)
