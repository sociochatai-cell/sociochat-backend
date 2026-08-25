"""
Phase D — SAFE reverse sync: SocioChat  ->  Sociovia.

When a *synced* user creates a workspace in SocioChat, mirror it into Sociovia
as a single plain workspaces2 row. This is the ONLY direction that writes to
Sociovia, so every safety rail matters:

  SAFETY DESIGN
  -------------
  * INSERT-ONLY. We never UPDATE or DELETE anything in Sociovia. Deleting a
    SocioChat workspace does NOT remove the Sociovia one (handled elsewhere:
    no reverse-delete is wired).
  * Only NON-sensitive columns: user_id, business_name, country, timestamps.
    No schema change, no touching existing Sociovia rows/tables.
  * Only SYNCED users (row in sync_user_map with sync_enabled=true). Everyone
    else -> no-op.
  * Idempotent + loop-free via sync_workspace_map (keyed on sociochat_workspace_id).
    A workspace already mapped is never re-created. Before inserting we also
    reuse an existing same-(user,name) Sociovia workspace so re-runs/races never
    duplicate.
  * Separately gated by env REVERSE_SYNC_ENABLED=1 (independent of the old
    SOCIOVIA_WORKSPACE_SYNC_ENABLED flag). Off => hard no-op.
  * Failure-isolated: every path swallows its own errors; a sync failure can
    NEVER break workspace creation in SocioChat.
"""
from __future__ import annotations

import os
import logging
from functools import lru_cache

from sqlalchemy import create_engine, text
from models import db

log = logging.getLogger("reverse_sync")


def _enabled() -> bool:
    return os.getenv("REVERSE_SYNC_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


@lru_cache(maxsize=1)
def _sociovia_engine():
    # Reuse the same read URL the inbound sync already uses.
    uri = os.getenv("SOCIOVIA_DB_URL", "").strip()
    if not uri:
        log.warning("SOCIOVIA_DB_URL not set; reverse sync is a no-op")
        return None
    return create_engine(uri, pool_pre_ping=True, pool_size=2, max_overflow=3, pool_recycle=280)


def _reverse_provision_enabled() -> bool:
    """Gate for creating the Sociovia MASTER user from a SocioChat-first signup
    (DEFAULT OFF). Independent of REVERSE_SYNC_ENABLED (workspace mirror)."""
    return os.getenv("REVERSE_PROVISION", "").strip().lower() in ("1", "true", "yes", "on")


def provision_sociovia_user(email: str, name, password_hash: str):
    """Create (or find) the Sociovia MASTER user for a T0000 SocioChat-first signup
    and return its id — so the SocioChat user can adopt the SAME id.

    INSERT-only on Sociovia. Copies the SAME password_hash (both apps use Werkzeug
    hashing) so the one password works on both sides. New master starts as
    status='pending_payment' so a later Sociovia login routes to /choose-plan.
    Gated by REVERSE_PROVISION. Never raises — returns None on any failure/disabled.
    """
    if not _reverse_provision_enabled():
        return None
    eng = _sociovia_engine()
    if eng is None:
        return None
    email = (email or "").strip().lower()
    if not email:
        return None
    try:
        with eng.begin() as conn:
            existing = conn.execute(
                text("SELECT id FROM users WHERE LOWER(email)=LOWER(:e) LIMIT 1"), {"e": email}
            ).scalar()
            if existing:
                return existing
            new_id = conn.execute(
                text(
                    "INSERT INTO users (email, name, password_hash, email_verified, "
                    "email_verified_at, status, plan, created_at, updated_at) "
                    "VALUES (:e, :n, :ph, true, now(), 'pending_payment', 'ai_free', now(), now()) "
                    "RETURNING id"
                ),
                {"e": email, "n": (name or email.split("@")[0]), "ph": password_hash},
            ).scalar()
            log.info("provisioned Sociovia master user id=%s for %s", new_id, email)
            return new_id
    except Exception:
        log.exception("provision_sociovia_user failed for %s", email)
        return None


def provision_sociovia_workspace(sociovia_user_id: int, business_name):
    """Create (or find) a Sociovia workspace for the master user and return its id,
    so a SocioChat-first signup's workspace can adopt the SAME id. INSERT-only,
    gated by REVERSE_PROVISION. Never raises — returns None on failure/disabled."""
    if not _reverse_provision_enabled():
        return None
    eng = _sociovia_engine()
    if eng is None or not sociovia_user_id:
        return None
    try:
        name = (business_name or "").strip() or None
        with eng.begin() as conn:
            existing = conn.execute(
                text("SELECT id FROM workspaces2 WHERE user_id=:u "
                     "AND COALESCE(business_name,'')=COALESCE(:n,'') ORDER BY id LIMIT 1"),
                {"u": sociovia_user_id, "n": name},
            ).scalar()
            if existing:
                return existing
            wid = conn.execute(
                text("INSERT INTO workspaces2 (user_id, business_name, country, created_at, updated_at) "
                     "VALUES (:u, :n, 'India', now(), now()) RETURNING id"),
                {"u": sociovia_user_id, "n": name},
            ).scalar()
            log.info("provisioned Sociovia master workspace id=%s (user %s)", wid, sociovia_user_id)
            return wid
    except Exception:
        log.exception("provision_sociovia_workspace failed (user %s)", sociovia_user_id)
        return None


def _synced_sociovia_user(email: str):
    """Return the Sociovia user_id iff this email is synced (sync_enabled=true)."""
    row = db.session.execute(
        text("SELECT sociovia_user_id FROM sync_user_map "
             "WHERE LOWER(email)=LOWER(:e) AND sync_enabled=true AND sociovia_user_id IS NOT NULL"),
        {"e": email or ""},
    ).first()
    return row[0] if row else None


def _is_main_tenant(user_id: int) -> bool:
    """True only for T0000 (tenant_id=1). Whitelabel tenants never sync to Sociovia."""
    try:
        row = db.session.execute(
            text("SELECT tenant_id FROM users WHERE id=:u"), {"u": user_id}
        ).first()
        return bool(row and row[0] == 1)
    except Exception:
        return False


def mirror_workspace_to_sociovia(sociochat_user_id: int, sociochat_ws_id: int,
                                 workspace_name, owner_email: str) -> None:
    """SocioChat workspace just created -> mirror into Sociovia. Never raises."""
    if not _enabled():
        return
    eng = _sociovia_engine()
    if eng is None:
        return
    try:
        sv_user = _synced_sociovia_user(owner_email)
        if not sv_user:
            return  # user not synced -> nothing to do

        if not _is_main_tenant(sociochat_user_id):
            return  # whitelabel tenant (not T0000) -> NEVER mirror to Sociovia

        # Already mapped? (idempotent + loop-free)
        already = db.session.execute(
            text("SELECT 1 FROM sync_workspace_map WHERE sociochat_workspace_id=:w"),
            {"w": sociochat_ws_id},
        ).first()
        if already:
            return

        name = (workspace_name or "").strip() or None

        with eng.begin() as conn:
            # Reuse an existing same-(user,name) Sociovia workspace if present, else INSERT.
            sv_ws = conn.execute(
                text("SELECT id FROM workspaces2 WHERE user_id=:u "
                     "AND COALESCE(business_name,'')=COALESCE(:n,'') ORDER BY id LIMIT 1"),
                {"u": sv_user, "n": name},
            ).scalar()
            if not sv_ws:
                sv_ws = conn.execute(
                    text("INSERT INTO workspaces2 (user_id, business_name, country, created_at, updated_at) "
                         "VALUES (:u, :n, 'India', now(), now()) RETURNING id"),
                    {"u": sv_user, "n": name},
                ).scalar()

        # Record mapping in the isolated table (origin=sociochat).
        db.session.execute(
            text("INSERT INTO sync_workspace_map "
                 "(owner_email, sociovia_workspace_id, sociochat_workspace_id, workspace_name, origin, synced_at) "
                 "VALUES (:e, :sv, :sc, :nm, 'sociochat', NOW()) "
                 "ON CONFLICT (sociochat_workspace_id) DO NOTHING"),
            {"e": (owner_email or "").lower(), "sv": sv_ws, "sc": sociochat_ws_id, "nm": name},
        )
        db.session.commit()
        log.info("reverse-mirrored sociochat ws %s -> sociovia ws %s (user %s->%s)",
                 sociochat_ws_id, sv_ws, sociochat_user_id, sv_user)
    except Exception:
        db.session.rollback()
        log.exception("reverse mirror_workspace_to_sociovia failed (sociochat ws %s)", sociochat_ws_id)
