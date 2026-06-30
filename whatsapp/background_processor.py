"""
Background Processor for WhatsApp Webhook
==========================================

Bounded ThreadPoolExecutor for running webhook processing tasks
asynchronously after the fast path returns 200.

Uses concurrent.futures.ThreadPoolExecutor — safe with Gunicorn gthread workers.
Flask app context is propagated so background tasks can access the DB.

Usage:
    from whatsapp.background_processor import bg_processor

    bg_processor.submit(process_automation, account_id=1, message_text="hi")
"""

import atexit
import logging
import os
from concurrent.futures import ThreadPoolExecutor, Future
from threading import BoundedSemaphore
from typing import Callable, Any, Optional

from flask import current_app

logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────
MAX_WORKERS = 5           # Bounded pool — prevents thread explosion
TASK_NAME = "webhook_bg"  # Prefix for log messages
MAX_WORKERS = int(os.getenv("WHATSAPP_BG_MAX_WORKERS", str(MAX_WORKERS)))
MAX_PENDING_TASKS = int(os.getenv("WHATSAPP_BG_MAX_PENDING", "200"))
# ─────────────────────────────────────────────────────────────


class BackgroundProcessor:
    """
    Bounded thread-pool executor with Flask app-context propagation.

    • submit() captures the current Flask app and pushes a new app context
      inside the worker thread so that db.session / current_app work.
    • Errors in background tasks are logged, never propagated.
    • atexit hook ensures clean shutdown on worker recycle.
    """

    def __init__(self, max_workers: int = MAX_WORKERS, max_pending_tasks: int = MAX_PENDING_TASKS):
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=TASK_NAME,
        )
        self._slots = BoundedSemaphore(max_workers + max_pending_tasks)
        self._active = True
        atexit.register(self.shutdown)

    # ── Public API ───────────────────────────────────────────

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Optional[Future]:
        """
        Submit *fn* to the background thread pool.

        Automatically wraps *fn* in a Flask app context so that
        db.session, current_app, etc. are available inside the worker.

        Returns the Future (for testing / optional chaining) or None if
        the pool is shut down.
        """
        if not self._active:
            logger.warning("[bg] Pool is shut down — task dropped")
            return None

        if not self._slots.acquire(blocking=False):
            logger.error("[bg] Queue saturated - task rejected")
            return None

        # Capture the Flask app *now* (in the request thread).
        try:
            app = current_app._get_current_object()
        except RuntimeError:
            self._slots.release()
            logger.error("[bg] No Flask app context — cannot submit background task")
            return None

        def _wrapper():
            """Run fn inside a fresh app context."""
            try:
                with app.app_context():
                    fn(*args, **kwargs)
            except Exception:
                # CRITICAL: background errors must never crash the worker
                logger.exception("[bg] Background task failed (non-fatal)")

        try:
            future = self._pool.submit(_wrapper)
        except Exception:
            self._slots.release()
            raise
        future.add_done_callback(self._on_done)
        return future

    # ── Lifecycle ────────────────────────────────────────────

    def shutdown(self, wait: bool = False):
        """Gracefully shut down the pool (called on worker recycle)."""
        if not self._active:
            return
        self._active = False
        try:
            self._pool.shutdown(wait=wait, cancel_futures=True)
            logger.info("[bg] Background processor shut down")
        except Exception:
            pass

    # ── Internals ────────────────────────────────────────────

    @staticmethod
    def _on_done(future: Future):
        """Log unhandled exceptions from futures."""
        try:
            exc = future.exception()
            if exc:
                logger.error("[bg] Task raised: %s", exc, exc_info=exc)
        finally:
            try:
                bg_processor._slots.release()
            except Exception:
                pass


# ── Module-level singleton ───────────────────────────────────
# Shared across all requests in the same Gunicorn worker process.
bg_processor = BackgroundProcessor()
