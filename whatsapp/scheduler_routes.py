import os
import logging
from flask import Blueprint, request, jsonify

from core.deployment_safety import scheduler_secret_acceptable_for_environment
from whatsapp.drip_engine import check_scheduled_campaigns, process_due_drip_enrollments

logger = logging.getLogger(__name__)

scheduler_bp = Blueprint("scheduler", __name__)

# To keep it secure, we expect GCP Cloud Scheduler to pass an authorization token
# This token should match the SCHEDULER_SECRET_TOKEN environment variable.
_DEFAULT_INSECURE = "default-insecure-token-please-change-me"

def _verify_scheduler_token():
    """Verify the authorization header matches our secret token."""
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        return False

    parts = auth_header.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        token = parts[1]
    else:
        token = auth_header

    expected_secret = os.getenv("SCHEDULER_SECRET_TOKEN", _DEFAULT_INSECURE).strip()
    return token == expected_secret

@scheduler_bp.route("/tick", methods=["POST"])
def scheduler_tick():
    """
    Main entry point for GCP Cloud Scheduler.
    Should be triggered every 1 minute.
    """
    ok_cfg, cfg_reason = scheduler_secret_acceptable_for_environment()
    if not ok_cfg:
        logger.error("Scheduler tick rejected (misconfiguration): %s", cfg_reason)
        return jsonify({"success": False, "error": cfg_reason}), 503

    # 1. Security Check
    if not _verify_scheduler_token():
        logger.warning(f"Unauthorized access attempt to scheduler tick endpoint from {request.remote_addr}")
        return jsonify({"success": False, "error": "Unauthorized"}), 401
        
    logger.info("[SCHEDULER TICK] Starting periodic sweep...")
    
    results = {}
    
    # 2. Check for newly scheduled campaigns that need to be started
    try:
        activated_campaigns = check_scheduled_campaigns()
        results["activated_campaigns"] = len(activated_campaigns)
    except Exception as e:
        logger.error(f"Error checking scheduled campaigns during tick: {e}")
        results["activated_campaigns_error"] = str(e)
        
    # 3. Process any drip enrollments that are due for their next step
    try:
        drip_stats = process_due_drip_enrollments()
        results["drip_enrollments"] = drip_stats
    except Exception as e:
        logger.error(f"Error processing due enrollments during tick: {e}")
        results["drip_enrollments_error"] = str(e)

    # 4. Advisory: mark expired Embedded Signup onboarding sessions (no disconnects)
    try:
        from whatsapp.onboarding_session_manager import sweep_expired_sessions

        n_exp = sweep_expired_sessions()
        results["onboarding_sessions_expired"] = n_exp
    except Exception as e:
        logger.warning("onboarding sweep during scheduler tick: %s", e)
        results["onboarding_sessions_expired_error"] = str(e)

    # 5. Warmup completions (lifecycle → active when window ends)
    try:
        from whatsapp.warmup_scheduler import tick_warmup_completions

        scanned, updated = tick_warmup_completions()
        results["warmup_completions_scanned"] = scanned
        results["warmup_completions_updated"] = updated
    except Exception as e:
        logger.warning("warmup scheduler tick: %s", e)
        results["warmup_completions_error"] = str(e)
        
    # 6. Safe Mode Engine Evaluations
    try:
        from whatsapp.safe_mode_engine import run_safe_mode_evaluations
        
        safe_mode_stats = run_safe_mode_evaluations(limit=100)
        results["safe_mode_evaluations"] = safe_mode_stats
    except Exception as e:
        logger.warning("safe mode engine tick: %s", e)
        results["safe_mode_evaluations_error"] = str(e)

    # 7. Auto-sync Google Sheet drip campaigns (enroll newly added rows)
    try:
        from whatsapp.drip_models import WhatsAppDripCampaign
        from whatsapp.drip_routes import sync_sheet_campaign_internal

        sheet_campaigns = WhatsAppDripCampaign.query.filter(
            WhatsAppDripCampaign.trigger_type == "google_sheet_row",
            WhatsAppDripCampaign.status == "active",
            WhatsAppDripCampaign.sheet_id.isnot(None),
        ).all()

        sheet_sync_stats = {
            "campaigns": len(sheet_campaigns),
            "synced": 0,
            "enrolled": 0,
            "errors": 0,
        }
        for campaign in sheet_campaigns:
            try:
                sync_result = sync_sheet_campaign_internal(campaign.id)
                if sync_result.get("success"):
                    sheet_sync_stats["synced"] += 1
                    sheet_sync_stats["enrolled"] += sync_result.get("enrolled", 0) or 0
                else:
                    sheet_sync_stats["errors"] += 1
                    logger.warning(
                        "Sheet sync failed for campaign %s: %s",
                        campaign.id,
                        sync_result.get("error"),
                    )
            except Exception as inner_e:
                sheet_sync_stats["errors"] += 1
                logger.warning("Sheet sync raised for campaign %s: %s", campaign.id, inner_e)

        results["sheet_syncs"] = sheet_sync_stats
    except Exception as e:
        logger.warning("sheet sync tick: %s", e)
        results["sheet_syncs_error"] = str(e)

    logger.info(f"[SCHEDULER TICK] Completed. Results: {results}")
    
    return jsonify({
        "success": True,
        "message": "Tick processed successfully",
        "results": results
    }), 200


@scheduler_bp.route("/webhook-integrity", methods=["POST"])
def scheduler_webhook_integrity():
    """
    Periodic WhatsApp webhook subscription + callback integrity sweep.
    Intended for Cloud Scheduler every ~30 minutes (separate job from /tick).
    """
    ok_cfg, cfg_reason = scheduler_secret_acceptable_for_environment()
    if not ok_cfg:
        logger.error("Webhook integrity sweep rejected (misconfiguration): %s", cfg_reason)
        return jsonify({"success": False, "error": cfg_reason}), 503

    if not _verify_scheduler_token():
        logger.warning(
            "Unauthorized webhook-integrity sweep attempt from %s", request.remote_addr
        )
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    from whatsapp.webhook_health import run_webhook_integrity_sweep

    body = request.get_json(silent=True) or {}
    try:
        limit = int(request.args.get("limit", body.get("limit", 100)))
    except (TypeError, ValueError):
        limit = 100
    probe_raw = request.args.get("probe_callback", body.get("probe_callback", "1"))
    probe = str(probe_raw).strip().lower() not in ("0", "false", "no")
    stats = run_webhook_integrity_sweep(limit=limit, only_active=True, probe_callback=probe)
    logger.info("[SCHEDULER WEBHOOK-INTEGRITY] completed stats=%s", stats)
    return jsonify({"success": True, "results": stats}), 200


@scheduler_bp.route("/trust-snapshots-daily", methods=["POST"])
def scheduler_trust_snapshots_daily():
    """
    Daily trust / reputation + meta app health snapshots (advisory).
    Intended for Cloud Scheduler once per day (separate from /tick).
    """
    ok_cfg, cfg_reason = scheduler_secret_acceptable_for_environment()
    if not ok_cfg:
        logger.error("Trust snapshots job rejected (misconfiguration): %s", cfg_reason)
        return jsonify({"success": False, "error": cfg_reason}), 503

    if not _verify_scheduler_token():
        logger.warning("Unauthorized trust-snapshots-daily attempt from %s", request.remote_addr)
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    from whatsapp.trust_snapshot_scheduler import run_daily_trust_snapshots

    body = request.get_json(silent=True) or {}
    try:
        limit = int(request.args.get("limit", body.get("limit", 500)))
    except (TypeError, ValueError):
        limit = 500
    force_raw = request.args.get("force", body.get("force", "0"))
    force = str(force_raw).strip().lower() in ("1", "true", "yes")
    include_inactive = str(body.get("include_inactive") or request.args.get("include_inactive") or "").lower() in (
        "1",
        "true",
        "yes",
    )
    stats = run_daily_trust_snapshots(limit=limit, include_inactive=include_inactive, force=force)
    logger.info("[SCHEDULER TRUST-SNAPSHOTS-DAILY] completed stats=%s", stats)
    return jsonify({"success": True, "results": stats}), 200


@scheduler_bp.route("/health", methods=["GET"])
def scheduler_health():
    """
    Health check endpoint for the Cloud Scheduler.
    Verifies DB connection, Redis connection, and secret key configurations.
    """
    from datetime import datetime, timezone
    from core.db import db
    from core.deployment_safety import scheduler_secret_acceptable_for_environment, is_non_dev_environment
    from whatsapp.drip_models import WhatsAppDripCampaign, WhatsAppDripEnrollment
    from sqlalchemy import text
    
    ok_cfg, cfg_reason = scheduler_secret_acceptable_for_environment()
    is_prod = is_non_dev_environment()
    
    db_ok = False
    db_error = None
    try:
        db.session.execute(text("SELECT 1")).scalar()
        db_ok = True
    except Exception as e:
        db_error = str(e)
        
    redis_ok = False
    redis_error = None
    try:
        from core.cache import get_redis_client
        rc = get_redis_client()
        if rc:
            rc.ping()
            redis_ok = True
    except Exception as e:
        redis_error = str(e)
        
    metrics = {}
    if db_ok:
        try:
            running_campaigns = WhatsAppDripCampaign.query.filter_by(status="running").count()
            scheduled_campaigns = WhatsAppDripCampaign.query.filter_by(status="scheduled").count()
            due_enrollments = WhatsAppDripEnrollment.query.filter(
                WhatsAppDripEnrollment.status == "active",
                WhatsAppDripEnrollment.next_run_at <= datetime.now(timezone.utc)
            ).count()
            
            metrics = {
                "running_campaigns": running_campaigns,
                "scheduled_campaigns": scheduled_campaigns,
                "due_enrollments": due_enrollments
            }
        except Exception as e:
            metrics["error"] = str(e)
            
    status_code = 200
    if not db_ok or (is_prod and not ok_cfg):
        status_code = 503
        
    return jsonify({
        "success": db_ok,
        "status": "healthy" if (db_ok and ok_cfg) else "degraded",
        "is_production": is_prod,
        "configuration": {
            "scheduler_secret_token_valid": ok_cfg,
            "scheduler_secret_reason": cfg_reason
        },
        "database": {
            "connected": db_ok,
            "error": db_error
        },
        "redis": {
            "connected": redis_ok,
            "error": redis_error
        },
        "metrics": metrics
    }), status_code
