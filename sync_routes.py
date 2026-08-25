"""
SocioChat ↔ Sociovia sync routes + shared helpers.

Endpoints:
  GET  /api/sync/status   → is this user synced? (drives the Return-to-Sociovia button)
  POST /api/sync/enable   → pull the user's Sociovia workspaces into SocioChat.

Shared helpers (also used by the admin per-user toggle in admin_routes.py):
  enable_sync_for_user(user) → run the inbound pull + mark synced. Returns summary.
  disable_sync_for_user(email) → set sync_enabled=false (keeps mappings/data).
  is_synced(email) → bool, drives the admin switch state.

Rules:
  * Sociovia is READ-ONLY here (we only SELECT from it).
  * All sync state lives in the isolated tables sync_user_map / sync_workspace_map.
  * Creating a missing workspace writes a NORMAL row into SocioChat.workspaces2
    (same shape the app already uses) — no schema change anywhere.
  * Reverse direction (SocioChat→Sociovia INSERT) is handled by reverse_sync.py,
    which keys off sync_user_map.sync_enabled — so enabling here turns on BOTH
    directions for that user.
"""
from __future__ import annotations

import os
import logging
from flask import Blueprint, jsonify

from sqlalchemy import create_engine, text
from models import db, Workspace
from tenant.context import get_current_user

logger = logging.getLogger(__name__)

sync_bp = Blueprint("sync", __name__, url_prefix="/api/sync")

_sociovia_engine = None


def _shared_id_master_enabled() -> bool:
    """Same-id master/slave flag (DEFAULT OFF). When ON, a workspace copied from
    Sociovia keeps the SAME id here (instead of a fresh 1,000,000+ autoincrement),
    provided that id is free in SocioChat. Collision-guarded + forward-only, so it
    never re-keys an existing workspace."""
    return os.environ.get("SHARED_ID_MASTER", "").strip().lower() in ("1", "true", "yes", "on")

# Columns we copy when replicating a Sociovia workspace into SocioChat.
_WS_COPY_COLS = [
    "business_name", "business_type", "industry", "description",
    "city", "country", "website", "address_line", "audience_description",
    "b2b_b2c", "competitor_direct_1", "competitor_direct_2",
    "competitor_indirect_1", "competitor_indirect_2", "creatives_path",
    "district", "logo_path", "pin_code", "registered_address", "remarks",
    "social_links", "usp",
]


def _sociovia():
    global _sociovia_engine
    if _sociovia_engine is None:
        url = os.environ.get("SOCIOVIA_DB_URL")
        if not url:
            raise RuntimeError("SOCIOVIA_DB_URL not set")
        _sociovia_engine = create_engine(url, pool_pre_ping=True, pool_recycle=300)
    return _sociovia_engine


# --------------------------------------------------------------- shared core ---
def is_synced(email: str) -> bool:
    """True iff this email is synced (sync_user_map.sync_enabled=true)."""
    if not email:
        return False
    row = db.session.execute(
        text("SELECT sync_enabled FROM sync_user_map WHERE LOWER(email)=LOWER(:e)"),
        {"e": email},
    ).fetchone()
    return bool(row and row[0])


def sociovia_user_id_for(email: str):
    """Return the Sociovia user id for this email, or None. READ ONLY."""
    if not email:
        return None
    with _sociovia().connect() as c:
        r = c.execute(text("SELECT id FROM users WHERE LOWER(email)=LOWER(:e) LIMIT 1"),
                      {"e": email}).fetchone()
    return r[0] if r else None


def enable_sync_for_user(user) -> dict:
    """Turn sync ON for `user`: pull their Sociovia workspaces into SocioChat and
    mark them synced (which also enables the reverse direction via reverse_sync).
    Sociovia is READ-ONLY here. Returns a summary dict. Raises RuntimeError with a
    machine code on expected failures (not_in_sociovia / sociovia_unreachable)."""
    email = (user.email or "").strip().lower()
    if not email:
        raise RuntimeError("no_email")

    # tenant-1 (T0000) only: whitelabel users never sync to Sociovia
    trow = db.session.execute(
        text("SELECT tenant_id FROM users WHERE id=:u"), {"u": user.id}
    ).first()
    if not trow or trow[0] != 1:
        raise RuntimeError("whitelabel_not_syncable")

    # 1. Find the Sociovia user by email (READ ONLY) + read their workspaces
    try:
        with _sociovia().connect() as c:
            sv_user = c.execute(
                text("SELECT id FROM users WHERE LOWER(email)=LOWER(:e) LIMIT 1"),
                {"e": email},
            ).fetchone()
            if not sv_user:
                raise RuntimeError("not_in_sociovia")
            sv_user_id = sv_user[0]

            col_list = ", ".join(_WS_COPY_COLS)
            sv_ws = c.execute(
                text(f"SELECT id, {col_list} FROM workspaces2 WHERE user_id=:uid ORDER BY id"),
                {"uid": sv_user_id},
            ).fetchall()
    except RuntimeError:
        raise
    except Exception as e:
        logger.exception("enable_sync_for_user: sociovia read failed")
        raise RuntimeError("sociovia_unreachable") from e

    created = linked = already = 0

    # 2. Diff + reconcile each Sociovia workspace
    for row in sv_ws:
        sv_ws_id = row[0]
        ws_vals = dict(zip(_WS_COPY_COLS, row[1:]))
        bname = ws_vals.get("business_name") or ""

        m = db.session.execute(
            text("SELECT sociochat_workspace_id FROM sync_workspace_map WHERE sociovia_workspace_id=:s"),
            {"s": sv_ws_id},
        ).fetchone()
        if m and m[0]:
            already += 1
            continue

        existing = db.session.execute(
            text("""SELECT w.id FROM workspaces2 w
                    WHERE w.user_id=:uid AND COALESCE(w.business_name,'')=:bn LIMIT 1"""),
            {"uid": user.id, "bn": bname},
        ).fetchone()

        if existing:
            socc_ws_id = existing[0]
            linked += 1
        else:
            new_ws = Workspace(user_id=user.id, business_name=bname)
            # Same-id master/slave: adopt the Sociovia workspace id when enabled and
            # that id is FREE here. If it's already taken (by any workspace), fall
            # back to autoincrement so we never clobber an existing workspace.
            if _shared_id_master_enabled():
                clash = db.session.execute(
                    text("SELECT id FROM workspaces2 WHERE id=:i"), {"i": sv_ws_id}
                ).fetchone()
                if clash is None:
                    new_ws.id = sv_ws_id
                else:
                    logger.warning(
                        "[sync] shared-id: Sociovia ws %s already used in SocioChat "
                        "— falling back to autoincrement", sv_ws_id,
                    )
            for col in _WS_COPY_COLS:
                if col == "business_name":
                    continue
                if hasattr(new_ws, col) and ws_vals.get(col) is not None:
                    setattr(new_ws, col, ws_vals.get(col))
            db.session.add(new_ws)
            db.session.flush()
            socc_ws_id = new_ws.id
            created += 1

        db.session.execute(
            text("""INSERT INTO sync_workspace_map
                     (owner_email, sociovia_workspace_id, sociochat_workspace_id, workspace_name, origin, synced_at)
                    VALUES (:e, :sv, :sc, :nm, 'sociovia', NOW())
                    ON CONFLICT (sociovia_workspace_id) DO UPDATE
                      SET sociochat_workspace_id=EXCLUDED.sociochat_workspace_id, updated_at=NOW()"""),
            {"e": email, "sv": sv_ws_id, "sc": socc_ws_id, "nm": bname},
        )

    # 3. Mark the user synced (this ALSO enables reverse sync for them)
    db.session.execute(
        text("""INSERT INTO sync_user_map (email, sociovia_user_id, sociochat_user_id, sync_enabled, synced_at)
                VALUES (:e, :sv, :sc, true, NOW())
                ON CONFLICT (email) DO UPDATE
                  SET sync_enabled=true, sociovia_user_id=EXCLUDED.sociovia_user_id,
                      sociochat_user_id=EXCLUDED.sociochat_user_id, synced_at=NOW(), updated_at=NOW()"""),
        {"e": email, "sv": sv_user_id, "sc": user.id},
    )
    db.session.commit()

    return {
        "sociovia_workspaces": len(sv_ws),
        "created": created,
        "linked": linked,
        "already_synced": already,
    }


def disable_sync_for_user(email: str) -> None:
    """Turn sync OFF for this email. Keeps existing mappings + already-copied data;
    just stops future propagation (both directions key off sync_enabled)."""
    db.session.execute(
        text("UPDATE sync_user_map SET sync_enabled=false, updated_at=NOW() WHERE LOWER(email)=LOWER(:e)"),
        {"e": (email or "").lower()},
    )
    db.session.commit()


# ---------------------------------------------------------------- endpoints ---
@sync_bp.route("/status", methods=["GET"])
def sync_status():
    """Return this user's sync state (used to show/hide 'Return to main dashboard')."""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "error": "authentication_required"}), 401

    row = db.session.execute(
        text("SELECT sync_enabled, synced_at, sociovia_user_id FROM sync_user_map WHERE LOWER(email)=LOWER(:e)"),
        {"e": user.email},
    ).fetchone()

    eligible = False
    try:
        eligible = sociovia_user_id_for(user.email) is not None
    except Exception as e:
        logger.warning("sync_status sociovia probe failed: %s", e)

    return jsonify({
        "success": True,
        "synced": bool(row and row[0]),
        "synced_at": row[1].isoformat() if (row and row[1]) else None,
        "eligible": eligible,
        "sociovia_user_id": row[2] if row else None,
    })


@sync_bp.route("/enable", methods=["POST"])
def sync_enable():
    """Toggle sync ON for the CURRENT user: pull their Sociovia workspaces in."""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "error": "authentication_required"}), 401
    try:
        summary = enable_sync_for_user(user)
    except RuntimeError as e:
        code = str(e)
        http = 404 if code == "not_in_sociovia" else (502 if code == "sociovia_unreachable" else 400)
        msg = {"not_in_sociovia": "This account does not exist on Sociovia."}.get(code)
        return jsonify({"success": False, "error": code, "message": msg}), http

    return jsonify({
        "success": True, "synced": True, "summary": summary,
        "message": f"Synced with Sociovia: {summary['created']} created, "
                   f"{summary['linked']} linked, {summary['already_synced']} already in sync.",
    })
