from flask import Blueprint, current_app
from .routes.dashboard import bp as dashboard_bp
from .routes.leads import bp as leads_bp
from .routes.contacts import bp as contacts_bp
from .routes.tasks import bp as tasks_bp
from .routes.settings import bp as settings_bp
from .routes.webhook import bp as webhook_bp
from .models import init_models
from .schemas import init_schemas
from .routes.deals import bp as deals_bp

def create_crm_blueprint():
    """
    Initialize models/schemas (requires current_app.db and current_app.ma to exist).
    Returns the main blueprint mounted at /api
    """
    init_models()
    init_schemas()
    main = Blueprint("crm", __name__, url_prefix="/api")
    main.register_blueprint(dashboard_bp)
    main.register_blueprint(leads_bp)
    main.register_blueprint(contacts_bp)
    main.register_blueprint(tasks_bp)
    main.register_blueprint(settings_bp)
    main.register_blueprint(webhook_bp)
    main.register_blueprint(deals_bp)
    return main
