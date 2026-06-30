import os
import sys
import logging
import threading
import signal
import time

# Ensure the app directory is in the path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from runtime import app, prepare_worker_runtime
from core.queue.manager import get_queue_backend, start_worker

logger = logging.getLogger("worker_main")

# Prepare the runtime (DB only)
prepare_worker_runtime()

# Global stop event for graceful shutdown
stop_event = threading.Event()

def handle_sigterm(signum, frame):
    logger.info("Received SIGTERM/SIGINT, initiating graceful shutdown...")
    stop_event.set()

# Register signal handlers for graceful shutdown (Cloud Run sends SIGTERM)
signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)


def start_dummy_http_server():
    from flask import Flask
    import logging
    # Suppress Flask logs for the dummy server
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    
    dummy_app = Flask(__name__)
    
    @dummy_app.route("/")
    @dummy_app.route("/health")
    def health():
        return "Worker is healthy", 200
        
    port = int(os.getenv("PORT", 8080))
    logger.info(f"Starting dummy HTTP server on port {port} for Cloud Run health checks")
    dummy_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

def main():
    logger.info("Starting WhatsApp Queue Worker...")
    
    # Start dummy HTTP server in a background thread
    http_thread = threading.Thread(target=start_dummy_http_server, daemon=True)
    http_thread.start()
    
    with app.app_context():
        backend = get_queue_backend()
        if backend != "redis":
            logger.info(
                "Queue backend is %s — worker idle (webhooks run inline on whatsapp-api).",
                backend,
            )
            while not stop_event.is_set():
                time.sleep(1.0)
        else:
            # Start the worker loop. It will block until stop_event is set.
            start_worker(stop_event=stop_event)
        
    logger.info("WhatsApp Queue Worker stopped gracefully.")


if __name__ == "__main__":
    main()
