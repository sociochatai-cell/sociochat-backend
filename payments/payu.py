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
