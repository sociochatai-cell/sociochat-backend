"""
JOM gate — the single switch that scopes this entire custom vertical to one (or a
few) workspace_id(s). Everything in custom/jom/ MUST be called behind is_jom_workspace().

Env: JOM_WORKSPACE_IDS="211"  (comma-separated allowlist; default 211 = Journey Of Miles).
Turn the vertical OFF entirely by setting JOM_WORKSPACE_IDS="" — every workspace then
falls back to the identical existing SocioChat behaviour.
"""
import os

_DEFAULT = "211"  # Journey Of Miles Pvt Ltd (SocioChat workspaces2.id)


def _allowed_ids() -> set:
    raw = os.getenv("JOM_WORKSPACE_IDS", _DEFAULT)
    ids = set()
    for part in str(raw).replace(" ", "").split(","):
        if part.isdigit():
            ids.add(int(part))
    return ids


def is_jom_workspace(workspace_id) -> bool:
    """True only for the allowlisted workspace(s). Fail-safe: any error → False."""
    try:
        return int(workspace_id) in _allowed_ids()
    except (TypeError, ValueError):
        return False


def allowed_workspace_ids() -> list:
    return sorted(_allowed_ids())
