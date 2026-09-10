"""
PayU integration helper
=======================

Pure functions for the PayU India hosted-checkout (redirect) flow:

* ``build_payment_request`` — the form fields + action URL to POST the browser to
  PayU, including the SHA-512 request hash.
* ``verify_response_hash`` — recompute the reverse hash on PayU's callback and
  compare; this is what proves the callback is genuine and untampered.
* ``verify_payment_api`` — optional server-to-server status double-check.

The hash sequences are PayU's documented ones (identical for salt v1 and v2 on
the ``_payment`` flow):

  request : sha512(key|txnid|amount|productinfo|firstname|email|udf1..udf5||||||SALT)
  response: sha512(SALT|status||||||udf5..udf1|email|firstname|productinfo|amount|txnid|key)

We use no UDFs (all empty strings), so the txn is looked up purely by ``txnid``.
"""

import hashlib
import logging

import requests

logger = logging.getLogger(__name__)

# Number of UDF slots PayU expects in the hash (udf1..udf5).
_UDF_COUNT = 5


def _sha512(raw: str) -> str:
    return hashlib.sha512(raw.encode("utf-8")).hexdigest().lower()


def format_amount(amount_inr) -> str:
    """PayU wants a decimal amount string, e.g. ``1999.00``."""
    return f"{float(amount_inr):.2f}"


def _norm_udfs(udfs):
    udfs = list(udfs or [])
    udfs += [""] * (_UDF_COUNT - len(udfs))
    return udfs[:_UDF_COUNT]


def compute_request_hash(key: str, txnid: str, amount: str, productinfo: str,
                         firstname: str, email: str, salt: str,
                         udfs=None) -> str:
    udfs = _norm_udfs(udfs)
    # key|txnid|amount|productinfo|firstname|email|udf1..udf5 + 5 reserved empties + SALT
    fields = (
        [key, txnid, amount, productinfo, firstname, email]
        + udfs
        + ["", "", "", "", ""]
        + [salt]
    )
    return _sha512("|".join(fields))


def compute_response_hash(key: str, salt: str, txnid: str, amount: str,
                          productinfo: str, firstname: str, email: str,
                          status: str, udfs=None,
                          additional_charges: str = None) -> str:
    udfs = _norm_udfs(udfs)
    # reverse: SALT|status + 5 reserved empties + udf5..udf1 + email|firstname|productinfo|amount|txnid|key
    fields = (
        [salt, status]
        + ["", "", "", "", ""]
        + list(reversed(udfs))
        + [email, firstname, productinfo, amount, txnid, key]
    )
    raw = "|".join(fields)
    # When PayU sends additionalCharges, it is prepended to the hash string.
    if additional_charges:
        raw = additional_charges + "|" + raw
    return _sha512(raw)


def build_payment_request(cfg, *, txnid, amount_inr, productinfo, firstname,
                          email, phone, surl, furl) -> dict:
    """Return everything the browser needs to POST to PayU.

    ``cfg`` is a ``tenant.integration.PayUConfig`` (must be ``configured``).
    Returns ``{"action": <url>, "params": {...}}``; the frontend renders a
    self-submitting form with these params to ``action``.
    """
    amount = format_amount(amount_inr)
    productinfo = (productinfo or "Subscription")[:100]
    firstname = (firstname or "Customer")[:60]
    h = compute_request_hash(
        key=cfg.key, txnid=txnid, amount=amount, productinfo=productinfo,
        firstname=firstname, email=email or "", salt=cfg.salt,
    )
    params = {
        "key": cfg.key,
        "txnid": txnid,
        "amount": amount,
        "productinfo": productinfo,
        "firstname": firstname,
        "email": email or "",
        "phone": (phone or "")[:15],
        "surl": surl,
        "furl": furl,
        "curl": surl,
        "hash": h,
    }
    return {"action": cfg.base_url, "params": params}


def verify_response_hash(cfg, posted: dict) -> bool:
    """True if PayU's posted-back ``hash`` matches our recomputed reverse hash."""
    try:
        their_hash = (posted.get("hash") or "").strip().lower()
        if not their_hash:
            return False
        udfs = [posted.get(f"udf{i}", "") or "" for i in range(1, _UDF_COUNT + 1)]
        ours = compute_response_hash(
            key=cfg.key, salt=cfg.salt,
            txnid=posted.get("txnid", "") or "",
            amount=posted.get("amount", "") or "",
            productinfo=posted.get("productinfo", "") or "",
            firstname=posted.get("firstname", "") or "",
            email=posted.get("email", "") or "",
            status=posted.get("status", "") or "",
            udfs=udfs,
            additional_charges=posted.get("additionalCharges"),
        )
        return ours == their_hash
    except Exception:
        logger.exception("verify_response_hash failed")
        return False


def build_si_details(amount_inr, start_date, end_date) -> dict:
    """si_details dict for a PayU SI (Standing Instruction) checkout.
    Posted as a form field on the registration checkout (NOT part of the hash)."""
    def _d(v):
        return v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else str(v)
    return {
        "billingAmount": format_amount(amount_inr),
        "billingCurrency": "INR",
        "billingCycle": "MONTHLY",
        "billingInterval": 1,
        "paymentStartDate": _d(start_date),
        "paymentEndDate": _d(end_date),
    }


def build_si_registration_request(cfg, *, txnid, amount_inr, productinfo, firstname,
                                  email, phone, surl, furl, start_date, end_date) -> dict:
    """PayU checkout that REGISTERS an autopay mandate + takes the first charge.

    Same as a normal checkout PLUS: ``si=1``, the ``si_details`` JSON field, and
    ``udf4=1`` (our autopay marker — udf4 IS part of the standard hash, so the
    hash is computed with it). si_details itself is NOT hashed (validated against
    PayU test)."""
    import json
    amount = format_amount(amount_inr)
    productinfo = (productinfo or "Subscription")[:100]
    firstname = (firstname or "Customer")[:60]
    udfs = ["", "", "", "1", ""]  # udf4=1 marks autopay
    h = compute_request_hash(
        key=cfg.key, txnid=txnid, amount=amount, productinfo=productinfo,
        firstname=firstname, email=email or "", salt=cfg.salt, udfs=udfs,
    )
    si_json = json.dumps(build_si_details(amount_inr, start_date, end_date), separators=(",", ":"))
    params = {
        "key": cfg.key, "txnid": txnid, "amount": amount, "productinfo": productinfo,
        "firstname": firstname, "email": email or "", "phone": (phone or "")[:15],
        "surl": surl, "furl": furl, "curl": surl, "hash": h,
        "udf1": "", "udf2": "", "udf3": "", "udf4": "1", "udf5": "",
        "si": "1", "si_details": si_json,
    }
    return {"action": cfg.base_url, "params": params}


def _postservice(cfg, command: str, var1: str, timeout: int = 20) -> dict:
    """PayU merchant postservice (form=2) call. hash = sha512(key|command|var1|salt)."""
    hash_ = _sha512(f"{cfg.key}|{command}|{var1}|{cfg.salt}")
    try:
        resp = requests.post(cfg.verify_url, data={
            "key": cfg.key, "command": command, "var1": var1, "hash": hash_,
        }, timeout=timeout)
        return resp.json()
    except Exception:
        logger.exception("PayU postservice %s failed", command)
        return {}


def charge_si(cfg, *, si_token: str, txnid: str, amount_inr, phone: str = "",
              email: str = "", invoice: str = None) -> dict:
    """Merchant-initiated recurring debit of a registered mandate via
    ``si_transaction``. Returns the raw PayU response dict ({} on transport error)."""
    import json
    var1 = json.dumps({
        "authpayuid": str(si_token),
        "invoiceDisplayNumber": str(invoice or txnid),
        "amount": format_amount(amount_inr),
        "txnid": str(txnid),
        "phone": str(phone or ""),
        "email": str(email or ""),
    }, separators=(",", ":"))
    return _postservice(cfg, "si_transaction", var1)


def pre_debit_notify(cfg, *, si_token: str, amount_inr, charge_date, request_id: str = None) -> dict:
    """RBI-mandated pre-debit notification for an upcoming SI charge (command
    ``pre_debit_SI``). Must reach the customer 24h+ before the debit (PayU
    recommends 48h) or PayU will NOT execute the recurring charge on LIVE."""
    import json
    def _d(v):
        return v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else str(v)
    req_id = str(request_id or f"predebit-{si_token}-{_d(charge_date)}")
    var1 = json.dumps({
        "authPayuId": str(si_token),
        "requestId": req_id,
        "debitDate": _d(charge_date),
        "amount": format_amount(amount_inr),
    }, separators=(",", ":"))
    return _postservice(cfg, "pre_debit_SI", var1)


def pre_debit_succeeded(resp: dict) -> bool:
    """True on an explicit success from a pre_debit_SI response."""
    if not isinstance(resp, dict):
        return False
    status = str(resp.get("status", "")).lower()
    return status in ("1", "success", "true") or bool(resp.get("requestId"))


def si_charge_succeeded(resp: dict) -> bool:
    """True only on an explicit success from a si_transaction response."""
    if not isinstance(resp, dict):
        return False
    status = str(resp.get("status", "")).lower()
    txn_status = str(resp.get("transaction_status", "") or resp.get("unmappedstatus", "")).lower()
    return status in ("1", "success", "true") or txn_status in ("success", "captured")


def verify_payment_api(cfg, txnid: str) -> dict:
    """Server-to-server status check (source of truth). Returns the parsed
    ``transaction_details`` entry for ``txnid`` or ``{}`` on any error."""
    try:
        command = "verify_payment"
        hash_raw = f"{cfg.key}|{command}|{txnid}|{cfg.salt}"
        payload = {
            "key": cfg.key,
            "command": command,
            "var1": txnid,
            "hash": _sha512(hash_raw),
        }
        resp = requests.post(cfg.verify_url, data=payload, timeout=20)
        data = resp.json()
        details = (data.get("transaction_details") or {}).get(txnid) or {}
        return details
    except Exception:
        logger.exception("verify_payment_api failed for txnid=%s", txnid)
        return {}
