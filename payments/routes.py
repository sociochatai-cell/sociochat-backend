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
    # Carry the origin the checkout started from through PayU (preserved in the
    # surl query string) so we can return the browser to THAT origin — the tenant's
    # own domain for white-label users — instead of the global platform host.
    from urllib.parse import quote_plus
    _ro = (data.get("return_origin") or "").strip()
    _ro_q = f"?ro={quote_plus(_ro)}" if _ro.startswith(("http://", "https://")) else ""
    surl = f"{app_base}/api/payments/payu/return{_ro_q}"
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

    # Recurring / auto-renew: only for user plans (not one-off tenant licenses).
    want_recurring = bool(data.get("recurring")) and ptype == LAYER_USER_PLAN

    db.session.commit()

    if want_recurring:
        from datetime import datetime, timezone
        from .mandate_service import add_billing_period, mandate_end_date
        from .mandate_models import PayuMandate, MANDATE_PENDING
        now = datetime.now(timezone.utc)
        _period = getattr(row, "billing_period", None) or "monthly"
        built = payu.build_si_registration_request(
            cfg, txnid=txnid, amount_inr=price, productinfo=product,
            firstname=getattr(user, "name", "") or "Customer",
            email=getattr(user, "email", "") or "",
            phone=getattr(user, "phone", "") or "",
            surl=surl, furl=furl,
            start_date=now, end_date=mandate_end_date(now),
        )
        try:
            # Retire any existing live mandate for this user, then register the new one.
            from .mandate_service import transition
            from .mandate_models import MANDATE_ACTIVE, MANDATE_PAUSED
            for m in PayuMandate.query.filter(
                PayuMandate.user_id == user.id,
                PayuMandate.status.in_([MANDATE_PENDING, MANDATE_ACTIVE, MANDATE_PAUSED]),
            ).all():
                m.status = "cancelled"
            db.session.add(PayuMandate(
                user_id=user.id, tenant_id=getattr(user, "tenant_id", None),
                plan_slug=slug, billing_period=_period, amount=int(price),
                currency="INR", status=MANDATE_PENDING, registration_txnid=txnid,
                payu_mode=cfg.mode, next_charge_at=add_billing_period(now, _period),
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("failed to create pending mandate for txn %s", txnid)
    else:
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


def _activate_mandate_if_any(txn: PaymentTransaction, posted: dict) -> None:
    """If this successful txn registered an autopay mandate, activate it and
    store the PayU SI reference for future recurring charges. Best-effort."""
    try:
        from .mandate_models import PayuMandate, MANDATE_PENDING
        from .mandate_service import transition, MANDATE_ACTIVE
    except Exception:
        return
    mandate = PayuMandate.query.filter_by(registration_txnid=txn.txnid).first()
    if not mandate or mandate.status != MANDATE_PENDING:
        return
    # SI reference: explicit si_reference from the callback, else the mihpayid.
    si_token = (posted.get("si_reference") or posted.get("mihpayid")
                or txn.payu_payment_id or "")
    transition(
        mandate, MANDATE_ACTIVE,
        si_token=str(si_token) if si_token else None,
        payu_mode=txn.payu_mode or mandate.payu_mode,
    )
    logger.info("PayU autopay mandate %s ACTIVATED (txn=%s, next_charge=%s)",
                mandate.id, txn.txnid, mandate.next_charge_at)


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
            _activate_mandate_if_any(txn, posted)
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

    # Return the browser to the SAME origin the checkout started from, so the
    # tenant's branding + session + localStorage (auth token, return path) all
    # survive — never dump a white-label tenant's user on the platform host.
    # Trusted origins = the platform FE and the tenant's own VERIFIED domain.
    tenant_origin = None
    try:
        if txn and getattr(txn, "tenant_id", None):
            from tenant.models import Tenant
            t = db.session.get(Tenant, txn.tenant_id)
            cd = (getattr(t, "custom_domain", None) or "").strip()
            if cd and getattr(t, "domain_verified", False):
                tenant_origin = (cd if cd.startswith(("http://", "https://")) else f"https://{cd}").rstrip("/")
    except Exception:
        logger.exception("payu_return: tenant origin resolution failed")

    # Prefer the exact client origin (from the surl query), but only if it's an
    # allowed origin — never open-redirect to an arbitrary host.
    ro = (request.args.get("ro") or "").strip().rstrip("/")
    allowed = {o for o in (fe, tenant_origin) if o}
    base = ro if ro in allowed else (tenant_origin or fe)
    redirect_to = f"{base}/payment/result?txnid={txnid}&status={result}"
    logger.info("PayU /return -> redirecting to %s (txn_status=%s, ro=%s, tenant_origin=%s)",
                redirect_to, getattr(txn, "status", None), ro, tenant_origin)
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


# --------------------------------------------------------------------------- #
# Autopay (recurring subscription) — user control + internal charge trigger
# --------------------------------------------------------------------------- #
@payments_bp.route("/autopay", methods=["GET"])
def autopay_status():
    """Current user's live auto-renew mandate (or none)."""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "error": "authentication_required"}), 401
    from .mandate_models import PayuMandate, MANDATE_PENDING, MANDATE_ACTIVE, MANDATE_PAUSED
    m = (PayuMandate.query
         .filter(PayuMandate.user_id == user.id,
                 PayuMandate.status.in_([MANDATE_PENDING, MANDATE_ACTIVE, MANDATE_PAUSED]))
         .order_by(PayuMandate.id.desc()).first())
    return jsonify({"success": True, "autopay": m.serialize() if m else None})


@payments_bp.route("/autopay/cancel", methods=["POST"])
def autopay_cancel():
    """User cancels auto-renew. Local cancel is authoritative — WE initiate every
    debit, so a cancelled mandate is never charged again regardless of PayU state.
    The current paid period is NOT refunded; access remains until it expires."""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "error": "authentication_required"}), 401
    from .mandate_models import PayuMandate, MANDATE_PENDING, MANDATE_ACTIVE, MANDATE_PAUSED, MANDATE_CANCELLED
    from .mandate_service import transition
    live = (PayuMandate.query
            .filter(PayuMandate.user_id == user.id,
                    PayuMandate.status.in_([MANDATE_PENDING, MANDATE_ACTIVE, MANDATE_PAUSED]))
            .all())
    if not live:
        return jsonify({"success": True, "message": "No active auto-renew to cancel."})
    for m in live:
        transition(m, MANDATE_CANCELLED, next_charge_at=None)
    db.session.commit()
    return jsonify({"success": True, "message": "Auto-renew cancelled."})


@payments_bp.route("/autopay/run", methods=["POST"])
def autopay_run():
    """Internal trigger for the recurring charge sweep (external cron / manual).
    Protected by the internal-jobs secret (same as other /api/internal jobs)."""
    import os
    secret = (os.getenv("INTERNAL_JOB_SECRET") or os.getenv("INTERNAL_API_SECRET") or "").strip()
    auth = (request.headers.get("Authorization") or "").strip()
    if not secret or auth != f"Bearer {secret}":
        return jsonify({"success": False, "error": "forbidden"}), 403
    from .autopay_jobs import run_due_autopay_charges, run_due_autopay_notifications
    notified = run_due_autopay_notifications()
    charged = run_due_autopay_charges()
    return jsonify({"success": True, "notified": notified, "charged": charged})
