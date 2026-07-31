# agent_auth/models.py
"""
Agent (sub-login) models — SocioChat.
=====================================
An agent is a restricted sub-login created by an account owner (a `users` row).

Hierarchy:  Tenant -> User (owner account) -> Workspace(s)
            An agent hangs off ONE owner account and is granted a SUBSET of that
            owner's workspaces (Level 2) plus a set of feature permissions (Level 1).

Security:
- Passwords are always stored hashed (werkzeug pbkdf2:sha256), never plaintext.
- Username is GLOBALLY unique (uq_workspace_agent_username) — the agent logs in
  with username + password only (no Account ID). The owner_user_id is still
  denormalized on the row for data-ownership checks + token minting.
- allowed_pages/allowed_paths never include admin-only surfaces (enforced on write).
"""

from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import JSONB
from werkzeug.security import generate_password_hash, check_password_hash

from models import db
from .config import convert_pages_to_paths


class WorkspaceAgent(db.Model):
    """A restricted sub-login owned by an account (users row)."""

    __tablename__ = "workspace_agents"
    # NOTE: per-column indexes come from index=True on the columns below; do not
    # also declare them here or create_all() emits a duplicate CREATE INDEX.
    __table_args__ = (
        db.UniqueConstraint("username", name="uq_workspace_agent_username"),
        {"extend_existing": True},
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)

    # The account (owner user) this agent belongs to. Agent acts on behalf of this
    # user for data-ownership checks, but is narrowed to allowed workspaces/features.
    owner_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Denormalized tenant of the owner (for future tenant-level replication + isolation).
    tenant_id = db.Column(db.Integer, nullable=True, index=True)

    # Credentials
    username = db.Column(db.String(100), nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    display_name = db.Column(db.String(255), nullable=True)

    is_active = db.Column(db.Boolean, default=True, nullable=False)

    # Level 1 — feature permissions. allowed_pages = page keys (UI source of truth);
    # allowed_paths = derived URL patterns for the frontend route guard.
    allowed_pages = db.Column(JSONB, nullable=False, default=list)
    allowed_paths = db.Column(JSONB, nullable=False, default=list)

    # Tracking
    last_login_at = db.Column(db.DateTime(timezone=True), nullable=True)
    login_count = db.Column(db.Integer, default=0, nullable=False)

    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    workspaces = db.relationship(
        "AgentWorkspace",
        backref="agent",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )

    # --- password ---
    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password, method="pbkdf2:sha256")

    def check_password(self, password: str) -> bool:
        try:
            return check_password_hash(self.password_hash, password)
        except Exception:
            return False

    # --- permissions ---
    def set_allowed_pages(self, page_keys) -> None:
        """Set page permissions and keep the derived path list in sync."""
        pages = list(page_keys or [])
        self.allowed_pages = pages
        self.allowed_paths = convert_pages_to_paths(pages)

    def get_allowed_pages(self) -> list:
        val = self.allowed_pages
        return list(val) if isinstance(val, list) else []

    def get_allowed_paths(self) -> list:
        val = self.allowed_paths
        if isinstance(val, list) and val:
            return list(val)
        # Fall back to deriving from pages if paths were never stored.
        return convert_pages_to_paths(self.get_allowed_pages())

    # --- workspaces (Level 2) ---
    def get_allowed_workspace_ids(self) -> list:
        return [w.workspace_id for w in self.workspaces.all()]

    def has_workspace(self, workspace_id) -> bool:
        try:
            wid = int(workspace_id)
        except (TypeError, ValueError):
            return False
        return wid in set(self.get_allowed_workspace_ids())

    # --- tracking ---
    def register_login(self) -> None:
        self.last_login_at = datetime.now(timezone.utc)
        self.login_count = (self.login_count or 0) + 1

    def serialize(self, include_sensitive: bool = False) -> dict:
        data = {
            "id": self.id,
            "owner_user_id": self.owner_user_id,
            "tenant_id": self.tenant_id,
            "username": self.username,
            "display_name": self.display_name,
            "is_active": self.is_active,
            "allowed_pages": self.get_allowed_pages(),
            "allowed_paths": self.get_allowed_paths(),
            "workspace_ids": self.get_allowed_workspace_ids(),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_sensitive:
            data["last_login_at"] = self.last_login_at.isoformat() if self.last_login_at else None
            data["login_count"] = self.login_count
        return data

    def __repr__(self):
        return f"<WorkspaceAgent {self.username} owner={self.owner_user_id}>"


class AgentWorkspace(db.Model):
    """Which workspaces an agent may access (Level 2 scoping).

    Also carries the per-workspace INBOX SCOPE (Level 3):
    - inbox_scope='all'     -> agent sees every conversation in the workspace
    - inbox_scope='by_chat' -> agent sees ONLY customer numbers assigned to them
                               via AgentNumberAssignment
    - auto_assign           -> when True (and the workspace's auto-assign is on),
                               this agent participates in round-robin assignment
                               of brand-new inbound customer numbers.
    """

    __tablename__ = "agent_workspaces"
    __table_args__ = (
        db.UniqueConstraint("agent_id", "workspace_id", name="uq_agent_workspace"),
        {"extend_existing": True},
    )

    INBOX_SCOPES = ("all", "by_chat")

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    agent_id = db.Column(
        db.Integer,
        db.ForeignKey("workspace_agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces2.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Level 3 inbox scoping for THIS agent in THIS workspace.
    inbox_scope = db.Column(db.String(16), nullable=False, default="all", server_default="all")
    # Participates in workspace round-robin auto-assignment of new numbers.
    auto_assign = db.Column(db.Boolean, nullable=False, default=False, server_default="false")
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    def __repr__(self):
        return f"<AgentWorkspace agent={self.agent_id} ws={self.workspace_id} scope={self.inbox_scope}>"


class AgentNumberAssignment(db.Model):
    """Level 3 — which CUSTOMER phone numbers an agent handles in a workspace.

    Exclusive: one number belongs to at most ONE agent per workspace (unique
    constraint). Release = delete the row. Keyed on the normalized customer
    phone (not conversation id) so numbers can be assigned IN ADVANCE, before
    the customer has ever messaged.

    assignment_type: 'advance' (pre-assigned before first message),
                     'manual'  (owner picked an existing inbox number),
                     'auto'    (round-robin assigned on first inbound message).
    """

    __tablename__ = "agent_number_assignments"
    __table_args__ = (
        db.UniqueConstraint("workspace_id", "customer_phone", name="uq_agent_number_ws_phone"),
        {"extend_existing": True},
    )

    ASSIGNMENT_TYPES = ("advance", "manual", "auto")

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    agent_id = db.Column(
        db.Integer,
        db.ForeignKey("workspace_agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces2.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Normalized (whatsapp.utils.normalize_phone) customer phone in E.164-ish form.
    customer_phone = db.Column(db.String(32), nullable=False, index=True)
    assignment_type = db.Column(db.String(16), nullable=False, default="manual")
    # Owner user who made the assignment (None for 'auto').
    assigned_by_user_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    def serialize(self) -> dict:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "workspace_id": self.workspace_id,
            "customer_phone": self.customer_phone,
            "assignment_type": self.assignment_type,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f"<AgentNumberAssignment {self.customer_phone} -> agent={self.agent_id} ws={self.workspace_id}>"


class WorkspaceAutoAssignState(db.Model):
    """Per-workspace round-robin auto-assignment switch + rotation pointer.

    When enabled, a brand-new inbound customer number (no existing assignment)
    is assigned to the next participating agent after last_agent_id. Default is
    OFF — nothing auto-routes until the owner turns it on.
    """

    __tablename__ = "workspace_autoassign_state"
    __table_args__ = ({"extend_existing": True},)

    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces2.id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled = db.Column(db.Boolean, nullable=False, default=False, server_default="false")
    # The agent that received the LAST auto assignment (rotation pointer).
    last_agent_id = db.Column(db.Integer, nullable=True)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self):
        return f"<WorkspaceAutoAssignState ws={self.workspace_id} enabled={self.enabled}>"


class AgentAuditLog(db.Model):
    """Security audit trail for agent auth + management actions."""

    __tablename__ = "agent_auth_audit_logs"
    __table_args__ = ({"extend_existing": True},)

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    agent_id = db.Column(db.Integer, nullable=True, index=True)       # nullable for failed logins
    owner_user_id = db.Column(db.Integer, nullable=True, index=True)
    action = db.Column(db.String(64), nullable=False)                # login_success, login_failed, agent_created, ...
    resource = db.Column(db.String(255), nullable=True)
    meta = db.Column(JSONB, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    user_agent = db.Column(db.String(512), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True)

    @classmethod
    def log(cls, action, agent_id=None, owner_user_id=None, resource=None,
            meta=None, ip_address=None, user_agent=None):
        entry = cls(
            action=action,
            agent_id=agent_id,
            owner_user_id=owner_user_id,
            resource=resource,
            meta=meta,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.session.add(entry)
        return entry

    def __repr__(self):
        return f"<AgentAuditLog {self.action} agent={self.agent_id}>"
