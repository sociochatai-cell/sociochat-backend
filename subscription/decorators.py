"""
Subscription Module - Decorators
================================

Flask route decorators for feature gating and limit enforcement.
"""

from functools import wraps
from flask import request, session, jsonify, g

from shared_models import db, User
from subscription.service import (
    check_feature_access, 
    check_message_limit,
    check_image_credits,
    check_flow_limit,
    check_ad_spend_limit,
    check_workspace_limit
)


def get_current_user():
    """Get current user from the server session or a SIGNED Bearer JWT only.

    The forgeable X-User-Id header is no longer trusted (see auth_core) — trusting
    it let any caller impersonate any user and bypass plan/quota gating.
    """
    from auth_core import authenticated_user_id
    user_id = authenticated_user_id()
    if not user_id:
        return None
    try:
        return db.session.get(User, int(user_id))
    except Exception:
        return None


def require_feature(feature_name: str):
    """
    Decorator to require a specific feature.
    
    Usage:
        @app.route("/api/generate-image")
        @require_feature("image_generation")
        def generate_image():
            ...
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            user = get_current_user()
            if not user:
                return jsonify({
                    "success": False,
                    "error": "authentication_required",
                    "message": "Please log in to access this feature."
                }), 401
            
            allowed, error_message = check_feature_access(user, feature_name)
            if not allowed:
                return jsonify({
                    "success": False,
                    "error": "feature_not_available",
                    "feature": feature_name,
                    "plan": user.plan,
                    "message": error_message
                }), 403
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator



""""
7	Whatsapp Smart AI
8	Human Agent (Whatsapp)  
9	Number of users
10	Number of messages per day
11	Number of interactive flows"""

def require_message_limit(workspace_id_param: str = "workspace_id"):
    """
    Decorator to enforce message limits.
    
    Usage:
        @app.route("/api/send-message")
        @require_message_limit()
        def send_message():
            ...
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            user = get_current_user()
            if not user:
                return jsonify({
                    "success": False,
                    "error": "authentication_required"
                }), 401
            
            # Get workspace_id from various sources
            workspace_id = (
                kwargs.get(workspace_id_param) or 
                request.args.get(workspace_id_param) or
                request.headers.get("X-Workspace-ID") or
                (request.get_json() or {}).get(workspace_id_param)
            )
            
            try:
                workspace_id = int(workspace_id) if workspace_id else None
            except (ValueError, TypeError):
                workspace_id = None
            
            allowed, current, limit = check_message_limit(user, workspace_id)
            if not allowed:
                return jsonify({
                    "success": False,
                    "error": "message_limit_exceeded",
                    "current": current,
                    "limit": limit,
                    "plan": user.plan,
                    "message": f"Daily message limit of {limit} reached. Please upgrade your plan."
                }), 429
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def require_image_credits(credits_needed: int = 1):
    """
    Decorator to enforce image credit limits.
    
    Usage:
        @app.route("/api/generate-image")
        @require_image_credits(credits_needed=3)
        def generate_image():
            ...
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            user = get_current_user()
            if not user:
                return jsonify({
                    "success": False,
                    "error": "authentication_required"
                }), 401
            
            allowed, used, limit = check_image_credits(user)
            
            # Check if enough credits remaining
            if limit != -1 and (used + credits_needed) > limit:
                return jsonify({
                    "success": False,
                    "error": "image_credits_exceeded",
                    "used": used,
                    "limit": limit,
                    "needed": credits_needed,
                    "plan": user.plan,
                    "message": f"Image credit limit of {limit} reached. Used: {used}, Needed: {credits_needed}. Please upgrade your plan."
                }), 429
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def require_workspace_limit():
    """
    Decorator to enforce workspace creation limits.
    
    Usage:
        @app.route("/api/workspaces", methods=["POST"])
        @require_workspace_limit()
        def create_workspace():
            ...
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            user = get_current_user()
            if not user:
                return jsonify({
                    "success": False,
                    "error": "authentication_required"
                }), 401
            
            allowed, current, limit = check_workspace_limit(user)
            if not allowed:
                return jsonify({
                    "success": False,
                    "error": "workspace_limit_exceeded",
                    "current": current,
                    "limit": limit,
                    "plan": user.plan,
                    "message": f"Workspace limit of {limit} reached. Please upgrade your plan."
                }), 403
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def require_flow_limit(workspace_id_param: str = "workspace_id"):
    """
    Decorator to enforce interactive flow limits.
    
    Usage:
        @app.route("/api/flows", methods=["POST"])
        @require_flow_limit()
        def create_flow():
            ...
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            user = get_current_user()
            if not user:
                return jsonify({
                    "success": False,
                    "error": "authentication_required"
                }), 401
            
            workspace_id = (
                kwargs.get(workspace_id_param) or 
                request.args.get(workspace_id_param) or
                request.headers.get("X-Workspace-ID") or
                (request.get_json() or {}).get(workspace_id_param)
            )
            
            if not workspace_id:
                return jsonify({
                    "success": False,
                    "error": "workspace_id_required"
                }), 400
            
            try:
                workspace_id = int(workspace_id)
            except (ValueError, TypeError):
                return jsonify({
                    "success": False,
                    "error": "invalid_workspace_id"
                }), 400
            
            allowed, current, limit = check_flow_limit(user, workspace_id)
            if not allowed:
                return jsonify({
                    "success": False,
                    "error": "flow_limit_exceeded",
                    "current": current,
                    "limit": limit,
                    "plan": user.plan,
                    "message": f"Interactive flow limit of {limit} reached. Please upgrade your plan."
                }), 403
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator
