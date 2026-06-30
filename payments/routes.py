"""
Payments API Routes (PayU)
==========================

Flow (no plan activates without a verified payment):

  POST /api/payments/initiate           (auth) -> create txn, return PayU form params
  POST /api/payments/payu/return        (PayU browser redirect, surl/furl)
  POST /api/payments/payu/webhook       (PayU server-to-server)
  GET  /api/payments/status/<txnid>     (auth) -> poll txn status from the SPA
"""

import os
import json
import uuid
import logging
from datetime import datetime, timezone, timedelta

from flask import Blueprint, request, jsonify, redirect

from models import db, User
from tenant.context import get_current_user
from tenant.integration import get_tenant_payu_config, get_platform_payu_config
from payments.models import (
    PaymentTransaction,
    LAYER_USER_PLAN, LAYER_TENANT_LICENSE,
    STATUS_INITIATED, STATUS_SUCCESS, STATUS_FAILED,
)
from payments import payu

logger = logging.getLogger(__name__)

payments_bp = Blueprint("payments", __name__, url_prefix="/api/payments")

# How long a paid subscription lasts before re-payment (monthly billing).
_SUBSCRIPTION_DAYS = 30


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _gen_txnid() -> str:
    return ("SC" + uuid.uuid4().hex)[:36]


def _app_base() -> str:
    return (os.getenv("APP_BASE_URL") or "").rstrip("/")


def _frontend_base() -> str:
    return (os.getenv("FRONTEND_BASE_URL") or os.getenv("APP_BASE_URL") or "").rstrip("/")


def _user_plan_row(slug: str):
    from subscription.plan_models import SubscriptionPlan
    return SubscriptionPlan.query.filter_by(slug=slug).first()


def _tenant_plan_row(slug: str):
    from tenant.tenant_plan_models import TenantPlan
    return TenantPlan.query.filter_by(slug=slug).first()


def _payee_config_for_txn(txn: PaymentTransaction):
    """Resolve the SAME PayU account used at initiate, so the reverse hash uses
    the right salt."""
    if txn.layer == LAYER_TENANT_LICENSE:
        return get_platform_payu_config()
    return get_tenant_payu_config(tenant_id=txn.payee_tenant_id or txn.tenant_id)


# --------------------------------------------------------------------------- #
# Initiate
# --------------------------------------------------------------------------- #
@payments_bp.route("/initiate", methods=["POST"])
def initiate():
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "error": "authentication_required"}), 401

    data = request.get_json(silent=True) or {}
    ptype = (data.get("type") or "user_plan").strip()
    slug = (data.get("plan") or data.get("plan_slug") or "").strip()
    if not slug:
        return jsonify({"success": False, "error": "plan_required"}), 400

    app_base = _app_base()
    if not app_base:
        return jsonify({"success": False, "error": "app_base_url_not_configured"}), 500
    surl = f"{app_base}/api/payments/payu/return"
    furl = surl

    # ---- Resolve plan, price, payee account by layer ----
    if ptype == LAYER_TENANT_LICENSE:
        # Match the roles allowed by require_tenant_admin (tenant_admin/admin/
        # marketing_admin) so a user who can open the page isn't rejected here.
        from tenant.context import TENANT_ADMIN_ROLES
        if (getattr(user, "role", "") or "") not in TENANT_ADMIN_ROLES:
            return jsonify({"success": False, "error": "tenant_admin_required"}), 403
        from tenant import service as tenant_service
        if not tenant_service.is_tenant_plan_assignable(user.tenant_id, slug):
            return jsonify({"success": False, "error": "invalid_plan"}), 400
        row = _tenant_plan_row(slug)
        price = row.price_inr if row else None
        product = row.name if row else slug
        if not price or price <= 0:
            # Free or custom (enterprise) license cannot be auto-charged.
            return jsonify({"success": False, "error": "plan_not_payable"}), 400
        cfg = get_platform_payu_config()
        payee_tenant_id = None
        billing_scope = None
    else:
        ptype = LAYER_USER_PLAN
        from subscription.service import get_assignable_user_plan_slugs
        if slug not in get_assignable_user_plan_slugs(user):
            return jsonify({"success": False, "error": "invalid_plan"}), 400
        row = _user_plan_row(slug)
        price = row.price_monthly_inr if row else None
        product = row.name if row else slug
        if not price or price <= 0:
            return jsonify({"success": False, "error": "plan_not_payable"}), 400
        cfg = get_tenant_payu_config(tenant_id=getattr(user, "tenant_id", None))
        payee_tenant_id = getattr(user, "tenant_id", None)
        billing_scope = getattr(user, "billing_scope", None)

    if not cfg.configured:
        # Tenant (or platform) hasn't set up PayU — clear, actionable error.
        return jsonify({
            "success": False,
            "error": "payu_not_configured",
            "is_tenant_license": ptype == LAYER_TENANT_LICENSE,
        }), 400

    txnid = _gen_txnid()
    txn = PaymentTransaction(
        txnid=txnid,
        tenant_id=getattr(user, "tenant_id", None),
        user_id=user.id,
        payee_tenant_id=payee_tenant_id,
        layer=ptype,
        plan_slug=slug,
        billing_scope=billing_scope,
        amount=int(price),
        currency="INR",
        status=STATUS_INITIATED,
        provider="payu",
        payu_mode=cfg.mode,
    )
    db.session.add(txn)

    # Record the intended plan on the user so it's visible as "pending" until paid
    # (does NOT grant access — access only flips in the verified callback).
    if ptype == LAYER_USER_PLAN:
        user.pending_plan = slug
        user.pending_billing = billing_scope

    db.session.commit()

    built = payu.build_payment_request(
        cfg,
        txnid=txnid,
        amount_inr=price,
        productinfo=product,
        firstname=getattr(user, "name", "") or "Customer",
        email=getattr(user, "email", "") or "",
        phone=getattr(user, "phone", "") or "",
        surl=surl,
        furl=furl,
    )
    logger.info(
        "PayU initiate OK txnid=%s layer=%s plan=%s amount=%s mode=%s key=%s "
        "action=%s surl=%s",
        txnid, ptype, slug, built["params"].get("amount"), cfg.mode,
        cfg.key, built["action"], surl,
    )
    return jsonify({
        "success": True,
        "txnid": txnid,
        "action": built["action"],
        "params": built["params"],
    })


# --------------------------------------------------------------------------- #
# Activation (shared, idempotent)
# --------------------------------------------------------------------------- #
def _activate_from_txn(txn: PaymentTransaction) -> None:
    """Apply the paid plan. Caller must have already verified the payment.
    Idempotent: a txn already marked success is left untouched."""
    if txn.status == STATUS_SUCCESS:
        return
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=_SUBSCRIPTION_DAYS)

    if txn.layer == LAYER_USER_PLAN:
        user = db.session.get(User, txn.user_id) if txn.user_id else None
        if not user:
            raise RuntimeError(f"user {txn.user_id} not found for txn {txn.txnid}")
        old_plan = user.plan or "beta"
        user.plan = txn.plan_slug
        if txn.billing_scope:
            user.billing_scope = txn.billing_scope
        from subscription.service import expiry_from_period
        from subscription.plan_models import SubscriptionPlan
        _sp = SubscriptionPlan.query.filter_by(slug=txn.plan_slug).first()
        user.subscription_expires_at = expiry_from_period(now, getattr(_sp, "billing_period", None)) or expires
        user.pending_plan = None
        user.pending_billing = None
        try:
            from subscription.models import PlanChangeHistory
            db.session.add(PlanChangeHistory(
                user_id=user.id, old_plan=old_plan, new_plan=txn.plan_slug,
                changed_by_admin_id=None, reason=f"payu_payment:{txn.txnid}",
            ))
        except Exception:
            logger.exception("PlanChangeHistory record failed for txn %s", txn.txnid)
    elif txn.layer == LAYER_TENANT_LICENSE:
        from tenant.models import Tenant
        from tenant import service as tenant_service
        tenant = db.session.get(Tenant, txn.tenant_id) if txn.tenant_id else None
        if not tenant:
            raise RuntimeError(f"tenant {txn.tenant_id} not found for txn {txn.txnid}")
        tenant_service.set_tenant_subscription(
            tenant, txn.plan_slug, expires_at=None, admin=None,
            reason=f"payu_payment:{txn.txnid}", payment_status="active",
        )

    txn.status = STATUS_SUCCESS
    txn.completed_at = now


def _process_payu_result(posted: dict) -> PaymentTransaction:
    """Verify a PayU callback/webhook and update the txn. Returns the txn (or
    None if the txnid is unknown)."""
    txnid = (posted.get("txnid") or "").strip()
    if not txnid:
        return None
    txn = PaymentTransaction.query.filter_by(txnid=txnid).first()
    if not txn:
        logger.warning("PayU callback for unknown txnid=%s", txnid)
        return None

    # Already finalized — idempotent no-op (PayU retries webhooks).
    if txn.status == STATUS_SUCCESS:
        return txn

    txn.raw_response = json.dumps(posted)[:8000]
    txn.payu_payment_id = posted.get("mihpayid") or txn.payu_payment_id
    txn.payu_status = posted.get("status") or txn.payu_status

    cfg = _payee_config_for_txn(txn)
    status = (posted.get("status") or "").strip().lower()
    hash_ok = payu.verify_response_hash(cfg, posted)

    # Amount tamper check: PayU's posted amount must match what we charged.
    amount_ok = True
    try:
        amount_ok = abs(float(posted.get("amount") or 0) - float(txn.amount)) < 0.01
    except (TypeError, ValueError):
        amount_ok = False

    logger.info(
        "PayU return txnid=%s payu_status=%s hash_ok=%s amount_ok=%s "
        "posted_amount=%s our_amount=%s error_msg=%s",
        txnid, status, hash_ok, amount_ok,
        posted.get("amount"), txn.amount, posted.get("error_Message") or posted.get("error"),
    )

    if status == "success" and hash_ok and amount_ok:
        try:
            _activate_from_txn(txn)
        except Exception:
            logger.exception("Activation failed for txn %s", txn.txnid)
            txn.status = STATUS_FAILED
            txn.error = "activation_failed"
    else:
        txn.status = STATUS_FAILED
        if not hash_ok:
            txn.error = "hash_verification_failed"
        elif not amount_ok:
            txn.error = "amount_mismatch"
        else:
            txn.error = f"payu_status:{status}"

    db.session.commit()
    return txn


# --------------------------------------------------------------------------- #
# PayU browser redirect (surl / furl)
# --------------------------------------------------------------------------- #
@payments_bp.route("/payu/return", methods=["POST", "GET"])
def payu_return():
    posted = request.form.to_dict() if request.form else request.args.to_dict()
    logger.info("PayU /return hit: keys=%s status=%s txnid=%s",
                list(posted.keys()), posted.get("status"), posted.get("txnid"))
    txn = _process_payu_result(posted)
    fe = _frontend_base()
    txnid = (posted.get("txnid") or "").strip()
    result = "success" if (txn and txn.status == STATUS_SUCCESS) else "failed"
    redirect_to = f"{fe}/payment/result?txnid={txnid}&status={result}"
    logger.info("PayU /return -> redirecting to %s (txn_status=%s)",
                redirect_to, getattr(txn, "status", None))
    return redirect(redirect_to, code=302)


# --------------------------------------------------------------------------- #
# PayU server-to-server webhook
# --------------------------------------------------------------------------- #
@payments_bp.route("/payu/webhook", methods=["POST"])
def payu_webhook():
    posted = request.form.to_dict() if request.form else (request.get_json(silent=True) or {})
    _process_payu_result(posted)
    # PayU only needs a 200 to stop retrying.
    return ("", 200)


# --------------------------------------------------------------------------- #
# Status poll
# --------------------------------------------------------------------------- #
@payments_bp.route("/status/<txnid>", methods=["GET"])
def status(txnid):
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "error": "authentication_required"}), 401
    txn = PaymentTransaction.query.filter_by(txnid=txnid).first()
    if not txn:
        return jsonify({"success": False, "error": "not_found"}), 404
    # A user may only read their own transactions.
    if txn.user_id and txn.user_id != user.id:
        return jsonify({"success": False, "error": "forbidden"}), 403
    return jsonify({"success": True, "transaction": txn.serialize()})
