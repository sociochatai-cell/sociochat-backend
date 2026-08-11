"""
Conversation State Engine
=========================

The brain of the visual automation system.
Tracks where each user is in their automation flow and handles transitions.

Key responsibilities:
- Track conversation state per phone number
- Handle button click branching
- Resume conversations correctly
- Respect 24-hour messaging window
- Execute nodes in sequence
"""

from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List, Tuple
import logging

from shared_models import db
from whatsapp.visual_automation_models import (
    WhatsAppVisualAutomation,
    WhatsAppConversationState
)

logger = logging.getLogger(__name__)


class ConversationStateEngine:
    """
    Manages conversation state for visual automations.
    
    Usage:
        engine = ConversationStateEngine(workspace_id)
        
        # When a message arrives:
        state, automation = engine.get_or_create_state(phone_number, trigger_info)
        
        # When a button is clicked:
        next_node = engine.handle_button_click(state, button_id)
        
        # To advance to next node:
        engine.advance_state(state, next_node_id)
    """
    
    # 24-hour window in seconds
    WINDOW_24H = 24 * 60 * 60
    
    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
    
    # =========================================
    # STATE RETRIEVAL & CREATION
    # =========================================
    
    def get_active_state(self, phone_number: str) -> Optional[WhatsAppConversationState]:
        """
        Get the active conversation state for a phone number.
        Returns None if no active state exists.
        """
        state = WhatsAppConversationState.query.filter_by(
            workspace_id=self.workspace_id,
            phone_number=phone_number,
            is_active=True
        ).order_by(WhatsAppConversationState.created_at.desc()).first()
        
        if state:
            # Check if still within 24h window
            if not self._is_within_24h_window(state):
                state.is_within_24h_window = False
                db.session.commit()
        
        return state
    
    def get_or_create_state(
        self, 
        phone_number: str,
        conversation_id: int,
        automation_id: int,
        start_node_id: str
    ) -> WhatsAppConversationState:
        """
        Get existing active state or create a new one.
        Deactivates any previous states for this phone number.
        """
        # Deactivate any existing active states
        existing = WhatsAppConversationState.query.filter_by(
            workspace_id=self.workspace_id,
            phone_number=phone_number,
            is_active=True
        ).all()
        
        for old_state in existing:
            old_state.is_active = False
            old_state.completed_at = datetime.now(timezone.utc)
        
        # Create new state
        new_state = WhatsAppConversationState(
            workspace_id=self.workspace_id,
            conversation_id=conversation_id,
            phone_number=phone_number,
            automation_id=automation_id,
            current_node_id=start_node_id,
            last_user_message_at=datetime.now(timezone.utc),
            is_within_24h_window=True,
            is_active=True,
            state_data={}
        )
        
        db.session.add(new_state)
        db.session.commit()
        
        logger.info(f"[State Engine] Created new state for {phone_number}, automation={automation_id}")
        
        return new_state
    
    # =========================================
    # STATE TRANSITIONS
    # =========================================
    
    def advance_to_node(self, state: WhatsAppConversationState, node_id: str) -> None:
        """Move the conversation to a new node."""
        state.current_node_id = node_id
        state.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        
        logger.info(f"[State Engine] Advanced {state.phone_number} to node: {node_id}")
    
    def record_user_message(self, state: WhatsAppConversationState) -> None:
        """Record that user sent a message (resets 24h window)."""
        state.last_user_message_at = datetime.now(timezone.utc)
        state.is_within_24h_window = True
        state.updated_at = datetime.now(timezone.utc)
        db.session.commit()
    
    def record_button_click(
        self, 
        state: WhatsAppConversationState, 
        button_id: str
    ) -> Optional[str]:
        """
        Record a button click and return the next node ID.
        
        Returns:
            The next node ID based on the button's configuration, or None.
        """
        state.last_button_clicked = button_id
        state.updated_at = datetime.now(timezone.utc)
        
        # Also counts as user interaction for 24h window
        state.last_user_message_at = datetime.now(timezone.utc)
        state.is_within_24h_window = True
        
        db.session.commit()
        
        # Find next node based on button configuration
        next_node_id = self._get_next_node_for_button(state, button_id)
        
        logger.info(f"[State Engine] Button click: {button_id} -> next node: {next_node_id}")
        
        return next_node_id
    
    def complete_automation(self, state: WhatsAppConversationState) -> None:
        """Mark the automation as completed for this conversation."""
        state.is_active = False
        state.completed_at = datetime.now(timezone.utc)
        state.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        
        logger.info(f"[State Engine] Completed automation for {state.phone_number}")
    
    # =========================================
    # NODE RETRIEVAL
    # =========================================
    
    def get_current_node(
        self, 
        state: WhatsAppConversationState
    ) -> Optional[Dict[str, Any]]:
        """Get the current node data from the automation."""
        if not state.automation_id or not state.current_node_id:
            return None
        
        automation = WhatsAppVisualAutomation.query.get(state.automation_id)
        if not automation:
            return None
        
        # Find node in automation's nodes array
        nodes = automation.nodes or []
        for node in nodes:
            if node.get('id') == state.current_node_id:
                return node
        
        return None
    
    def get_next_node(
        self, 
        state: WhatsAppConversationState
    ) -> Optional[Dict[str, Any]]:
        """
        Get the next node in the sequence.
        Follows edges from current node.
        """
        if not state.automation_id or not state.current_node_id:
            return None
        
        automation = WhatsAppVisualAutomation.query.get(state.automation_id)
        if not automation:
            return None
        
        # Find edge from current node
        edges = automation.edges or []
        next_node_id = None
        
        for edge in edges:
            if edge.get('source') == state.current_node_id:
                next_node_id = edge.get('target')
                break
        
        if not next_node_id:
            return None
        
        # Find the target node
        nodes = automation.nodes or []
        for node in nodes:
            if node.get('id') == next_node_id:
                return node
        
        return None
    
    def _get_next_node_for_button(
        self, 
        state: WhatsAppConversationState, 
        button_id: str
    ) -> Optional[str]:
        """
        Find the next node based on which button was clicked.
        Handles button branching logic.
        """
        current_node = self.get_current_node(state)
        if not current_node:
            return None
        
        node_type = current_node.get('type')
        node_data = current_node.get('data', {})
        
        if node_type != 'buttons':
            return None
        
        # Find the button configuration
        buttons = node_data.get('buttons', [])
        for button in buttons:
            if button.get('id') == button_id:
                # Check if button has specific next node
                next_node = button.get('nextNodeId')
                if next_node:
                    return next_node
                
                # Check button action type
                action_type = button.get('actionType')
                if action_type == 'trigger_automation':
                    # Handle automation trigger
                    return None  # Will be handled separately
                elif action_type == 'start_flow':
                    # Handle flow start
                    return None  # Will be handled separately
                
        # Default: follow the normal edge
        return self._get_default_next_node_id(state)
    
    def _get_default_next_node_id(
        self, 
        state: WhatsAppConversationState
    ) -> Optional[str]:
        """Get the default next node ID following edges."""
        if not state.automation_id or not state.current_node_id:
            return None
        
        automation = WhatsAppVisualAutomation.query.get(state.automation_id)
        if not automation:
            return None
        
        edges = automation.edges or []
        for edge in edges:
            if edge.get('source') == state.current_node_id:
                return edge.get('target')
        
        return None
    
    # =========================================
    # 24-HOUR WINDOW MANAGEMENT
    # =========================================
    
    def _is_within_24h_window(self, state: WhatsAppConversationState) -> bool:
        """Check if we're still within the 24-hour messaging window."""
        if not state.last_user_message_at:
            return False
        
        now = datetime.now(timezone.utc)
        last_message = state.last_user_message_at
        
        # Make timezone aware if needed
        if last_message.tzinfo is None:
            last_message = last_message.replace(tzinfo=timezone.utc)
        
        elapsed = (now - last_message).total_seconds()
        return elapsed < self.WINDOW_24H
    
    def check_24h_window(self, state: WhatsAppConversationState) -> Tuple[bool, int]:
        """
        Check 24-hour window status.
        
        Returns:
            Tuple of (is_within_window, seconds_remaining)
        """
        if not state.last_user_message_at:
            return False, 0
        
        now = datetime.now(timezone.utc)
        last_message = state.last_user_message_at
        
        if last_message.tzinfo is None:
            last_message = last_message.replace(tzinfo=timezone.utc)
        
        elapsed = (now - last_message).total_seconds()
        remaining = max(0, int(self.WINDOW_24H - elapsed))
        
        return elapsed < self.WINDOW_24H, remaining
    
    def can_send_message(self, state: WhatsAppConversationState) -> bool:
        """
        Check if we can send a message to this conversation.
        Must be within 24-hour window for non-template messages.
        """
        is_within, _ = self.check_24h_window(state)
        return is_within
    
    # =========================================
    # STATE DATA MANAGEMENT
    # =========================================
    
    def set_state_data(
        self, 
        state: WhatsAppConversationState, 
        key: str, 
        value: Any
    ) -> None:
        """Store a value in the conversation state data."""
        state_data = state.state_data or {}
        state_data[key] = value
        state.state_data = state_data
        state.updated_at = datetime.now(timezone.utc)
        db.session.commit()
    
    def get_state_data(
        self, 
        state: WhatsAppConversationState, 
        key: str, 
        default: Any = None
    ) -> Any:
        """Retrieve a value from the conversation state data."""
        state_data = state.state_data or {}
        return state_data.get(key, default)
    
    def clear_state_data(self, state: WhatsAppConversationState) -> None:
        """Clear all state data."""
        state.state_data = {}
        state.updated_at = datetime.now(timezone.utc)
        db.session.commit()
    
    # =========================================
    # TRIGGER MATCHING
    # =========================================
    
    def find_matching_automation(
        self, 
        message_text: str,
        account_id: int
    ) -> Optional[WhatsAppVisualAutomation]:
        """
        Find an automation that matches the incoming message.
        
        Checks trigger types in order:
        1. Keyword match
        2. Template reply
        3. Any message (fallback)
        """
        # Get all active automations for this workspace
        automations = WhatsAppVisualAutomation.query.filter_by(
            workspace_id=self.workspace_id,
            account_id=account_id,
            is_active=True
        ).all()
        
        any_message_automation = None
        
        for automation in automations:
            trigger_type = automation.trigger_type
            trigger_config = automation.trigger_config or {}
            
            if trigger_type == 'keyword':
                keywords = trigger_config.get('keywords', [])
                match_type = trigger_config.get('matchType', 'contains')
                
                if self._matches_keyword(message_text, keywords, match_type):
                    logger.info(f"[State Engine] Matched keyword trigger: {automation.name}")
                    return automation
            
            elif trigger_type in ('any_message', 'any_reply'):
                # Store for fallback — accept both legacy 'any_message' and
                # the frontend's newer 'any_reply' slug for the same behaviour.
                any_message_automation = automation

        # Return any_message / any_reply automation as fallback
        if any_message_automation:
            logger.info(f"[State Engine] Using {any_message_automation.trigger_type} trigger: {any_message_automation.name}")
            return any_message_automation
        
        return None
    
    def _matches_keyword(
        self, 
        message_text: str, 
        keywords: List[str], 
        match_type: str
    ) -> bool:
        """Check if message matches any of the keywords."""
        message_lower = message_text.lower().strip()
        
        for keyword in keywords:
            keyword_lower = keyword.lower().strip()
            
            if match_type == 'exact':
                if message_lower == keyword_lower:
                    return True
            elif match_type == 'starts_with':
                if message_lower.startswith(keyword_lower):
                    return True
            else:  # contains (default)
                if keyword_lower in message_lower:
                    return True
        
        return False
    
    # =========================================
    # STATISTICS
    # =========================================
    
    def get_active_states_count(self) -> int:
        """Get count of active conversation states."""
        return WhatsAppConversationState.query.filter_by(
            workspace_id=self.workspace_id,
            is_active=True
        ).count()
    
    def get_states_by_automation(self, automation_id: int) -> List[WhatsAppConversationState]:
        """Get all active states for a specific automation."""
        return WhatsAppConversationState.query.filter_by(
            workspace_id=self.workspace_id,
            automation_id=automation_id,
            is_active=True
        ).all()
