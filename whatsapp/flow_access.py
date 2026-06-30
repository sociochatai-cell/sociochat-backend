"""
Flow Access Control - Multi-Business Isolation (Phase 4)
=========================================================

Ensures flows are properly scoped per WABA/workspace.
Prevents cross-tenant data access.

Access Control Rules:
1. Flows belong to a single WhatsApp Account (WABA)
2. Users can only access flows for accounts they have access to
3. Template flow buttons can only reference flows from same WABA
4. Published flows are visible only within their workspace

Usage:
    from whatsapp.flow_access import require_flow_access, validate_flow_account_match

    @flow_bp.route("/<int:flow_id>", methods=["GET"])
    @require_flow_access
    def get_flow(flow_id):
        ...
"""

import os
from functools import wraps
from typing import Optional, List, Tuple
from flask import request, jsonify, g

from .models import WhatsAppFlow, WhatsAppAccount


def _strict_workspace_access() -> bool:
    """Require workspace scoping on API routes (default on Cloud Run)."""
    flag = (os.getenv("STRICT_WORKSPACE_ACCESS") or "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    if flag in {"0", "false", "no", "off"}:
        return False
    return bool(os.getenv("K_SERVICE"))


def _resolve_workspace_id() -> Optional[str]:
    return (
        request.headers.get("X-Workspace-ID")
        or request.args.get("workspace_id")
        or (request.get_json(silent=True) or {}).get("workspace_id")
        or request.form.get("workspace_id")
    )


def _require_workspace_scope(account_workspace_id: Optional[str]) -> Optional[Tuple[dict, int]]:
    user_workspace_id = _resolve_workspace_id()
    if not user_workspace_id:
        if _strict_workspace_access():
            return {"success": False, "error": "workspace_id required (X-Workspace-ID header)"}, 401
        return None
    if account_workspace_id and str(account_workspace_id) != str(user_workspace_id):
        return {"success": False, "error": "Access denied: resource belongs to a different workspace"}, 403
    return None


META_FLOW_CATEGORY_ALIASES = {
    "LEAD_GEN": "LEAD_GENERATION",
    "LEADS": "LEAD_GENERATION",
    "LEAD_GENERATION": "LEAD_GENERATION",
    "SURVEY": "SURVEY",
    "BOOKING": "APPOINTMENT_BOOKING",
    "APPOINTMENT_BOOKING": "APPOINTMENT_BOOKING",
    "FEEDBACK": "SURVEY",
    "CUSTOMER_SUPPORT": "CUSTOMER_SUPPORT",
    "SUPPORT": "CUSTOMER_SUPPORT",
    "CONTACT_US": "CONTACT_US",
    "CONTACT": "CONTACT_US",
    "SIGN_UP": "SIGN_UP",
    "SIGNUP": "SIGN_UP",
    "SIGN_IN": "SIGN_IN",
    "SIGNIN": "SIGN_IN",
    "CUSTOM": "OTHER",
    "OTHER": "OTHER",
}


# ============================================================
# Access Control Decorators
# ============================================================

def require_flow_access(f):
    """
    Decorator to verify user has access to the requested flow.
    
    Checks:
    1. Flow exists
    2. User has access to the flow's account/workspace
    3. Attaches flow and account to request context (g)
    """
    @wraps(f)
    def wrapper(flow_id: int, *args, **kwargs):
        # Get the flow
        flow = WhatsAppFlow.query.get(flow_id)
        if not flow:
            return jsonify({"success": False, "error": "Flow not found"}), 404
        
        # Get the account
        account = flow.account
        if not account:
            return jsonify({"success": False, "error": "Flow account not found"}), 404
        
        denied = _require_workspace_scope(account.workspace_id)
        if denied:
            body, status = denied
            return jsonify(body), status
        
        # Attach to request context for use in route
        g.flow = flow
        g.account = account
        
        return f(flow_id, *args, **kwargs)
    
    return wrapper


def require_account_access(f):
    """
    Decorator to verify user has access to the requested account.
    
    For routes that take account_id as parameter or in request body.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        # Get account_id from various sources
        account_id = (
            kwargs.get("account_id") or
            request.args.get("account_id", type=int) or
            (request.get_json() or {}).get("account_id")
        )
        
        if not account_id:
            return jsonify({"success": False, "error": "account_id is required"}), 400
        
        account = WhatsAppAccount.query.get(account_id)
        if not account:
            return jsonify({"success": False, "error": "Account not found"}), 404
        
        denied = _require_workspace_scope(account.workspace_id)
        if denied:
            body, status = denied
            return jsonify(body), status

        user_workspace_id = _resolve_workspace_id()

        # Attach to context
        g.account = account
        
        # Inject into kwargs for the route handler if it expects them
        # Most of our new routes expect (account, workspace_id)
        kwargs['account'] = account
        kwargs['workspace_id'] = (
            str(account.workspace_id)
            if account.workspace_id
            else str(user_workspace_id) if user_workspace_id else None
        )

        return f(*args, **kwargs)
    
    return wrapper


# ============================================================
# Validation Functions
# ============================================================

def validate_flow_account_match(flow_id: str, account_id: int) -> Tuple[bool, Optional[str]]:
    """
    Validate that a flow belongs to the specified account.
    
    Used when attaching flows to templates - ensures template and flow
    belong to the same WABA.
    
    Args:
        flow_id: Meta flow ID or local flow ID
        account_id: Account ID to check against
        
    Returns:
        (is_valid, error_message)
    """
    flow = get_flow_by_reference(flow_id)
    
    if not flow:
        return False, f"Flow '{flow_id}' not found"
    
    if flow.account_id != account_id:
        return False, "Flow belongs to a different WhatsApp account"
    
    if flow.status != "PUBLISHED":
        return False, f"Flow is not published (status: {flow.status})"
    
    return True, None


def get_accessible_flows(account_id: int, workspace_id: Optional[str] = None) -> List[WhatsAppFlow]:
    """
    Get all flows accessible to a user based on their account/workspace.
    
    Args:
        account_id: The WhatsApp account ID
        workspace_id: Optional workspace ID for additional filtering
        
    Returns:
        List of accessible flows
    """
    query = WhatsAppFlow.query.filter_by(account_id=account_id)
    
    # If workspace_id is provided, ensure account belongs to that workspace
    if workspace_id:
        account = WhatsAppAccount.query.get(account_id)
        if account and str(account.workspace_id) != str(workspace_id):
            return []  # No access
    
    return query.order_by(WhatsAppFlow.name, WhatsAppFlow.flow_version.desc()).all()


def validate_template_flow_attachment(template_account_id: int, flow_id: str) -> Tuple[bool, Optional[str]]:
    """
    Validate that a flow can be attached to a template.
    
    Rules:
    1. Flow must exist
    2. Flow must belong to the same WABA as the template
    3. Flow must be PUBLISHED
    
    Args:
        template_account_id: Account ID of the template
        flow_id: Meta flow ID to attach
        
    Returns:
        (is_valid, error_message)
    """
    flow, error = resolve_template_flow_attachment(template_account_id, flow_id)
    return flow is not None, error


def normalize_flow_category(category: Optional[str]) -> str:
    """Map legacy/internal flow categories to Meta-accepted category values."""
    normalized = str(category or "OTHER").strip().upper()
    return META_FLOW_CATEGORY_ALIASES.get(normalized, normalized or "OTHER")


def get_flow_by_reference(flow_id: Optional[str]) -> Optional[WhatsAppFlow]:
    """Resolve a flow by Meta flow ID first, then local numeric ID."""
    ref = str(flow_id or "").strip()
    if not ref:
        return None

    flow = WhatsAppFlow.query.filter_by(meta_flow_id=ref).first()
    if not flow and ref.isdigit():
        flow = WhatsAppFlow.query.get(int(ref))
    return flow


def resolve_template_flow_attachment(
    template_account_id: int,
    flow_id: Optional[str],
) -> Tuple[Optional[WhatsAppFlow], Optional[str]]:
    """Resolve and validate a flow before attaching it to a template."""
    flow = get_flow_by_reference(flow_id)
    if not flow:
        return None, f"Flow with ID '{flow_id}' not found. Make sure the flow is published."

    if flow.account_id != template_account_id:
        return None, "Flow must belong to the same WhatsApp Business Account as the template"

    if flow.status != "PUBLISHED":
        return None, f"Only published flows can be attached to templates. Current status: {flow.status}"

    return flow, None


# ============================================================
# Middleware for Session-Based Access (Optional)
# ============================================================

def check_user_workspace_access(user_id: str, workspace_id: str) -> bool:
    """
    Check if a user has access to a workspace.
    
    This integrates with your existing user/workspace system.
    Implement based on your authentication setup.
    
    Args:
        user_id: The authenticated user's ID
        workspace_id: The workspace to check access for
        
    Returns:
        True if user has access, False otherwise
    """
    # TODO: Implement based on your user/workspace model
    # Example:
    # from models import WorkspaceMember
    # member = WorkspaceMember.query.filter_by(
    #     user_id=user_id, 
    #     workspace_id=workspace_id
    # ).first()
    # return member is not None
    
    # For now, allow all access (implement in production)
    return True


def get_user_account_ids(user_id: str) -> List[int]:
    """
    Get all WhatsApp account IDs a user has access to.
    
    Args:
        user_id: The authenticated user's ID
        
    Returns:
        List of account IDs the user can access
    """
    # TODO: Implement based on your user/workspace/account relationships
    # Example:
    # accounts = WhatsAppAccount.query.join(
    #     WorkspaceMember, 
    #     WhatsAppAccount.workspace_id == WorkspaceMember.workspace_id
    # ).filter(
    #     WorkspaceMember.user_id == user_id
    # ).all()
    # return [a.id for a in accounts]
    
    # For now, return all accounts (implement in production)
    return [a.id for a in WhatsAppAccount.query.all()]


# ============================================================
# Access Audit Logging (Recommended for Production)
# ============================================================

def log_flow_access(user_id: Optional[str], flow_id: int, action: str, success: bool):
    """
    Log flow access attempts for audit purposes.
    
    Args:
        user_id: ID of user attempting access (None if anonymous)
        flow_id: ID of the flow being accessed
        action: Action being performed (create, read, update, delete, publish)
        success: Whether the action was successful
    """
    # TODO: Implement audit logging
    # Example:
    # audit_log = FlowAccessLog(
    #     user_id=user_id,
    #     flow_id=flow_id,
    #     action=action,
    #     success=success,
    #     timestamp=datetime.now(timezone.utc),
    #     ip_address=request.remote_addr
    # )
    # db.session.add(audit_log)
    # db.session.commit()
    pass
