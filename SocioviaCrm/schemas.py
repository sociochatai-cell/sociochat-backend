# crm_management/schemas.py
from flask import current_app

def get_ma():
    ma = getattr(current_app, "ma", None)
    if ma is None:
        raise RuntimeError("app.ma (Marshmallow) not found on current_app")
    return ma

def init_schemas():
    # Flask-Marshmallow is optional: the live CRM routes (leads/contacts/deals/
    # tasks/dashboard/settings/webhook) serialize by hand and never read
    # current_app.crm_schemas. When app.ma is absent, degrade to a safe no-op so
    # the blueprint can register without pulling in marshmallow.
    ma = getattr(current_app, "ma", None)
    if ma is None:
        current_app.crm_schemas = {}
        return

    models = getattr(current_app, "crm_models", None)
    if models is None:
        raise RuntimeError("crm_models not initialized; call init_models first")

    Lead = models["Lead"]
    Contact = models["Contact"]
    Task = models["Task"]
    Activity = models["Activity"]
    Campaign = models["Campaign"]
    Setting = models["Setting"]

    class LeadSchema(ma.SQLAlchemyAutoSchema):
        class Meta:
            model = Lead
            load_instance = True
            include_fk = True

    class ContactSchema(ma.SQLAlchemyAutoSchema):
        class Meta:
            model = Contact
            load_instance = True

    class TaskSchema(ma.SQLAlchemyAutoSchema):
        class Meta:
            model = Task
            load_instance = True

    class ActivitySchema(ma.SQLAlchemyAutoSchema):
        class Meta:
            model = Activity
            load_instance = True

    class CampaignSchema(ma.SQLAlchemyAutoSchema):
        class Meta:
            model = Campaign
            load_instance = True

    class SettingSchema(ma.SQLAlchemyAutoSchema):
        class Meta:
            model = Setting
            load_instance = True

    current_app.crm_schemas = {
        "lead": LeadSchema(),
        "leads": LeadSchema(many=True),
        "contact": ContactSchema(),
        "contacts": ContactSchema(many=True),
        "task": TaskSchema(),
        "tasks": TaskSchema(many=True),
        "activity": ActivitySchema(),
        "activities": ActivitySchema(many=True),
        "campaigns": CampaignSchema(many=True),
        "setting": SettingSchema(),
    }
