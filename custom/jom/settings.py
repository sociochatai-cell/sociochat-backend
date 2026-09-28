"""
JOM per-workspace settings — the single place lifecycle/scheduler/qualification read
their tunables from. Every getter falls back to the code default in config.py, so a
workspace with no jom_settings row behaves exactly as before. Owner-editable via
/api/jom/settings. Fail-safe: any DB error returns the config default, never raises.
"""
import logging

from . import config as cfg

logger = logging.getLogger(__name__)

# Which step maps to which template — keys are stable, values are the DEFAULT template
# names. The owner can remap any of these to another approved template of theirs.
DEFAULT_TEMPLATES = {
    "opening": cfg.TPL_OPENING,
    "continue": cfg.TPL_CONTINUE,
    "ghosted_checkin": cfg.TPL_GHOSTED_CHECKIN,
    "ghosted_exit": cfg.TPL_GHOSTED_EXIT,
    "dormant_checkin": cfg.TPL_DORMANT_CHECKIN,
    "close": cfg.TPL_CLOSE,
}


def _row(workspace_id):
    try:
        from .models import JomSettings
        return JomSettings.query.filter_by(workspace_id=int(workspace_id)).first()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[jom] settings row load failed: %s", exc)
        return None


def get_settings(workspace_id):
    """Merged view (defaults + overrides) — used by the API and callers alike."""
    r = _row(workspace_id)
    return {
        "ghosted_days": (r.ghosted_days if r and r.ghosted_days else cfg.GHOSTED_DAYS),
        "dormant_days": (r.dormant_days if r and r.dormant_days else cfg.DORMANT_DAYS),
        "qualify_max_turns": (r.qualify_max_turns if r and r.qualify_max_turns else cfg.QUALIFY_MAX_TURNS),
        "templates": {**DEFAULT_TEMPLATES, **((r.templates if r and r.templates else {}) or {})},
    }


def cadence(workspace_id, status):
    s = get_settings(workspace_id)
    return s["dormant_days"] if status == cfg.STATUS_DORMANT else s["ghosted_days"]


def qualify_max_turns(workspace_id):
    return int(get_settings(workspace_id)["qualify_max_turns"])


def template_name(workspace_id, key):
    return get_settings(workspace_id)["templates"].get(key, DEFAULT_TEMPLATES.get(key))


def template_for(workspace_id, status, stage):
    """Which template + whether it uses the {{3}} tip, honouring the CURRENT cadence
    length (so if the owner adds/removes drip steps the last-step = close logic still
    lines up). Returns (template_name, uses_tip)."""
    s = get_settings(workspace_id)
    tpls = s["templates"]
    if stage is not None and stage < 0:
        return tpls["close"], False
    if status == cfg.STATUS_GHOSTED:
        last = len(s["ghosted_days"]) - 1
        return (tpls["ghosted_exit"], False) if int(stage or 0) >= last else (tpls["ghosted_checkin"], False)
    last = len(s["dormant_days"]) - 1
    return (tpls["close"], False) if int(stage or 0) >= last else (tpls["dormant_checkin"], True)


def save(workspace_id, ghosted_days=None, dormant_days=None, qualify_max_turns=None, templates=None):
    """Upsert the overrides. Only non-None args are written."""
    from models import db
    from .models import JomSettings
    r = JomSettings.query.filter_by(workspace_id=int(workspace_id)).first()
    if not r:
        r = JomSettings(workspace_id=int(workspace_id))
        db.session.add(r)
    if ghosted_days is not None:
        r.ghosted_days = [int(x) for x in ghosted_days]
    if dormant_days is not None:
        r.dormant_days = [int(x) for x in dormant_days]
    if qualify_max_turns is not None:
        r.qualify_max_turns = int(qualify_max_turns)
    if templates is not None:
        merged = dict(r.templates or {})
        merged.update({k: v for k, v in templates.items() if k in DEFAULT_TEMPLATES and v})
        r.templates = merged
    db.session.commit()
    return get_settings(workspace_id)
