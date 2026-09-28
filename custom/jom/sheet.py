"""
Google Sheet lead intake (JOM vertical).

The client keeps pre-collected leads (from their ad campaigns / manual outreach) in
a Google Sheet. The owner pastes the Sheet's share link ("anyone with the link can
view"); we read it as CSV via the public export endpoint — no OAuth, no API key —
and for every NEW phone number we:
  1) create a jom_lifecycle_state row (status = new) with the memory-card fields
     (name / destination / budget / travel_dates) taken from the sheet, and
  2) fire the OPENING WhatsApp template so the AI conversation begins.

Idempotent on (workspace_id, customer_phone): a row already present is skipped, so
re-syncing the same sheet never double-messages anyone. Fail-safe: any error is
recorded on jom_sheet_config.last_error and surfaced as a red health badge; it never
raises into the scheduler.

Gated: only runs for JOM workspaces (call behind allowed_workspace_ids()).
"""
import csv
import io
import logging
import re
from datetime import datetime, timezone

from models import db
from . import config as cfg

logger = logging.getLogger(__name__)

_TIMEOUT = 15  # seconds for the sheet fetch

# Sheet header -> our field. Lower-cased, punctuation-insensitive matching (see _norm).
_FIELD_ALIASES = {
    "customer_phone": ["phone", "phone number", "mobile", "mobile number", "whatsapp",
                       "whatsapp number", "contact", "contact number", "number", "phone no"],
    "customer_name": ["name", "full name", "customer name", "lead name", "traveller",
                      "traveler", "client name"],
    "destination": ["destination", "place", "location", "trip", "package", "interested in"],
    "budget": ["budget", "price", "amount", "spend"],
    "travel_dates": ["travel dates", "dates", "travel date", "when", "month", "travel month"],
    "party_size": ["party size", "pax", "travellers", "travelers", "people", "persons", "group size"],
    "email": ["email", "email id", "e-mail", "mail"],
}


def _now():
    return datetime.now(timezone.utc)


def _norm(s):
    """Lower-case, strip non-alphanumerics for tolerant header matching."""
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


# --------------------------------------------------------------------------- #
# URL parsing / CSV fetch
# --------------------------------------------------------------------------- #
def csv_export_url(sheet_url):
    """Turn a Google Sheet share/edit URL into its CSV export URL.
    Handles .../d/<ID>/edit#gid=<GID> and ?gid=<GID>. Returns None if not a Google
    Sheets URL. If it's ALREADY an export/csv URL we return it unchanged."""
    if not sheet_url:
        return None
    url = sheet_url.strip()
    if "format=csv" in url or "/export" in url or "output=csv" in url:
        return url
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        return None
    sheet_id = m.group(1)
    gid = None
    g = re.search(r"[#&?]gid=([0-9]+)", url)
    if g:
        gid = g.group(1)
    base = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    return f"{base}&gid={gid}" if gid else base


def _fetch_csv(sheet_url):
    """Fetch the sheet as CSV text. Raises on network / access errors."""
    export = csv_export_url(sheet_url)
    if not export:
        raise ValueError("Not a valid Google Sheets link.")
    import requests  # local import: keeps module import cheap
    resp = requests.get(export, timeout=_TIMEOUT, allow_redirects=True)
    if resp.status_code == 200 and "text/html" in (resp.headers.get("content-type") or ""):
        # Google served a sign-in / permission page instead of the CSV.
        raise PermissionError("Sheet is not publicly readable — set sharing to "
                              "'Anyone with the link can view'.")
    resp.raise_for_status()
    return resp.text


def _build_header_index(fieldnames, header_map):
    """Map our field -> the sheet column name actually present.
    header_map (owner override) wins; otherwise auto-detect via aliases."""
    present = {_norm(h): h for h in (fieldnames or [])}
    resolved = {}
    # 1) explicit owner mapping {sheet_header: our_field}
    for sheet_header, our_field in (header_map or {}).items():
        key = _norm(sheet_header)
        if key in present and our_field:
            resolved[our_field] = present[key]
    # 2) auto-detect the rest
    for field, aliases in _FIELD_ALIASES.items():
        if field in resolved:
            continue
        for alias in aliases:
            if _norm(alias) in present:
                resolved[field] = present[_norm(alias)]
                break
    return resolved


def _normalize_phone(raw):
    """Best-effort E.164-ish digits. Assumes India (91) for 10-digit local numbers,
    matching the client's market. Returns None if it can't make a plausible number."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return None
    if len(digits) == 10:            # bare local mobile
        digits = "91" + digits
    elif len(digits) == 11 and digits.startswith("0"):
        digits = "91" + digits[1:]
    if len(digits) < 11 or len(digits) > 15:
        return None
    return digits


# --------------------------------------------------------------------------- #
# Config accessors
# --------------------------------------------------------------------------- #
def get_config(workspace_id):
    from .models import JomSheetConfig
    return JomSheetConfig.query.filter_by(workspace_id=int(workspace_id)).first()


def config_json(workspace_id):
    r = get_config(workspace_id)
    if not r:
        return {"enabled": False, "sheet_url": None, "header_map": {},
                "last_synced_at": None, "last_status": None, "last_error": None,
                "last_row_count": None, "last_imported_count": None, "total_imported": 0}
    return {
        "enabled": bool(r.enabled),
        "sheet_url": r.sheet_url,
        "header_map": r.header_map or {},
        "last_synced_at": r.last_synced_at.isoformat() if r.last_synced_at else None,
        "last_status": r.last_status,
        "last_error": r.last_error,
        "last_row_count": r.last_row_count,
        "last_imported_count": r.last_imported_count,
        "total_imported": r.total_imported or 0,
    }


def save_config(workspace_id, sheet_url=None, enabled=None, header_map=None):
    from .models import JomSheetConfig
    r = JomSheetConfig.query.filter_by(workspace_id=int(workspace_id)).first()
    if not r:
        r = JomSheetConfig(workspace_id=int(workspace_id))
        db.session.add(r)
    if sheet_url is not None:
        r.sheet_url = sheet_url.strip() or None
    if enabled is not None:
        r.enabled = bool(enabled)
    if header_map is not None:
        r.header_map = header_map or None
    r.updated_at = _now()
    db.session.commit()
    return config_json(workspace_id)


# --------------------------------------------------------------------------- #
# The sync itself
# --------------------------------------------------------------------------- #
def _opening_send_fn(workspace_id):
    """Build a send_fn(phone, name, destination) that fires the OPENING template.
    Reuses the scheduler's workspace-bound WhatsAppService. Returns None if the
    account/service can't be built (sync still imports the leads, just doesn't message)."""
    try:
        from . import scheduler, settings
        svc = scheduler._service_for(workspace_id)
        if svc is None:
            return None
        tpl = settings.template_name(workspace_id, "opening")

        def _send(phone, name, destination):
            components = scheduler._body_components({
                "1": name or "there",
                "2": destination or "your trip",
            })
            svc.send_template(to=phone, template_name=tpl, components=components)
        return _send
    except Exception as exc:  # noqa: BLE001
        logger.warning("[jom] opening sender unavailable for ws %s: %s", workspace_id, exc)
        return None


def sync_workspace(workspace_id, send_opening=True):
    """Read the configured sheet and import NEW leads. Returns a result dict; also
    records health on jom_sheet_config. Idempotent per phone. Never raises."""
    from .models import JomLifecycleState, JomBehaviorScore, JomSheetConfig
    r = get_config(workspace_id)
    if not r or not r.enabled or not r.sheet_url:
        return {"success": False, "skipped": True, "reason": "not_configured"}

    try:
        text = _fetch_csv(r.sheet_url)
    except Exception as exc:  # noqa: BLE001
        r.last_synced_at = _now()
        r.last_status = "error"
        r.last_error = str(exc)[:500]
        db.session.commit()
        logger.warning("[jom] sheet sync FAILED ws %s: %s", workspace_id, exc)
        return {"success": False, "error": str(exc)}

    reader = csv.DictReader(io.StringIO(text))
    idx = _build_header_index(reader.fieldnames, r.header_map)
    if "customer_phone" not in idx:
        r.last_synced_at = _now()
        r.last_status = "error"
        r.last_error = ("Could not find a phone-number column. Rename it to 'Phone' "
                        "or map it in settings.")
        db.session.commit()
        return {"success": False, "error": r.last_error, "headers": reader.fieldnames}

    send_fn = _opening_send_fn(workspace_id) if send_opening else None
    seen = imported = messaged = 0
    for raw in reader:
        seen += 1
        phone = _normalize_phone(raw.get(idx["customer_phone"]))
        if not phone:
            continue
        exists = JomLifecycleState.query.filter_by(
            workspace_id=int(workspace_id), customer_phone=phone).first()
        if exists:
            continue  # already in the pipeline — never re-import / re-message

        def _val(field):
            col = idx.get(field)
            v = (raw.get(col) or "").strip() if col else ""
            return v or None

        row = JomLifecycleState(
            workspace_id=int(workspace_id), customer_phone=phone,
            status=cfg.STATUS_NEW,
            customer_name=_val("customer_name"),
            destination=_val("destination"),
            budget=_val("budget"),
            travel_dates=_val("travel_dates"),
            party_size=_val("party_size"),
            email=_val("email"),
            status_since=_now(),
            last_summary="Imported from Google Sheet.",
        )
        db.session.add(row)
        db.session.add(JomBehaviorScore(workspace_id=int(workspace_id), customer_phone=phone))
        db.session.commit()
        imported += 1

        if send_fn:
            try:
                send_fn(phone, row.customer_name, row.destination)
                row.last_outbound_at = _now()
                row.status = cfg.STATUS_CONTACTED
                db.session.commit()
                messaged += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("[jom] opening template FAILED for %s: %s — approved?", phone, exc)

    r.last_synced_at = _now()
    r.last_status = "ok"
    r.last_error = None
    r.last_row_count = seen
    r.last_imported_count = imported
    r.total_imported = (r.total_imported or 0) + imported
    db.session.commit()
    logger.info("[jom] sheet sync ws %s: %s rows, %s new, %s messaged",
                workspace_id, seen, imported, messaged)
    return {"success": True, "rows": seen, "imported": imported, "messaged": messaged}


def run_jom_sheet_syncs():
    """Sweep all JOM workspaces with an enabled sheet. Called from the periodic tick."""
    from .gate import allowed_workspace_ids
    results = {}
    for ws in allowed_workspace_ids():
        try:
            r = get_config(ws)
            if r and r.enabled and r.sheet_url:
                results[str(ws)] = sync_workspace(ws)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[jom] sheet sweep failed ws %s: %s", ws, exc)
            results[str(ws)] = {"error": str(exc)}
    return results
