"""
Cross-app workspace sync: SocioChat  <->  Sociovia  (Phase 2).

Both apps are siblings sharing the same `workspaces2` schema and live on the
same Postgres server (different database names). A user can exist in BOTH apps,
matched by email; the link is recorded in `user_cross_app_links`. When such a
linked user creates a workspace in one app, the same-named workspace is
created in the other. Per-workspace mapping lives in `workspace_cross_app_links`
(keeps the sync idempotent and loop-free).

SAFETY / DESIGN:
  - OFF by default. Enable with env SOCIOVIA_WORKSPACE_SYNC_ENABLED=1.
  - Needs env SOCIOVIA_DATABASE_URI (Sociovia's Postgres). If unset -> no-op.
  - Only LINKED users sync (row in user_cross_app_links). Everyone else: no-op.
  - Idempotent: a workspace already in workspace_cross_app_links is never
    re-created; before inserting we also match an existing (user,name) on the
    far side so re-runs/races don't create duplicates.
  - Failure-isolated: every public function swallows its own errors and logs;
    a sync failure must NEVER break the caller (e.g. workspace creation).
"""
import os
import logging
from functools import lru_cache

from sqlalchemy import create_engine, text
from models import db, Workspace

log = logging.getLogger("sociovia_sync")


def _enabled() -> bool:
    return os.getenv("SOCIOVIA_WORKSPACE_SYNC_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


@lru_cache(maxsize=1)
def _sociovia_engine():
    uri = os.getenv("SOCIOVIA_DATABASE_URI", "").strip()
    if not uri:
        log.warning("SOCIOVIA_DATABASE_URI not set; cross-app sync is a no-op")
        return None
    return create_engine(
        uri, pool_pre_ping=True,
        pool_size=int(os.getenv("SOCIOVIA_DB_POOL_SIZE", "2")),
        max_overflow=int(os.getenv("SOCIOVIA_DB_MAX_OVERFLOW", "3")),
        pool_recycle=int(os.getenv("DB_POOL_RECYCLE", "280")),
    )


def _linked_sovia_user(user_id: int):
    row = db.session.execute(
        text("select sociovia_user_id from user_cross_app_links where sociochat_user_id=:u"),
        {"u": user_id},
    ).first()
    return row[0] if row else None


# ---------------------------------------------------------------- outbound ----
def mirror_workspace_to_sociovia(user_id: int, workspace_id: int, workspace_name):
    """SocioChat workspace was just created -> mirror it into Sociovia.
    No-op unless enabled + the user is linked. Never raises."""
    if not _enabled():
        return
    eng = _sociovia_engine()
    if eng is None:
        return
    try:
        sovia_user = _linked_sovia_user(user_id)
        if not sovia_user:
            return  # user has no Sociovia account linked -> nothing to do
        already = db.session.execute(
            text("select 1 from workspace_cross_app_links where sociochat_ws_id=:w"),
            {"w": workspace_id},
        ).first()
        if already:
            return  # idempotent
        with eng.begin() as conn:
            # reuse an existing same-named workspace on the Sociovia side if present
            existing = conn.execute(
                text("select id from workspaces2 where user_id=:u and coalesce(business_name,'')=coalesce(:n,'') order by id limit 1"),
                {"u": sovia_user, "n": workspace_name},
            ).scalar()
            sovia_ws = existing or conn.execute(
                text("insert into workspaces2 (user_id, business_name, country, created_at, updated_at) "
                     "values (:u, :n, 'India', now(), now()) returning id"),
                {"u": sovia_user, "n": workspace_name},
            ).scalar()
        db.session.execute(
            text("insert into workspace_cross_app_links (sociochat_ws_id, sociovia_ws_id, sociochat_user_id, source, sync_status) "
                 "values (:sw, :vw, :u, 'live_outbound', 'in_sync') on conflict (sociochat_ws_id) do nothing"),
            {"sw": workspace_id, "vw": sovia_ws, "u": user_id},
        )
        db.session.commit()
        log.info("mirrored sociochat ws %s -> sociovia ws %s (user %s->%s)", workspace_id, sovia_ws, user_id, sovia_user)
    except Exception:
        db.session.rollback()
        log.exception("mirror_workspace_to_sociovia failed (sociochat ws %s)", workspace_id)


# ----------------------------------------------------------------- inbound ----
def reconcile_inbound(limit_per_user: int = 200) -> dict:
    """Pull Sociovia workspaces that linked users own but that aren't mirrored
    into SocioChat yet, and create the matching SocioChat workspaces.
    Idempotent. Returns a summary. Never raises."""
    summary = {"created": 0, "users": 0, "skipped_disabled": False}
    if not _enabled():
        summary["skipped_disabled"] = True
        return summary
    eng = _sociovia_engine()
    if eng is None:
        return summary
    try:
        links = db.session.execute(
            text("select sociochat_user_id, sociovia_user_id from user_cross_app_links")
        ).all()
        for sc_user, sv_user in links:
            mapped = {r[0] for r in db.session.execute(
                text("select sociovia_ws_id from workspace_cross_app_links where sociochat_user_id=:u"),
                {"u": sc_user}).all()}
            with eng.connect() as conn:
                rows = conn.execute(
                    text("select id, business_name from workspaces2 where user_id=:u order by id limit :lim"),
                    {"u": sv_user, "lim": limit_per_user},
                ).all()
            created_here = False
            for sv_ws_id, sv_name in rows:
                if sv_ws_id in mapped:
                    continue
                ws = Workspace(user_id=sc_user, business_name=sv_name)
                db.session.add(ws)
                db.session.flush()  # get ws.id
                db.session.execute(
                    text("insert into workspace_cross_app_links (sociochat_ws_id, sociovia_ws_id, sociochat_user_id, source, sync_status) "
                         "values (:sw, :vw, :u, 'live_inbound', 'in_sync') on conflict (sociovia_ws_id) do nothing"),
                    {"sw": ws.id, "vw": sv_ws_id, "u": sc_user},
                )
                summary["created"] += 1
                created_here = True
            if created_here:
                summary["users"] += 1
        db.session.commit()
        log.info("reconcile_inbound: created %s workspaces for %s users", summary["created"], summary["users"])
    except Exception:
        db.session.rollback()
        log.exception("reconcile_inbound failed")
    return summary


# ------------------------------------------------------------------ linking ---
def link_user_by_email(sociochat_user_id: int, email: str) -> bool:
    """If a Sociovia user exists with this (verified) email, record the cross-app
    link. Independent of the sync flag so links can accumulate before sync is
    turned on; only needs SOCIOVIA_DATABASE_URI. Never raises. Returns True iff a
    NEW link was created."""
    eng = _sociovia_engine()
    if eng is None or not email:
        return False
    try:
        if db.session.execute(
            text("select 1 from user_cross_app_links where sociochat_user_id=:u"),
            {"u": sociochat_user_id},
        ).first():
            return False  # already linked
        with eng.connect() as conn:
            row = conn.execute(
                text("select id from users where lower(email)=lower(:e) order by id limit 1"),
                {"e": email},
            ).first()
        if not row:
            return False  # no Sociovia account with this email
        db.session.execute(
            text("insert into user_cross_app_links (sociochat_user_id, sociovia_user_id, email, source, sync_status) "
                 "values (:u, :v, :e, 'signup', 'in_sync') on conflict (sociochat_user_id) do nothing"),
            {"u": sociochat_user_id, "v": row[0], "e": email},
        )
        db.session.commit()
        log.info("linked sociochat user %s -> sociovia user %s by email %s", sociochat_user_id, row[0], email)
        return True
    except Exception:
        db.session.rollback()
        log.exception("link_user_by_email failed for user %s", sociochat_user_id)
        return False


def mirror_user_workspaces(sociochat_user_id: int):
    """Mirror all of a user's current SocioChat workspaces into Sociovia (outbound
    backfill for a freshly-linked user). No-op unless enabled; failure-isolated.
    Idempotent via mirror_workspace_to_sociovia()."""
    if not _enabled():
        return
    try:
        rows = db.session.execute(
            text("select id, business_name from workspaces2 where user_id=:u order by id"),
            {"u": sociochat_user_id},
        ).all()
    except Exception:
        log.exception("mirror_user_workspaces lookup failed for user %s", sociochat_user_id)
        return
    for ws_id, ws_name in rows:
        mirror_workspace_to_sociovia(sociochat_user_id, ws_id, ws_name)


def mirror_workspace_delete_to_sociovia(workspace_id: int):
    """A SocioChat workspace was deleted -> delete its mirrored Sociovia workspace
    and drop the link (the other half of two-way sync). No-op unless enabled + a
    link exists. Never raises. If the Sociovia workspace still has dependent data
    and the DB blocks the delete, we log and keep the link (no force-delete)."""
    if not _enabled():
        return
    eng = _sociovia_engine()
    if eng is None:
        return
    try:
        row = db.session.execute(
            text("select sociovia_ws_id from workspace_cross_app_links where sociochat_ws_id=:w"),
            {"w": workspace_id},
        ).first()
        if not row:
            return  # not a mirrored workspace -> nothing to delete
        sovia_ws = row[0]
        with eng.begin() as conn:
            conn.execute(text("delete from workspaces2 where id=:i"), {"i": sovia_ws})
        db.session.execute(
            text("delete from workspace_cross_app_links where sociochat_ws_id=:w"),
            {"w": workspace_id},
        )
        db.session.commit()
        log.info("deleted mirrored sociovia ws %s (sociochat ws %s)", sovia_ws, workspace_id)
    except Exception:
        db.session.rollback()
        log.exception("mirror_workspace_delete_to_sociovia failed (sociochat ws %s)", workspace_id)


# ------------------------------------------- admin per-user toggle (Phase-2) ---
def is_user_linked(sociochat_user_id: int) -> bool:
    """True if this user has a cross-app link row (drives the admin toggle state)."""
    try:
        return db.session.execute(
            text("select 1 from user_cross_app_links where sociochat_user_id=:u"),
            {"u": sociochat_user_id},
        ).first() is not None
    except Exception:
        return False


def _force_mirror_user_workspaces(sociochat_user_id: int, sovia_user: int, eng) -> int:
    """Mirror all of a user's SocioChat workspaces into Sociovia, ignoring the global
    enable flag (admin explicitly triggered it). Idempotent. Returns count created."""
    created = 0
    rows = db.session.execute(
        text("select id, business_name from workspaces2 where user_id=:u order by id"),
        {"u": sociochat_user_id},
    ).all()
    for ws_id, ws_name in rows:
        if db.session.execute(
            text("select 1 from workspace_cross_app_links where sociochat_ws_id=:w"),
            {"w": ws_id},
        ).first():
            continue
        with eng.begin() as conn:
            existing = conn.execute(
                text("select id from workspaces2 where user_id=:u and coalesce(business_name,'')=coalesce(:n,'') order by id limit 1"),
                {"u": sovia_user, "n": ws_name}).scalar()
            sovia_ws = existing or conn.execute(
                text("insert into workspaces2 (user_id, business_name, country, created_at, updated_at) "
                     "values (:u,:n,'India',now(),now()) returning id"),
                {"u": sovia_user, "n": ws_name}).scalar()
        db.session.execute(
            text("insert into workspace_cross_app_links (sociochat_ws_id, sociovia_ws_id, sociochat_user_id, source, sync_status) "
                 "values (:sw,:vw,:u,'admin_toggle','in_sync') on conflict (sociochat_ws_id) do nothing"),
            {"sw": ws_id, "vw": sovia_ws, "u": sociochat_user_id})
        created += 1
    db.session.commit()
    return created


def trigger_sociovia_link(sociochat_user_id: int, email: str, enable: bool) -> dict:
    """Admin per-user toggle — NOT gated by the global sync flag (an admin explicitly
    requested it); only needs SOCIOVIA_DATABASE_URI. Never raises.
      enable=True  -> link this user to their Sociovia account (matched by email) and
                      mirror their current workspaces into Sociovia.
      enable=False -> remove the link (leaves already-mirrored workspaces alone)."""
    eng = _sociovia_engine()
    if eng is None:
        return {"ok": False, "error": "sociovia_db_not_configured"}
    try:
        if not enable:
            db.session.execute(
                text("delete from user_cross_app_links where sociochat_user_id=:u"),
                {"u": sociochat_user_id})
            db.session.commit()
            return {"ok": True, "linked": False}
        with eng.connect() as conn:
            row = conn.execute(
                text("select id from users where lower(email)=lower(:e) order by id limit 1"),
                {"e": email or ""}).first()
        if not row:
            return {"ok": False, "error": "no_sociovia_account_for_email", "email": email}
        sovia_user = row[0]
        db.session.execute(
            text("insert into user_cross_app_links (sociochat_user_id, sociovia_user_id, email, source, sync_status) "
                 "values (:u,:v,:e,'admin_toggle','in_sync') "
                 "on conflict (sociochat_user_id) do update set sociovia_user_id=excluded.sociovia_user_id, email=excluded.email"),
            {"u": sociochat_user_id, "v": sovia_user, "e": email})
        db.session.commit()
        mirrored = _force_mirror_user_workspaces(sociochat_user_id, sovia_user, eng)
        return {"ok": True, "linked": True, "sociovia_user_id": sovia_user, "workspaces_mirrored": mirrored}
    except Exception:
        db.session.rollback()
        log.exception("trigger_sociovia_link failed for user %s", sociochat_user_id)
        return {"ok": False, "error": "exception"}
