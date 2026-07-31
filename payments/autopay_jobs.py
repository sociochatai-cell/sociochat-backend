"""
Recurring autopay charge sweep — the job that actually re-bills subscriptions.

For every ACTIVE mandate whose ``next_charge_at`` is due, initiate a PayU
``si_transaction`` debit. On success: extend the user's subscription (same path
as a normal payment) and advance ``next_charge_at``. On failure: retry daily up
to 3 times, then retire the mandate. Idempotent per charge txnid.

Runs in-process from the scheduler tick and is also callable via the internal
run endpoint.
"""

import logging
from datetime import datetime, timezone, timedelta

from models import db, User
from tenant.integration import get_tenant_payu_config
from . import payu
from .mandate_models import PayuMandate, PayuMandateCharge, MANDATE_ACTIVE, MANDATE_FAILED
from .mandate_service import transition, add_billing_period, MAX_CONSECUTIVE_FAILURES

logger = logging.getLogger(__name__)


def _gen_charge_txnid(mandate_id: int) -> str:
    import uuid
    return f"SIC{mandate_id}{uuid.uuid4().hex[:10]}"


def _needs_predebit(mandate: PayuMandate, now: datetime) -> bool:
    """LIVE mandates must have a valid pre-debit notice (within this cycle) before
    a charge. Test/sandbox does not enforce it, so we don't block there."""
    if (mandate.payu_mode or "test").lower() != "live":
        return False
    if mandate.notified_at is None:
        return True
    # Notice must belong to THIS billing cycle (after the previous charge window).
    return mandate.notified_at < (mandate.next_charge_at - timedelta(days=32))


def _charge_one(mandate: PayuMandate, now: datetime) -> str:
    user = db.session.get(User, mandate.user_id)
    if not user:
        transition(mandate, MANDATE_FAILED, last_charge_status="user_missing")
        db.session.commit()
        return "user_missing"

    # RBI: never debit a live mandate the customer wasn't pre-notified for.
    if _needs_predebit(mandate, now):
        logger.info("autopay: mandate %s awaiting pre-debit notification; skipping charge", mandate.id)
        return "awaiting_notice"

    cfg = get_tenant_payu_config(tenant_id=mandate.tenant_id)
    if not getattr(cfg, "configured", False) or not mandate.si_token:
        # Can't charge without a live gateway / SI token — retry later, don't fail hard.
        mandate.next_charge_at = now + timedelta(days=1)
        db.session.commit()
        return "not_chargeable"

    txnid = _gen_charge_txnid(mandate.id)
    charge = PayuMandateCharge(mandate_id=mandate.id, txnid=txnid,
                               amount=mandate.amount, status="created")
    db.session.add(charge)
    db.session.commit()

    resp = payu.charge_si(
        cfg, si_token=mandate.si_token, txnid=txnid, amount_inr=mandate.amount,
        phone=getattr(user, "phone", "") or "", email=getattr(user, "email", "") or "",
        invoice=txnid,
    )

    if payu.si_charge_succeeded(resp):
        charge.status = "success"
        charge.payu_id = str(resp.get("mihpayid") or resp.get("payuMoneyId") or "")
        # Extend the subscription via the same activation path as a normal payment.
        try:
            from .models import PaymentTransaction, STATUS_INITIATED
            from .routes import _activate_from_txn
            txn = PaymentTransaction(
                txnid=txnid, tenant_id=mandate.tenant_id, user_id=mandate.user_id,
                layer="user_plan", plan_slug=mandate.plan_slug, amount=int(mandate.amount),
                currency="INR", status=STATUS_INITIATED, provider="payu",
                payu_mode=mandate.payu_mode, payu_payment_id=charge.payu_id,
            )
            db.session.add(txn)
            _activate_from_txn(txn)
        except Exception:
            logger.exception("autopay: subscription extend failed for mandate %s", mandate.id)
        mandate.last_charge_at = now
        mandate.last_charge_status = "success"
        mandate.consecutive_failures = 0
        mandate.next_charge_at = add_billing_period(now, mandate.billing_period)
        db.session.commit()
        logger.info("autopay: mandate %s charged OK txn=%s next=%s",
                    mandate.id, txnid, mandate.next_charge_at)
        return "success"

    # Failure path
    charge.status = "failed"
    charge.error = str(resp)[:2000] if resp else "no_response"
    mandate.last_charge_at = now
    mandate.last_charge_status = "failed"
    mandate.consecutive_failures = (mandate.consecutive_failures or 0) + 1
    if mandate.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        transition(mandate, MANDATE_FAILED)
    else:
        mandate.next_charge_at = now + timedelta(days=1)  # retry tomorrow
    db.session.commit()
    logger.warning("autopay: mandate %s charge FAILED (%s/%s)",
                   mandate.id, mandate.consecutive_failures, MAX_CONSECUTIVE_FAILURES)
    return "failed"


def run_due_autopay_charges(limit: int = 50) -> dict:
    """Charge all active mandates whose next_charge_at is due. Returns a summary."""
    now = datetime.now(timezone.utc)
    due = (PayuMandate.query
           .filter(PayuMandate.status == MANDATE_ACTIVE,
                   PayuMandate.next_charge_at != None,  # noqa: E711
                   PayuMandate.next_charge_at <= now)
           .order_by(PayuMandate.next_charge_at.asc())
           .limit(limit).all())
    results = {"due": len(due), "success": 0, "failed": 0, "other": 0}
    for m in due:
        try:
            r = _charge_one(m, now)
            results["success" if r == "success" else "failed" if r == "failed" else "other"] += 1
        except Exception:
            db.session.rollback()
            logger.exception("autopay: unexpected error charging mandate %s", getattr(m, "id", "?"))
            results["other"] += 1
    if due:
        logger.info("autopay sweep: %s", results)
    return results


def run_due_autopay_notifications(hours_ahead: int = 48, limit: int = 100) -> dict:
    """RBI pre-debit notification sweep. For every active mandate whose next
    charge is within ``hours_ahead`` and which hasn't been notified for this
    cycle, fire PayU's pre_debit_SI and stamp ``notified_at``. Idempotent per
    cycle. Only meaningful for LIVE mandates (test/sandbox doesn't enforce it),
    but we notify regardless so the flow is exercised."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=hours_ahead)
    due = (PayuMandate.query
           .filter(PayuMandate.status == MANDATE_ACTIVE,
                   PayuMandate.si_token.isnot(None),
                   PayuMandate.next_charge_at.isnot(None),
                   PayuMandate.next_charge_at <= horizon)
           .order_by(PayuMandate.next_charge_at.asc())
           .limit(limit).all())
    results = {"candidates": 0, "notified": 0, "failed": 0, "skipped": 0}
    for m in due:
        # Already notified for this cycle? (notice newer than one period back)
        if m.notified_at is not None and m.notified_at >= (m.next_charge_at - timedelta(days=32)):
            results["skipped"] += 1
            continue
        results["candidates"] += 1
        try:
            cfg = get_tenant_payu_config(tenant_id=m.tenant_id)
            if not getattr(cfg, "configured", False):
                results["skipped"] += 1
                continue
            resp = payu.pre_debit_notify(
                cfg, si_token=m.si_token, amount_inr=m.amount, charge_date=m.next_charge_at,
            )
            if payu.pre_debit_succeeded(resp):
                m.notified_at = now
                db.session.commit()
                results["notified"] += 1
                logger.info("autopay: pre-debit notice sent for mandate %s (charge %s)",
                            m.id, m.next_charge_at)
            else:
                results["failed"] += 1
                logger.warning("autopay: pre-debit notice FAILED for mandate %s: %s", m.id, resp)
        except Exception:
            db.session.rollback()
            results["failed"] += 1
            logger.exception("autopay: pre-debit notify error for mandate %s", getattr(m, "id", "?"))
    if due:
        logger.info("autopay notify sweep: %s", results)
    return results
