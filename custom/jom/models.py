"""
JOM vertical models (bind to the shared SocioChat `db`). New tables only —
see schema_patch.py for the idempotent DDL. All rows are scoped by workspace_id.
"""
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import JSONB

from models import db


def _now():
    return datetime.now(timezone.utc)


class JomDepartment(db.Model):
    __tablename__ = "jom_departments"
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces2.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)
    match_keywords = db.Column(JSONB, nullable=False, default=list)
    routing_mode = db.Column(db.String(16), nullable=False, default="round_robin")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    last_agent_id = db.Column(db.Integer, nullable=True)   # round-robin pointer within this dept
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)
    __table_args__ = (db.UniqueConstraint("workspace_id", "name", name="uq_jom_dept_ws_name"),)


class JomDepartmentAgent(db.Model):
    __tablename__ = "jom_department_agents"
    id = db.Column(db.Integer, primary_key=True)
    department_id = db.Column(db.Integer, db.ForeignKey("jom_departments.id", ondelete="CASCADE"), nullable=False, index=True)
    agent_id = db.Column(db.Integer, db.ForeignKey("workspace_agents.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    __table_args__ = (db.UniqueConstraint("department_id", "agent_id", name="uq_jom_dept_agent"),)


class JomLifecycleState(db.Model):
    __tablename__ = "jom_lifecycle_state"
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces2.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_phone = db.Column(db.String(32), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("jom_departments.id", ondelete="SET NULL"), nullable=True)
    status = db.Column(db.String(24), nullable=False, default="new")
    assigned_agent_id = db.Column(db.Integer, db.ForeignKey("workspace_agents.id", ondelete="SET NULL"), nullable=True)
    meta_lead_id = db.Column(db.String(40), nullable=True)   # Meta Lead Ads 15-17 digit Lead ID (CAPI)
    email = db.Column(db.String(160), nullable=True)         # for CAPI match keys
    reopen_count = db.Column(db.Integer, nullable=False, default=0)  # returning-fresh-lead history
    drip_stage = db.Column(db.String(16), nullable=True)
    status_since = db.Column(db.DateTime(timezone=True), nullable=True)      # when current status was set
    next_followup_at = db.Column(db.DateTime(timezone=True), nullable=True)  # when next Ghosted/Dormant msg is due
    last_inbound_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_outbound_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)
    __table_args__ = (db.UniqueConstraint("workspace_id", "customer_phone", name="uq_jom_lifecycle_ws_phone"),)


class JomBehaviorScore(db.Model):
    __tablename__ = "jom_behavior_score"
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces2.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_phone = db.Column(db.String(32), nullable=False)
    engagement_score = db.Column(db.Integer, nullable=False, default=0)
    consecutive_no_reply = db.Column(db.Integer, nullable=False, default=0)
    total_inbound = db.Column(db.Integer, nullable=False, default=0)
    total_outbound = db.Column(db.Integer, nullable=False, default=0)
    last_intent = db.Column(db.String(32), nullable=True)
    last_confidence = db.Column(db.Float, nullable=True)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)
    __table_args__ = (db.UniqueConstraint("workspace_id", "customer_phone", name="uq_jom_behavior_ws_phone"),)
