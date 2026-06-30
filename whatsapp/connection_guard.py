"""
WhatsApp Connection Guard
=========================

Centralized cross-workspace protection for WABA connections.

PROBLEM SOLVED:
    A phone_number_id can only belong to ONE active workspace at a time.
    Without this guard, multiple connection paths silently "steal" an
    active account from workspace A when workspace B connects the same
    WABA number — breaking all automations, messages, and webhooks for
    workspace A.

USAGE:
    from whatsapp.connection_guard import check_phone_available

    conflict = check_phone_available(phone_number_id, target_workspace_id)
    if conflict:
        return error_response(conflict)    # contains human-readable message
    # safe to proceed with connection
"""

import logging
from typing import Optional, Dict, Any

from shared_models import db
from .models import WhatsAppAccount

logger = logging.getLogger(__name__)


def check_phone_available(
    phone_number_id: str,
    target_workspace_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Check whether a phone_number_id is available for the target workspace.

    Returns:
        None             – phone is free or already belongs to this workspace
        Dict with error  – blocked because it's active in another workspace

    The returned dict structure:
        {
            "blocked": True,
            "error": "This WhatsApp number is already connected ...",
            "error_code": "ALREADY_CONNECTED_OTHER",
            "owner_workspace_id": "<current owner>",
        }
    """
    if not phone_number_id:
        return None  # nothing to guard

    existing = WhatsAppAccount.query.filter_by(
        phone_number_id=phone_number_id
    ).first()

    if not existing:
        return None  # no record → free

    # Normalise workspace comparison to string
    existing_ws = str(existing.workspace_id) if existing.workspace_id else None
    target_ws = str(target_workspace_id) if target_workspace_id else None

    if existing_ws == target_ws:
        return None  # same workspace → allowed (will be an update)

    if not existing.is_active:
        return None  # inactive in another workspace → transfer is OK

    # BLOCKED – active in a different workspace
    logger.warning(
        f"WABA guard: phone_number_id={phone_number_id} is active in "
        f"workspace {existing_ws}, blocking connection from workspace {target_ws}"
    )
    return {
        "blocked": True,
        "error": (
            "This WhatsApp number is already actively connected to another "
            "workspace. Please disconnect it from the other workspace first, "
            "or use a different number."
        ),
        "error_code": "ALREADY_CONNECTED_OTHER",
        "owner_workspace_id": existing_ws,
    }


def deactivate_account_for_workspace(workspace_id: str) -> Optional[WhatsAppAccount]:
    """
    Deactivate (soft-disconnect) a WhatsApp account from a workspace.

    This is the safe way to release a phone_number_id so it can be
    connected to a different workspace.

    Returns the deactivated account or None.
    """
    account = WhatsAppAccount.query.filter_by(
        workspace_id=str(workspace_id),
        is_active=True,
    ).first()

    if not account:
        return None

    account.is_active = False
    db.session.commit()
    logger.info(
        f"Deactivated WhatsApp account {account.id} "
        f"(phone={account.phone_number_id}) in workspace {workspace_id}"
    )
    return account


def transfer_account_to_workspace(
    phone_number_id: str,
    new_workspace_id: str,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Admin-level transfer: move a phone_number_id to a new workspace.

    If force=False (default), refuses when the account is active elsewhere.
    If force=True, deactivates the old workspace first then transfers.

    Returns:
        {"success": True/False, "message": str, ...}
    """
    existing = WhatsAppAccount.query.filter_by(
        phone_number_id=phone_number_id
    ).first()

    if not existing:
        return {"success": False, "message": "No account found for this phone_number_id"}

    old_ws = str(existing.workspace_id) if existing.workspace_id else None
    new_ws = str(new_workspace_id)

    if old_ws == new_ws:
        if not existing.is_active:
            existing.is_active = True
            db.session.commit()
            return {"success": True, "message": "Account reactivated in same workspace"}
        return {"success": True, "message": "Account already belongs to this workspace"}

    if existing.is_active and not force:
        return {
            "success": False,
            "message": (
                f"Account is active in workspace {old_ws}. "
                "Disconnect it first or use force=True."
            ),
            "error_code": "ALREADY_CONNECTED_OTHER",
        }

    # Perform transfer
    existing.workspace_id = new_ws
    existing.is_active = True
    db.session.commit()
    logger.info(
        f"Transferred phone_number_id={phone_number_id} "
        f"from workspace {old_ws} to {new_ws}"
    )
    return {
        "success": True,
        "message": f"Account transferred from workspace {old_ws} to {new_ws}",
        "old_workspace_id": old_ws,
        "new_workspace_id": new_ws,
    }
