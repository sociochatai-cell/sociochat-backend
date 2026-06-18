"""
Visual Automation Models
========================

Database models for the visual automation builder.
Stores automations with nodes (triggers, messages, buttons) and edges (connections).

Multi-tenant: All automations scoped by workspace_id and account_id.
"""

from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from models import db
from sqlalchemy import Index, JSON, Text
from sqlalchemy.orm.attributes import flag_modified


class WhatsAppVisualAutomation(db.Model):
    """
    A visual automation flow created in the automation builder.
    
    Stores the entire flow as nodes and edges (like React Flow).
    """
    __tablename__ = "whatsapp_visual_automations"
    __table_args__ = (
        Index("ix_visual_automation_workspace", "workspace_id"),
        Index("ix_visual_automation_account", "account_id"),
        Index("ix_visual_automation_status", "status"),
        {"extend_existing": True},
    )

    id = db.Column(db.Integer, primary_key=True)
    
    # Multi-tenant scoping
    workspace_id = db.Column(db.String(255), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey("whatsapp_accounts.id"), nullable=False)
    
    # Basic info
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    
    # Trigger configuration
    trigger_type = db.Column(db.String(32), nullable=False)  # any_message, keyword, template_reply
    trigger_config = db.Column(JSON, nullable=False, default=dict)
    
    # Visual canvas data (React Flow format)
    nodes = db.Column(JSON, nullable=False, default=list)  # Array of node objects
    edges = db.Column(JSON, nullable=False, default=list)  # Array of edge objects
    
    # Viewport state (for restoring canvas position)
    viewport = db.Column(JSON, nullable=True)  # {x, y, zoom}
    
    # Status
    status = db.Column(db.String(32), default="draft")  # draft, active, paused
    is_active = db.Column(db.Boolean, default=False)
    
    # Statistics
    trigger_count = db.Column(db.Integer, default=0)
    last_triggered_at = db.Column(db.DateTime, nullable=True)
    
    # Version control
    version = db.Column(db.Integer, default=1)
    
    # Audit
    created_by = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "id": self.id,
            "workspaceId": self.workspace_id,
            "accountId": self.account_id,
            "name": self.name,
            "description": self.description,
            "triggerType": self.trigger_type,
            "triggerConfig": self.trigger_config,
            "variables": self.variables,
            "flowConfig": self.flow_config,
            "flow_config": self.flow_config,
            "nodes": self.nodes,
            "edges": self.edges,
            "viewport": self.viewport,
            "status": self.status,
            "isActive": self.is_active,
            "triggerCount": self.trigger_count,
            "lastTriggeredAt": self.last_triggered_at.isoformat() if self.last_triggered_at else None,
            "version": self.version,
            "createdBy": self.created_by,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
        }
    
    def activate(self):
        """Activate the automation."""
        self.status = "active"
        self.is_active = True
        self.updated_at = datetime.now(timezone.utc)
    
    def pause(self):
        """Pause the automation."""
        self.status = "paused"
        self.is_active = False
        self.updated_at = datetime.now(timezone.utc)
    
    def increment_trigger_count(self):
        """Increment trigger count and update last triggered time."""
        self.trigger_count = (self.trigger_count or 0) + 1
        self.last_triggered_at = datetime.now(timezone.utc)

    # -- Flow Variables & Config (additive, no migration) ---------------
    # Flow-level variables (API tokens, URLs) and flow_config are stored
    # inside the existing trigger_config JSON column under reserved keys so
    # no schema migration is required.
    _VARIABLES_KEY = "_flow_variables"
    _FLOW_CONFIG_KEY = "_flow_config"

    @property
    def variables(self) -> Dict[str, Any]:
        """Per-flow variables (e.g. {"flow_api_token": "..."})."""
        cfg = self.trigger_config if isinstance(self.trigger_config, dict) else {}
        value = cfg.get(self._VARIABLES_KEY)
        return value if isinstance(value, dict) else {}

    @variables.setter
    def variables(self, value: Optional[Dict[str, Any]]) -> None:
        cfg = dict(self.trigger_config) if isinstance(self.trigger_config, dict) else {}
        cfg[self._VARIABLES_KEY] = value if isinstance(value, dict) else {}
        self.trigger_config = cfg
        flag_modified(self, "trigger_config")

    @property
    def flow_config(self) -> Dict[str, Any]:
        """Per-flow config, e.g. {"variableDefaults": {...}, "buttonCaptureRules": [...]}."""
        cfg = self.trigger_config if isinstance(self.trigger_config, dict) else {}
        value = cfg.get(self._FLOW_CONFIG_KEY)
        return value if isinstance(value, dict) else {}

    @flow_config.setter
    def flow_config(self, value: Optional[Dict[str, Any]]) -> None:
        cfg = dict(self.trigger_config) if isinstance(self.trigger_config, dict) else {}
        cfg[self._FLOW_CONFIG_KEY] = value if isinstance(value, dict) else {}
        self.trigger_config = cfg
        flag_modified(self, "trigger_config")


class WhatsAppAutomationNode(db.Model):
    """
    Individual node in a visual automation.
    
    This is an alternative to storing nodes as JSON in the parent.
    Useful for more complex querying and indexing.
    """
    __tablename__ = "whatsapp_automation_nodes"
    __table_args__ = (
        Index("ix_automation_node_automation", "automation_id"),
        Index("ix_automation_node_type", "node_type"),
        {"extend_existing": True},
    )
    
    id = db.Column(db.Integer, primary_key=True)
    automation_id = db.Column(db.Integer, db.ForeignKey("whatsapp_visual_automations.id", ondelete="CASCADE"), nullable=False)
    
    # Node identification
    node_id = db.Column(db.String(64), nullable=False)  # Client-side ID like "message-1"
    node_type = db.Column(db.String(32), nullable=False)  # trigger, message, buttons, delay, flow, condition
    
    # Position on canvas
    position_x = db.Column(db.Float, default=0)
    position_y = db.Column(db.Float, default=0)
    
    # Node data (type-specific)
    data = db.Column(JSON, nullable=False, default=dict)
    
    # Order (for sequential execution)
    execution_order = db.Column(db.Integer, default=0)
    
    # Audit
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to React Flow node format."""
        return {
            "id": self.node_id,
            "type": self.node_type,
            "position": {"x": self.position_x, "y": self.position_y},
            "data": self.data,
        }


class WhatsAppConversationState(db.Model):
    """
    Tracks the current state of a conversation within an automation.
    
    This is the "Conversation State Engine" - remembers where each user is.
    """
    __tablename__ = "whatsapp_conversation_states"
    __table_args__ = (
        Index("ix_conv_state_conversation", "conversation_id"),
        Index("ix_conv_state_automation", "automation_id"),
        Index("ix_conv_state_active", "is_active"),
        {"extend_existing": True},
    )
    
    id = db.Column(db.Integer, primary_key=True)
    
    # Multi-tenant scoping
    workspace_id = db.Column(db.String(255), nullable=False)
    
    # Conversation tracking
    conversation_id = db.Column(db.Integer, nullable=False)
    phone_number = db.Column(db.String(32), nullable=False)
    
    # Active automation
    automation_id = db.Column(db.Integer, db.ForeignKey("whatsapp_visual_automations.id", ondelete="SET NULL"), nullable=True)
    
    # Current position in flow
    current_node_id = db.Column(db.String(64), nullable=True)  # Which node are we at?
    last_button_clicked = db.Column(db.String(64), nullable=True)  # For button tracking
    
    # State data (for complex flows)
    state_data = db.Column(JSON, default=dict)  # Collected inputs, variables, etc.
    
    # 24-hour window tracking
    last_user_message_at = db.Column(db.DateTime, nullable=True)
    is_within_24h_window = db.Column(db.Boolean, default=True)
    
    # Status
    is_active = db.Column(db.Boolean, default=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    
    # Audit
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for debugging/API."""
        return {
            "id": self.id,
            "conversationId": self.conversation_id,
            "phoneNumber": self.phone_number,
            "automationId": self.automation_id,
            "currentNodeId": self.current_node_id,
            "lastButtonClicked": self.last_button_clicked,
            "stateData": self.state_data,
            "isWithin24hWindow": self.is_within_24h_window,
            "isActive": self.is_active,
        }
    
    def advance_to_node(self, node_id: str):
        """Move to the next node in the automation."""
        self.current_node_id = node_id
        self.updated_at = datetime.now(timezone.utc)
    
    def record_button_click(self, button_id: str):
        """Record which button was clicked."""
        self.last_button_clicked = button_id
        self.updated_at = datetime.now(timezone.utc)
    
    def complete(self):
        """Mark this conversation state as completed."""
        self.is_active = False
        self.completed_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)

    # -- Input Flow Helpers (additive) ----------------------------------
    # Collected input fields, field order (for corrections), and the
    # waiting-for-input flag are all stored inside the existing state_data
    # JSON column - no schema migration required.

    def set_collected_field(self, field: str, value) -> None:
        """Store a collected input field value."""
        sd = self.state_data if isinstance(self.state_data, dict) else {}
        collected = sd.get("collected", {})
        collected[field] = value
        sd["collected"] = collected
        # Track field order for correction support
        field_order = sd.get("_field_order", [])
        if field not in field_order:
            field_order.append(field)
        sd["_field_order"] = field_order
        self.state_data = sd
        flag_modified(self, "state_data")
        self.updated_at = datetime.now(timezone.utc)

    def get_collected_fields(self) -> dict:
        """Get all collected input values."""
        return (self.state_data or {}).get("collected", {})

    def get_last_collected_field(self) -> Optional[str]:
        """Get the name of the last collected field (for correction support)."""
        sd = self.state_data if isinstance(self.state_data, dict) else {}
        field_order = sd.get("_field_order", [])
        return field_order[-1] if field_order else None

    def set_waiting_for_input(self, field: str) -> None:
        """Mark state as waiting for user input on a specific field."""
        sd = self.state_data if isinstance(self.state_data, dict) else {}
        sd["waiting_for_input"] = True
        sd["current_field"] = field
        self.state_data = sd
        flag_modified(self, "state_data")
        self.updated_at = datetime.now(timezone.utc)

    def clear_waiting_for_input(self) -> None:
        """Clear the input-waiting flag after successful input."""
        sd = self.state_data if isinstance(self.state_data, dict) else {}
        sd.pop("waiting_for_input", None)
        sd.pop("current_field", None)
        self.state_data = sd
        flag_modified(self, "state_data")
        self.updated_at = datetime.now(timezone.utc)

    @property
    def is_waiting_for_input(self) -> bool:
        """True when the flow is blocked waiting for user text input."""
        return bool((self.state_data or {}).get("waiting_for_input"))

    @property
    def current_input_field(self) -> Optional[str]:
        """The field name we are currently collecting, or None."""
        return (self.state_data or {}).get("current_field")
