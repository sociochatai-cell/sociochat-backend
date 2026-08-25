import logging
import enum
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, Tuple

from core.db import db
from whatsapp.models import WhatsAppAccount

logger = logging.getLogger(__name__)

# ============================================================
# Enums
# ============================================================

class OperationalMode(enum.Enum):
    NORMAL = "normal"
    WARMUP = "warming_up"
    ADVISORY_SAFE_MODE = "advisory_safe_mode"
    DEGRADED = "degraded"
    RESTRICTED = "restricted"

class RiskClass(enum.Enum):
    LOW = "low"            # e.g., manual chat, basic text auto-reply
    MEDIUM = "medium"      # e.g., AI auto-reply, interactive flows
    HIGH = "high"          # e.g., bulk broadcasts, large templates
    VERY_HIGH = "very_high" # e.g., aggressive drip campaigns, cold outreach

class SafeModeReasonCode(enum.Enum):
    WEBHOOK_STALE = "WEBHOOK_STALE"
    QUALITY_YELLOW = "QUALITY_YELLOW"
    QUALITY_RED = "QUALITY_RED"
    SUBSCRIPTION_MISMATCH = "SUBSCRIPTION_MISMATCH"
    WARMUP_PROTECTION = "WARMUP_PROTECTION"
    HIGH_OUTBOUND_SPIKE = "HIGH_OUTBOUND_SPIKE"
    NOISY_ACCOUNT = "NOISY_ACCOUNT"

class MitigationType(enum.Enum):
    WEBHOOK_RETRY = "webhook_retry"
    WEBHOOK_REFRESH = "webhook_refresh"
    THROTTLE = "throttle"
    SUPPRESSION = "suppression"

# ============================================================
# Core Configuration
# ============================================================

# How long a metric must be stable before an auto-recovery from safe mode
COOLDOWN_HOURS_SAFE_MODE = 12
COOLDOWN_HOURS_DEGRADED = 24

# Self-healing retry limits to prevent retry storms
MAX_WEBHOOK_RETRIES_PER_DAY = 3

# ============================================================
# Operational Policies
# ============================================================

def is_risk_allowed(account: WhatsAppAccount, risk_class: RiskClass) -> bool:
    """
    Check if a specific risk class of automation is allowed given the account's operational mode.
    This preserves the advisory-first philosophy while safely mitigating degradation.
    """
    mode = getattr(OperationalMode, (account.operational_mode or "normal").upper(), OperationalMode.NORMAL)
    
    if mode == OperationalMode.NORMAL:
        return True
        
    if mode == OperationalMode.WARMUP:
        # In warmup, very high risk actions (like cold outreach drip) are throttled/rejected
        return risk_class != RiskClass.VERY_HIGH
        
    if mode == OperationalMode.ADVISORY_SAFE_MODE:
        # Advisory-first: when the account is in advisory-only posture (the
        # default), Safe Mode WARNS but does not block high-risk bulk sends.
        # Enforcement (suppressing High/Very-High) only applies when an operator
        # has explicitly cleared advisory-only via a time-boxed override.
        if bool(getattr(account, "safe_mode_advisory_only", True)):
            return True
        return risk_class in (RiskClass.LOW, RiskClass.MEDIUM)
        
    if mode == OperationalMode.DEGRADED:
        # If degraded (failing webhooks, yellow quality), only allow Low risk
        return risk_class == RiskClass.LOW
        
    if mode == OperationalMode.RESTRICTED:
        # If restricted by Meta, do not attempt ANY automated outbound
        return False

    return True

# ============================================================
# Logging & History
# ============================================================

def log_operational_adaptation(account_id: int, event_type: str, details: str, severity: str = "info", previous_mode: str = None, new_mode: str = None):
    """
    Record self-healing and operational changes for the explainability history.
    """
    logger.info(f"[SAFE_MODE_ENGINE] Account {account_id} | {event_type.upper()} | {details} | Severity: {severity}")
    
    try:
        from whatsapp.models import WhatsAppOperationalLog
        log = WhatsAppOperationalLog(
            account_id=account_id,
            event_type=event_type,
            reason=details,
            previous_mode=previous_mode,
            new_mode=new_mode,
            context={"severity": severity}
        )
        db.session.add(log)
    except Exception as e:
        logger.error(f"Failed to log operational adaptation to DB for account {account_id}: {e}")

def check_override_expiration(account: WhatsAppAccount) -> bool:
    """
    Check if a manual override has expired. If so, revert to advisory only.
    Returns True if state was modified.
    """
    if not account.safe_mode_advisory_only and account.safe_mode_override_expires_at:
        now = datetime.now(timezone.utc)
        expires_at = account.safe_mode_override_expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
            
        if now > expires_at:
            account.safe_mode_advisory_only = True
            account.safe_mode_override_expires_at = None
            log_operational_adaptation(account.id, "override_expired", "Operator override expired. Reverted to advisory safe mode logic.", "info")
            return True
            
    return False

# ============================================================
# Safe Mode Evaluations
# ============================================================

def evaluate_webhook_health(account: WhatsAppAccount) -> bool:
    """
    Check if webhook health requires entering safe mode or triggering a self-healing retry.
    Implements retry budgets, backoff, and exhaustion limits.
    Returns True if state was modified.
    """
    modified = False
    
    if account.webhook_health == "unhealthy" or account.webhook_subscription_status == "failed":
        now = datetime.now(timezone.utc)
        
        # 1. Check if we are in an active cooldown (backoff)
        if account.webhook_cooldown_ends_at:
            ends_at = account.webhook_cooldown_ends_at
            if ends_at.tzinfo is None: ends_at = ends_at.replace(tzinfo=timezone.utc)
            if now < ends_at:
                # Cooldown active, do not retry yet
                return modified
                
        # 2. Check retry limit (Self-Heal Exhaustion)
        # If they fail 3 times, we lock them out for 24h
        if account.webhook_failure_count < MAX_WEBHOOK_RETRIES_PER_DAY:
            logger.info(f"Account {account.id} attempting self-healing webhook retry...")
            log_operational_adaptation(account.id, "webhook_self_heal_started", "Attempting automatic WABA subscription retry.")
            
            # Execute webhook retry here
            from whatsapp.webhook_health import retry_subscribe_webhooks
            success, msg = retry_subscribe_webhooks(account)
            
            if success:
                account.webhook_failure_count = 0
                account.webhook_health = "healthy"
                account.webhook_subscription_status = "subscribed"
                account.webhook_cooldown_ends_at = None
                modified = True
                log_operational_adaptation(account.id, "webhook_self_heal_success", "Successfully repaired webhook subscription.")
            else:
                account.webhook_failure_count += 1
                # Exponential backoff: 30m, 2h, 24h
                backoff_hours = [0.5, 2.0, 24.0]
                idx = min(account.webhook_failure_count - 1, len(backoff_hours) - 1)
                account.webhook_cooldown_ends_at = now + timedelta(hours=backoff_hours[idx])
                modified = True
                log_operational_adaptation(account.id, "webhook_self_heal_failed", f"Retry failed. Backing off for {backoff_hours[idx]} hours.", "warning")
                
        # If retry failed or limit exceeded, escalate to safe mode
        if account.webhook_health != "healthy" and account.operational_mode != "degraded":
            old_mode = account.operational_mode
            account.operational_mode = "degraded"
            account.safe_mode_reason = "Prolonged webhook failure preventing message delivery tracking."
            account.safe_mode_reason_code = SafeModeReasonCode.WEBHOOK_STALE.value
            modified = True
            
            # Send an explicit operational alert
            logger.critical(f"[ALERT] Account {account.id} entered DEGRADED mode due to webhook self-heal exhaustion.")
            
            log_operational_adaptation(
                account.id, 
                "degradation_detected", 
                "Webhook failures exceeded retry threshold or active cooldown. Entered DEGRADED mode.", 
                "warning", 
                previous_mode=old_mode, 
                new_mode="degraded"
            )
            
            # Record degradation count in operational_metrics
            metrics = account.operational_metrics or {}
            metrics["degradation_count"] = metrics.get("degradation_count", 0) + 1
            account.operational_metrics = metrics
            
    return modified

def evaluate_quality_downgrade(account: WhatsAppAccount) -> bool:
    """
    Evaluate if a drop in Meta quality score warrants entering safe mode.
    Returns True if state was modified.
    """
    modified = False
    quality = (account.quality_score or "GREEN").upper()
    
    # We do not override WARMUP mode for YELLOW, but RED overrides everything
    if quality == "RED" and account.operational_mode != "degraded":
        old_mode = account.operational_mode
        account.operational_mode = "degraded"
        account.safe_mode_reason = "Meta quality score is RED. Severe risk of ban."
        account.safe_mode_reason_code = SafeModeReasonCode.QUALITY_RED.value
        modified = True
        log_operational_adaptation(account.id, "degradation_detected", "Quality score dropped to RED. Entering DEGRADED mode to prevent ban.", "critical", previous_mode=old_mode, new_mode="degraded")
        
        metrics = account.operational_metrics or {}
        metrics["degradation_count"] = metrics.get("degradation_count", 0) + 1
        account.operational_metrics = metrics
        
    elif quality == "YELLOW" and account.operational_mode == "normal":
        old_mode = account.operational_mode
        account.operational_mode = "advisory_safe_mode"
        account.safe_mode_reason = "Meta quality score dropped to YELLOW. High-risk broadcasts paused."
        account.safe_mode_reason_code = SafeModeReasonCode.QUALITY_YELLOW.value
        modified = True
        log_operational_adaptation(account.id, "safe_mode_entered", "Quality score dropped to YELLOW. Entering ADVISORY_SAFE_MODE.", "warning", previous_mode=old_mode, new_mode="advisory_safe_mode")
        
        metrics = account.operational_metrics or {}
        metrics["safe_mode_entries_count"] = metrics.get("safe_mode_entries_count", 0) + 1
        account.operational_metrics = metrics
        
    return modified

def evaluate_recovery_stabilization(account: WhatsAppAccount) -> bool:
    """
    Check if the account has been healthy for long enough to exit safe mode.
    Returns True if state was modified.
    """
    modified = False
    
    # Operator override blocks automatic recovery
    if not account.safe_mode_advisory_only:
        return False
        
    if account.operational_mode in ("advisory_safe_mode", "degraded"):
        # We need to know when it entered this mode. For now we use trust_score_computed_at or webhook_last_success_at
        # Assuming we just check current metrics
        quality = (account.quality_score or "GREEN").upper()
        
        # If metrics have recovered, we still enforce a cooldown.
        if quality == "GREEN" and account.webhook_health == "healthy":
            # Simplified cooldown check: Check if last webhook failure was more than 24 hours ago
            now = datetime.now(timezone.utc)
            last_fail = account.webhook_last_failure_at
            
            if last_fail:
                # Ensure it's offset-aware
                if last_fail.tzinfo is None:
                    last_fail = last_fail.replace(tzinfo=timezone.utc)
                hours_since_fail = (now - last_fail).total_seconds() / 3600
                
                metrics = account.operational_metrics or {}
                deg_count = metrics.get("degradation_count", 0)
                
                # Noisy accounts require longer stabilization (up to 48 hours max)
                required_cooldown = COOLDOWN_HOURS_SAFE_MODE + min(deg_count * 12, 48)
                
                if hours_since_fail < required_cooldown:
                    return False # Cooldown active
            
            # Metrics stable and cooldown passed -> Recover
            old_mode = account.operational_mode
            account.operational_mode = "normal"
            account.safe_mode_reason = None
            account.safe_mode_reason_code = None
            account.webhook_failure_count = 0 # Reset retry counter
            modified = True
            log_operational_adaptation(account.id, "recovery_detected", f"Metrics stabilized. Escaping {old_mode.upper()} mode.", "info", previous_mode=old_mode, new_mode="normal")
            
            metrics = account.operational_metrics or {}
            metrics["recovery_count"] = metrics.get("recovery_count", 0) + 1
            account.operational_metrics = metrics
            
    return modified

def process_safe_mode_tick(account: WhatsAppAccount) -> bool:
    """
    Main orchestration function to be called from the scheduler.
    Runs all evaluations and commits if changes were made.
    """
    modified = False
    
    if check_override_expiration(account):
        modified = True
        
    if evaluate_webhook_health(account):
        modified = True
        
    if evaluate_quality_downgrade(account):
        modified = True
        
    if evaluate_recovery_stabilization(account):
        modified = True
        
    return modified

def run_safe_mode_evaluations(limit: int = 100) -> Dict[str, int]:
    """
    Sweeps active accounts to evaluate safe mode transitions.
    Intended to be called by the scheduler.
    """
    accounts = WhatsAppAccount.query.filter_by(is_active=True).limit(limit).all()
    stats = {"scanned": len(accounts), "modified": 0}
    
    for account in accounts:
        try:
            if process_safe_mode_tick(account):
                stats["modified"] += 1
        except Exception as e:
            logger.error(f"Error processing safe mode tick for account {account.id}: {e}")
            
    if stats["modified"] > 0:
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(f"Failed to commit safe mode transitions: {e}")
            
    return stats
