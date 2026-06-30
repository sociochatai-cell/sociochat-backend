"""
Automation Node Executor
========================

Executes individual nodes in a visual automation.
Handles message sending, delays, buttons, flows, and templates.

Works with the ConversationStateEngine to advance conversations.
"""

from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Callable
import logging
import asyncio

from shared_models import db
from whatsapp.visual_automation_models import WhatsAppVisualAutomation, WhatsAppConversationState
from whatsapp.conversation_state_engine import ConversationStateEngine
from whatsapp.template_node_executor import TemplateNodeExecutor

logger = logging.getLogger(__name__)


class NodeExecutor:
    """
    Executes automation nodes and sends appropriate WhatsApp messages.
    
    Usage:
        executor = NodeExecutor(workspace_id, state_engine, send_message_fn)
        result = executor.execute_node(state, node)
    """
    
    def __init__(
        self, 
        workspace_id: str,
        state_engine: ConversationStateEngine,
        send_message_fn: Callable[[str, str, Dict], Any],
        send_template_fn: Optional[Callable[[str, str, str, list], Any]] = None
    ):
        """
        Initialize the executor.
        
        Args:
            workspace_id: Current workspace ID
            state_engine: ConversationStateEngine instance
            send_message_fn: Function to send WhatsApp messages
                            Signature: (phone_number, message_type, payload) -> result
            send_template_fn: Function to send WhatsApp templates
                            Signature: (phone_number, template_name, language, components) -> result
        """
        self.workspace_id = workspace_id
        self.state_engine = state_engine
        self.send_message = send_message_fn
        self.send_template_fn = send_template_fn
    
    def execute_node(
        self, 
        state: WhatsAppConversationState, 
        node: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Execute a single node and return the result.
        
        Returns:
            Dict with keys:
                - success: bool
                - next_node_id: Optional[str]
                - wait_for_input: bool - True if waiting for user response
                - error: Optional[str]
        """
        node_type = node.get('type')
        node_id = node.get('id')
        node_data = node.get('data', {})
        
        logger.info(f"[Node Executor] Executing node: {node_id} (type={node_type})")
        
        try:
            if node_type == 'message':
                return self._execute_message_node(state, node_id, node_data)
            elif node_type == 'buttons':
                return self._execute_buttons_node(state, node_id, node_data)
            elif node_type == 'template':
                return self._execute_template_node(state, node_id, node_data, node)
            elif node_type == 'delay':
                return self._execute_delay_node(state, node_id, node_data)
            elif node_type == 'flow':
                return self._execute_flow_node(state, node_id, node_data)
            elif node_type == 'trigger':
                # Trigger nodes just pass through to next
                return {
                    'success': True,
                    'next_node_id': self._get_next_node_id(state),
                    'wait_for_input': False
                }
            elif node_type == 'end':
                # End nodes complete the automation
                self.state_engine.complete_automation(state)
                return {
                    'success': True,
                    'next_node_id': None,
                    'wait_for_input': False,
                    'completed': True
                }
            else:
                logger.warning(f"[Node Executor] Unknown node type: {node_type}")
                return {
                    'success': False,
                    'error': f'Unknown node type: {node_type}'
                }
        
        except Exception as e:
            logger.error(f"[Node Executor] Error executing node {node_id}: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def _execute_message_node(
        self, 
        state: WhatsAppConversationState, 
        node_id: str, 
        data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute a message node - sends a text message."""
        text = data.get('text', '')
        
        if not text:
            return {'success': False, 'error': 'Message text is empty'}
        
        # Check 24h window
        if not self.state_engine.can_send_message(state):
            logger.warning(f"[Node Executor] Outside 24h window for {state.phone_number}")
            return {
                'success': False,
                'error': '24-hour messaging window has expired'
            }
        
        # Send the message
        try:
            result = self.send_message(
                state.phone_number,
                'text',
                {'body': text}
            )
            
            logger.info(f"[Automation Source: VISUAL_AUTOMATION] Sent message to {state.phone_number}")
            
            # Advance to next node
            next_node_id = self._get_next_node_id(state)
            self.state_engine.advance_to_node(state, next_node_id) if next_node_id else None
            
            return {
                'success': True,
                'next_node_id': next_node_id,
                'wait_for_input': False
            }
        
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def _execute_buttons_node(
        self, 
        state: WhatsAppConversationState, 
        node_id: str, 
        data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute a buttons node - sends an interactive button message."""
        body_text = data.get('bodyText', '')
        buttons = data.get('buttons', [])
        
        if not body_text:
            return {'success': False, 'error': 'Button message body is empty'}
        
        if not buttons:
            return {'success': False, 'error': 'No buttons configured'}
        
        # Check 24h window
        if not self.state_engine.can_send_message(state):
            return {
                'success': False,
                'error': '24-hour messaging window has expired'
            }
        
        # Format buttons for WhatsApp API
        formatted_buttons = []
        for btn in buttons[:3]:  # Max 3 buttons
            formatted_buttons.append({
                'type': 'reply',
                'reply': {
                    'id': btn.get('id', f'btn_{len(formatted_buttons)}'),
                    'title': btn.get('text', 'Button')[:20]  # Max 20 chars
                }
            })
        
        # Send interactive message
        try:
            result = self.send_message(
                state.phone_number,
                'interactive',
                {
                    'type': 'button',
                    'body': {'text': body_text},
                    'action': {'buttons': formatted_buttons}
                }
            )
            
            logger.info(f"[Automation Source: VISUAL_AUTOMATION] Sent buttons to {state.phone_number}")
            
            # Stay on this node, waiting for button click
            return {
                'success': True,
                'next_node_id': None,  # Don't advance yet
                'wait_for_input': True  # Waiting for button response
            }
        
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def _execute_template_node(
        self, 
        state: WhatsAppConversationState, 
        node_id: str, 
        data: Dict[str, Any],
        node: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Execute a template node - sends an approved WhatsApp template.
        
        Templates can have quick reply buttons that route to other nodes.
        The button payloads encode the automation_id and target_node_id.
        """
        template_name = data.get('templateName', '')
        
        if not template_name:
            return {'success': False, 'error': 'No template selected'}
        
        if not self.send_template_fn:
            return {'success': False, 'error': 'Template sending not configured'}
        
        # Note: Templates can be sent outside 24h window, so we don't check it
        
        # Get runtime variables from conversation context
        runtime_variables = self._get_runtime_variables(state)
        
        # Create template executor
        executor = TemplateNodeExecutor(
            workspace_id=self.workspace_id,
            account_id=state.automation.account_id if state.automation else 0,
            send_template_fn=self.send_template_fn
        )
        
        # Execute the template node
        result = executor.execute_template_node(state, node, runtime_variables)
        
        if result.get('success'):
            logger.info(
                f"[Automation Source: VISUAL_AUTOMATION] Sent template '{template_name}' "
                f"to {state.phone_number}"
            )
            
            # If template has routing buttons, stay on this node waiting for click
            if result.get('wait_for_input'):
                return {
                    'success': True,
                    'next_node_id': None,  # Determined by button click
                    'wait_for_input': True
                }
            else:
                # No routing buttons, advance to next node
                next_node_id = self._get_next_node_id(state)
                if next_node_id:
                    self.state_engine.advance_to_node(state, next_node_id)
                return {
                    'success': True,
                    'next_node_id': next_node_id,
                    'wait_for_input': False
                }
        
        return result
    
    def _get_runtime_variables(self, state: WhatsAppConversationState) -> Dict[str, Any]:
        """
        Get runtime variables for template substitution.
        
        These can be used in template variables like {{customer_name}}.
        """
        variables = {}
        
        # Get state data if any
        if state.state_data:
            variables.update(state.state_data)
        
        # Try to get conversation/contact info
        try:
            from whatsapp.models import WhatsAppConversation
            conversation = WhatsAppConversation.query.get(state.conversation_id)
            if conversation:
                variables['phone_number'] = conversation.phone_number or state.phone_number
                variables['contact_name'] = conversation.contact_name or ''
                variables['customer_name'] = conversation.contact_name or ''
        except Exception as e:
            logger.warning(f"[Node Executor] Could not get conversation info: {e}")
        
        return variables
    
    def _execute_delay_node(
        self, 
        state: WhatsAppConversationState, 
        node_id: str, 
        data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Execute a delay node - schedules next execution after delay.
        
        Note: Actual delay scheduling should be handled by a task queue
        (like Celery). This just returns the delay info.
        """
        delay_value = data.get('delaySeconds', data.get('value', 0))
        delay_unit = data.get('delayUnit', data.get('unit', 'seconds'))
        
        # Convert to seconds
        multipliers = {
            'seconds': 1,
            'minutes': 60,
            'hours': 3600,
            'days': 86400
        }
        delay_seconds = delay_value * multipliers.get(delay_unit, 1)
        
        # Calculate when to resume
        resume_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        
        # Store delay info in state
        self.state_engine.set_state_data(state, 'delay_resume_at', resume_at.isoformat())
        
        # Get next node ID for when delay completes
        next_node_id = self._get_next_node_id(state)
        
        logger.info(f"[Node Executor] Delay node: wait {delay_seconds}s, resume at {resume_at}")
        
        return {
            'success': True,
            'next_node_id': next_node_id,
            'wait_for_input': False,
            'delay_seconds': delay_seconds,
            'resume_at': resume_at.isoformat()
        }
    
    def _execute_flow_node(
        self, 
        state: WhatsAppConversationState, 
        node_id: str, 
        data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute a flow node - sends a WhatsApp Flow message."""
        flow_id = data.get('flowId', '')
        cta_text = data.get('ctaText', 'Open Form')
        body_text = data.get('bodyText', 'Tap below to continue.')
        header_text = data.get('headerText', '')
        
        if not flow_id:
            return {'success': False, 'error': 'No flow selected'}
        
        # Check 24h window
        if not self.state_engine.can_send_message(state):
            return {
                'success': False,
                'error': '24-hour messaging window has expired'
            }
        
        # Send flow message
        try:
            flow_payload = {
                'type': 'flow',
                'body': {'text': body_text},
                'action': {
                    'name': 'flow',
                    'parameters': {
                        'flow_message_version': '3',
                        'flow_token': f'flow_{state.id}_{node_id}',
                        'flow_id': flow_id,
                        'flow_cta': cta_text,
                        'mode': 'published'
                    }
                }
            }
            
            if header_text:
                flow_payload['header'] = {'type': 'text', 'text': header_text}
            
            result = self.send_message(
                state.phone_number,
                'interactive',
                flow_payload
            )
            
            logger.info(f"[Automation Source: VISUAL_AUTOMATION] Sent flow {flow_id} to {state.phone_number}")
            
            # Stay on this node, waiting for flow completion
            return {
                'success': True,
                'next_node_id': None,
                'wait_for_input': True  # Waiting for flow response
            }
        
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def _get_next_node_id(self, state: WhatsAppConversationState) -> Optional[str]:
        """Get the next node ID following the edges."""
        automation = WhatsAppVisualAutomation.query.get(state.automation_id)
        if not automation:
            return None
        
        edges = automation.edges or []
        for edge in edges:
            if edge.get('source') == state.current_node_id:
                return edge.get('target')
        
        return None
    
    def execute_sequence(
        self, 
        state: WhatsAppConversationState,
        max_nodes: int = 10
    ) -> Dict[str, Any]:
        """
        Execute a sequence of nodes until we hit a wait point or end.
        
        Stops when:
        - We hit a node that requires user input (buttons, flow)
        - We hit a delay node
        - We reach the end of the automation
        - We've executed max_nodes (safety limit)
        
        Returns summary of execution.
        """
        results = []
        nodes_executed = 0
        
        while nodes_executed < max_nodes:
            current_node = self.state_engine.get_current_node(state)
            
            if not current_node:
                # End of automation
                self.state_engine.complete_automation(state)
                break
            
            result = self.execute_node(state, current_node)
            results.append({
                'node_id': current_node.get('id'),
                'node_type': current_node.get('type'),
                'result': result
            })
            
            nodes_executed += 1
            
            if not result.get('success'):
                # Error occurred
                break
            
            if result.get('wait_for_input'):
                # Waiting for user response
                break
            
            if result.get('delay_seconds'):
                # Delay node - schedule continuation
                break
            
            next_node_id = result.get('next_node_id')
            if not next_node_id:
                # End of automation
                self.state_engine.complete_automation(state)
                break
            
            # Advance to next node
            self.state_engine.advance_to_node(state, next_node_id)
        
        return {
            'nodes_executed': nodes_executed,
            'results': results,
            'state': state.to_dict()
        }
