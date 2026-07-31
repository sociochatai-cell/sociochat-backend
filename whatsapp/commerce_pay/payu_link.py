# whatsapp/commerce_pay/payu_link.py
"""
PayU payment-link plumbing for in-chat "Request Payment" (SocioChat-only).
=========================================================================

Flow (self-hosted redirect, works with just Merchant Key + Salt — no extra
PayU API onboarding):

  1. request-payment  -> create a CommerceOrder (txnid) + send a link into chat.
  2. GET /pay/<txnid> -> build the PayU ``_payment`` form (with request hash)
                         for THAT business's credentials and auto-submit the
                         customer's browser to PayU checkout.
  3. PayU callback    -> verify the reverse hash, mark the order paid/failed,
                         and drop a "✅ Payment received" reply into the chat.

Reuses the audited pure hash/form helpers in ``payments/payu.py`` so the
signing logic lives in exactly one place.
"""

import logging
import os

from payments import payu as payu_core

logger = logging.getLogger(__name__)


def _public_base(host_url: str) -> str:
    """Public base URL for customer-facing pay links / callbacks.

    Precedence:
      1. COMMERCE_PUBLIC_BASE_URL  — explicit override (always wins).
      2. request host_url          — the live request host (inline/API mode).
      3. APP_BASE_URL / OAUTH_REDIRECT_BASE — the app's configured public base,
         used when there is NO request context (queue WORKER mode) so auto-pay
         links generated off the webhook are still absolute + reachable.

    This is why auto-pay worked locally (inline → host_url present) but produced
    a broken relative link once deployed behind a Redis worker (no request).
    """
    override = (os.getenv("COMMERCE_PUBLIC_BASE_URL") or "").strip()
    if override:
        return override.rstrip("/")
    if host_url:
        return host_url.rstrip("/")
    for env_name in ("APP_BASE_URL", "OAUTH_REDIRECT_BASE", "BACKEND_PUBLIC_URL"):
        val = (os.getenv(env_name) or "").strip()
        if val:
            return val.rstrip("/")
    return ""


class PayUCfg:
    """Minimal cfg object shaped like ``tenant.integration.PayUConfig`` so the
    reusable ``payments.payu`` helpers accept it, but backed by a per-business
    ``WorkspacePaymentConfig`` (key + decrypted salt + mode)."""

    def __init__(self, key: str, salt: str, mode: str):
        self.key = key
        self.salt = salt
        self.mode = (mode or "test").lower()
        live = self.mode == "live"
        self.base_url = (
            "https://secure.payu.in/_payment" if live
            else "https://test.payu.in/_payment"
        )
        self.verify_url = (
            "https://info.payu.in/merchant/postservice.php?form=2" if live
            else "https://test.payu.in/merchant/postservice?form=2"
        )


def cfg_from_row(row) -> "PayUCfg | None":
    """Build a PayUCfg from a connected WorkspacePaymentConfig row (or None)."""
    if not row or not row.is_connected:
        return None
    salt = row.get_salt()
    if not row.merchant_key or not salt:
        return None
    return PayUCfg(key=row.merchant_key, salt=salt, mode=row.mode)


def pay_link(host_url: str, txnid: str) -> str:
    return f"{_public_base(host_url)}/api/whatsapp/commerce/pay/{txnid}"


def callback_url(host_url: str) -> str:
    return f"{_public_base(host_url)}/api/whatsapp/commerce/payu/callback"


def build_checkout(cfg: "PayUCfg", order, host_url: str) -> dict:
    """Return ``{"action": <payu url>, "params": {...}}`` for the auto-submit form."""
    surl = furl = callback_url(host_url)
    return payu_core.build_payment_request(
        cfg,
        txnid=order.txnid,
        amount_inr=order.amount,
        productinfo=order.productinfo,
        firstname=order.customer_name or "Customer",
        email=order.customer_email or "",
        phone=order.customer_phone or "",
        surl=surl,
        furl=furl,
    )


def render_autosubmit(checkout: dict) -> str:
    """Tiny HTML page that POSTs the customer's browser to PayU on load."""
    import html
    action = html.escape(checkout["action"], quote=True)
    fields = "\n".join(
        f'    <input type="hidden" name="{html.escape(str(k), quote=True)}" '
        f'value="{html.escape(str(v), quote=True)}">'
        for k, v in checkout["params"].items()
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Redirecting to secure payment…</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;display:flex;
min-height:100vh;align-items:center;justify-content:center;margin:0;background:#f6f7f9;color:#333}}
.box{{text-align:center}}.spin{{width:34px;height:34px;border:3px solid #ddd;border-top-color:#128C7E;
border-radius:50%;animation:s 1s linear infinite;margin:0 auto 14px}}@keyframes s{{to{{transform:rotate(360deg)}}}}</style>
</head><body>
<form id="payu" method="post" action="{action}">
{fields}
</form>
<div class="box"><div class="spin"></div>Redirecting to secure PayU checkout…</div>
<script>document.getElementById('payu').submit();</script>
</body></html>"""


def _thankyou(title: str, msg: str, ok: bool) -> str:
    color = "#128C7E" if ok else "#c0392b"
    icon = "✅" if ok else "⚠️"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;display:flex;
min-height:100vh;align-items:center;justify-content:center;margin:0;background:#f6f7f9;color:#333}}
.card{{background:#fff;border-radius:14px;padding:32px 28px;box-shadow:0 6px 24px rgba(0,0,0,.08);
text-align:center;max-width:360px}}.i{{font-size:44px}}h2{{margin:12px 0 6px;color:{color}}}
p{{color:#666;margin:0}}</style></head><body>
<div class="card"><div class="i">{icon}</div><h2>{title}</h2><p>{msg}</p>
<p style="margin-top:14px;font-size:13px">You can close this tab and return to WhatsApp.</p></div>
</body></html>"""


def send_chat_text(workspace_id, phone: str, text: str, conversation_id=None) -> bool:
    """Send a plain text message into the customer's chat for this workspace."""
    try:
        from whatsapp.services import WhatsAppService
        from models import db
        service = WhatsAppService(db.session, workspace_id=str(workspace_id))
        res = service.send_text(
            to=phone, text=text, preview_url=True, conversation_id=conversation_id
        )
        return bool(res and res.get("success", True) is not False)
    except Exception:
        logger.exception("send_chat_text failed (workspace=%s phone=%s)", workspace_id, phone)
        return False
