"""
Journey Of Miles (JOM) custom vertical — SocioChat.
Additive, isolated, and gated to workspace_id 211 (env JOM_WORKSPACE_IDS).
Nothing here affects any other workspace. Wired in app.py:
  - register_blueprint(jom_bp)
  - ensure_jom_schema(db.engine)
"""
from .gate import is_jom_workspace, allowed_workspace_ids  # noqa: F401
from .routes import jom_bp  # noqa: F401
from .schema_patch import ensure_jom_schema  # noqa: F401

__all__ = ["jom_bp", "ensure_jom_schema", "is_jom_workspace", "allowed_workspace_ids"]
