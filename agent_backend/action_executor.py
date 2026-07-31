"""
Action Executor — orchestration pipeline.
==========================================

Flow:
1. Receives parsed AgentIntent from intent_parser
2. Looks up action in action_registry
3. Checks for missing required params → returns ASK_USER response
4. Validates all params
5. Executes action
6. Returns structured result
"""

import logging
from typing import Dict, Any, Optional

from .intent_parser import AgentIntent, intent_parser
from .action_registry import action_registry, BaseAction
from .session_manager import session_manager, AgentSession

logger = logging.getLogger(__name__)


def _build_response(
    status: str,
    message: str,
    data: Any = None,
    navigate_to: Optional[str] = None,
    ui_action: Optional[str] = None,
    next_step: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Standard agent response envelope."""
    resp: Dict[str, Any] = {
        "status": status,
        "message": message,
    }
    if data is not None:
        resp["data"] = data
    if navigate_to:
        resp["navigate_to"] = navigate_to
    if ui_action:
        resp["ui_action"] = ui_action
    if next_step:
        resp["next_step"] = next_step
    if session_id:
        resp["session_id"] = session_id
    return resp


class ActionExecutor:
    """Orchestrates intent → action lookup → validation → execution."""

    def process_message(
        self,
        message: str,
        workspace_id: str,
        session_id: Optional[str] = None,
        account_id: Optional[int] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        End-to-end processing of a single user message.

        Returns a dict ready to be JSON-serialised and sent to the frontend.
        """
        # 1. Get / create session
        session = session_manager.get_or_create(session_id, workspace_id)
        session.add_message("user", message)

        # Route the message, then ALWAYS persist the session's final state back to
        # the DB so the next turn — which may be served by a different gunicorn
        # worker — resumes the multi-turn flow instead of starting over.
        try:
            return self._route(message, session, workspace_id, account_id, user_id)
        finally:
            session_manager.persist(session)

    def _route(
        self,
        message: str,
        session: AgentSession,
        workspace_id: str,
        account_id: Optional[int],
        user_id: Optional[str],
    ) -> Dict[str, Any]:
        """Decide how to handle the (already-recorded) user message."""
        # 2. If we're in a multi-turn flow, try to collect the missing param
        if session.current_domain and session.current_action and session.missing_params:
            return self._handle_multiturn_input(message, session, workspace_id, account_id, user_id)

        # 2b. If awaiting confirmation
        if session.awaiting_confirmation:
            return self._handle_confirmation(message, session, workspace_id, account_id, user_id)

        # 3. Parse intent
        schema = action_registry.get_schema_for_intent_parser()
        logger.info(
            "ActionExecutor: schema has %d actions across %d domains",
            len(schema), len(action_registry.list_domains()),
        )
        intent = intent_parser.parse(
            message,
            action_schema=schema,
            conversation_history=session.history,
        )
        logger.info(
            "ActionExecutor: intent domain=%s action=%s confidence=%.2f params=%s",
            intent.domain, intent.action, intent.confidence, intent.params,
        )

        # 4. No match
        if not intent.domain or intent.confidence < 0.15:
            reply = (
                "I'm not sure what you'd like to do. Here are some things I can help with:\n"
                "• Show account details\n"
                "• List/create templates\n"
                "• Create drip campaigns\n"
                "• Send bulk messages\n"
                "• Setup automation rules\n"
                "• View analytics\n"
                "• Navigate to any page\n\n"
                "Try something like: \"Show my approved templates\" or \"Create a drip campaign\"."
            )
            session.add_message("agent", reply)
            return _build_response("no_match", reply, session_id=session.session_id)

        # 5. Look up action handler
        handler = action_registry.get(intent.domain, intent.action)
        if not handler:
            reply = f"I understand you want to **{intent.action}** in **{intent.domain}**, but that action isn't available yet."
            session.add_message("agent", reply)
            return _build_response("error", reply, session_id=session.session_id)

        # 6. Merge intent params into session
        session.current_domain = intent.domain
        session.current_action = intent.action
        session.collected_params.update(intent.params)

        # 7. Validate / check missing
        return self._validate_and_execute(handler, session, workspace_id, account_id, user_id)

    # ------------------------------------------------------------------ #

    def _handle_multiturn_input(
        self,
        message: str,
        session: AgentSession,
        workspace_id: str,
        account_id: Optional[int],
        user_id: Optional[str],
    ) -> Dict[str, Any]:
        """User is providing a missing param value in a multi-turn flow."""
        # Check if user wants to cancel
        if message.strip().lower() in ("cancel", "stop", "nevermind", "never mind", "quit"):
            session.clear_action_state()
            reply = "No problem, action cancelled. What else can I help you with?"
            session.add_message("agent", reply)
            return _build_response("cancelled", reply, session_id=session.session_id)

        # Assign the value to the first missing param
        param_name = session.missing_params[0]
        session.collected_params[param_name] = message.strip()
        session.missing_params.pop(0)

        handler = action_registry.get(session.current_domain, session.current_action)
        if not handler:
            session.clear_action_state()
            reply = "Something went wrong — the action is no longer available."
            session.add_message("agent", reply)
            return _build_response("error", reply, session_id=session.session_id)

        return self._validate_and_execute(handler, session, workspace_id, account_id, user_id)

    def _handle_confirmation(
        self,
        message: str,
        session: AgentSession,
        workspace_id: str,
        account_id: Optional[int],
        user_id: Optional[str],
    ) -> Dict[str, Any]:
        """User is responding yes/no to a confirmation prompt."""
        lower = message.strip().lower()
        if lower in ("yes", "y", "confirm", "ok", "sure", "do it", "go ahead", "proceed"):
            session.awaiting_confirmation = False
            handler = action_registry.get(session.current_domain, session.current_action)
            if handler:
                return self._execute_action(handler, session, workspace_id, account_id, user_id)
            session.clear_action_state()
            reply = "Something went wrong — the action is no longer available."
            session.add_message("agent", reply)
            return _build_response("error", reply, session_id=session.session_id)

        # No / cancel
        session.clear_action_state()
        reply = "Alright, cancelled. What else can I help with?"
        session.add_message("agent", reply)
        return _build_response("cancelled", reply, session_id=session.session_id)

    def _validate_and_execute(
        self,
        handler: BaseAction,
        session: AgentSession,
        workspace_id: str,
        account_id: Optional[int],
        user_id: Optional[str],
    ) -> Dict[str, Any]:
        """Check missing params; if all present, execute."""
        is_valid, missing = handler.validate(session.collected_params)

        if not is_valid:
            session.missing_params = missing
            prompt = handler.get_missing_params_prompt(missing)
            session.add_message("agent", prompt)
            return _build_response(
                "need_input",
                prompt,
                data={"missing_params": missing, "collected": session.collected_params},
                session_id=session.session_id,
                ui_action="ask_param",
            )

        # All params present — execute (some destructive actions need confirmation)
        return self._execute_action(handler, session, workspace_id, account_id, user_id)

    def _execute_action(
        self,
        handler: BaseAction,
        session: AgentSession,
        workspace_id: str,
        account_id: Optional[int],
        user_id: Optional[str],
    ) -> Dict[str, Any]:
        """Run the action handler and package the result."""
        context = {
            "workspace_id": workspace_id,
            "account_id": account_id,
            "user_id": user_id,
            "session": session,
        }

        try:
            result = handler.execute(session.collected_params, context)
        except Exception as exc:
            logger.exception("Action %s/%s failed", handler.domain, handler.action)
            session.clear_action_state()
            reply = f"Sorry, something went wrong while executing that action: {exc}"
            session.add_message("agent", reply)
            return _build_response("error", reply, session_id=session.session_id)

        # Package response
        status = result.get("status", "success")
        message = result.get("message", "Done!")

        session.add_message("agent", message)

        # If confirmation_required, set session state
        if status == "confirmation_required":
            session.awaiting_confirmation = True
            return _build_response(
                "confirmation_required",
                message,
                data=result.get("data"),
                session_id=session.session_id,
                ui_action="confirm_dialog",
            )

        # On success/error, clear multi-turn state
        session.clear_action_state()

        return _build_response(
            status,
            message,
            data=result.get("data"),
            navigate_to=result.get("navigate_to"),
            ui_action=result.get("ui_action"),
            next_step=result.get("next_step"),
            session_id=session.session_id,
        )


# Module-level singleton
action_executor = ActionExecutor()
