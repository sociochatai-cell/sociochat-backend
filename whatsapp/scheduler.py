import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.pool import ThreadPoolExecutor
from pytz import utc

logger = logging.getLogger(__name__)

# Global scheduler instance
scheduler = None
# Store Flask app reference for app context in background jobs
_flask_app = None

def init_scheduler(app):
    global scheduler, _flask_app
    
    # Store app reference for background job context
    _flask_app = app
    
    if scheduler and scheduler.running:
        return scheduler

    from models import db

    jobstores = {
        'default': SQLAlchemyJobStore(engine=db.engine, tablename='whatsapp_jobs')
    }
    executors = {
        'default': ThreadPoolExecutor(20)
    }
    job_defaults = {
        'coalesce': True,          # Merge missed runs into one (prevent duplicates)
        'max_instances': 1,        # Only one instance of each campaign job at a time
        'misfire_grace_time': 60   # Allow 60 seconds grace time
    }

    scheduler = BackgroundScheduler(jobstores=jobstores, executors=executors, job_defaults=job_defaults, timezone=utc)
    scheduler.start()
    logger.info("WhatsApp APScheduler started with app context support")
    return scheduler


def execute_campaign_job(campaign_id):
    """
    Execute a campaign job with proper Flask app context.
    This is the actual function that APScheduler will call.
    """
    logger.info(f"[SCHEDULER] execute_campaign_job called for campaign {campaign_id}")
    
    if _flask_app is None:
        logger.error("[SCHEDULER] Flask app not initialized - cannot execute campaign job")
        return
    
    try:
        with _flask_app.app_context():
            logger.info(f"[SCHEDULER] Inside app context for campaign {campaign_id}")
            # Import here to avoid circular imports and ensure fresh module state
            from .drip_engine import trigger_campaign_now
            trigger_campaign_now(campaign_id)
            logger.info(f"[SCHEDULER] Completed campaign {campaign_id}")
    except Exception as e:
        logger.exception(f"[SCHEDULER] Error executing campaign {campaign_id}: {e}")


def add_campaign_job(campaign_id, run_date, func=None):
    """
    Schedule a campaign processing job at a specific time.
    The func parameter is ignored - we always use execute_campaign_job for reliability.
    """
    if not scheduler:
        logger.error("Scheduler not initialized")
        return None
        
    job_id = f"campaign_{campaign_id}"
    
    # Remove existing job if any (for rescheduling)
    try:
        scheduler.remove_job(job_id)
        logger.info(f"Removed existing job {job_id}")
    except Exception:
        pass
    
    # Use our dedicated job function (not passing func as arg - pickle issues)
    job = scheduler.add_job(
        execute_campaign_job,  # Direct function reference, not wrapper
        'date',
        run_date=run_date,
        args=[campaign_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=60  # 60 seconds grace time
    )
    logger.info(f"[SCHEDULER] Scheduled job {job_id} for {run_date}")
    return job.id
