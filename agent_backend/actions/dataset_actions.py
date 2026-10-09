"""
Dataset Actions — list and manage contact datasets for bulk/drip campaigns.
"""

import logging
from typing import Dict, Any, List

from agent_backend.action_registry import BaseAction, action_registry

logger = logging.getLogger(__name__)


@action_registry.register(domain="dataset", action="list")
class ListDatasetsAction(BaseAction):
    name = "list_datasets"
    description = "List contact datasets"
    required_params: List[str] = []

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.drip_models import WhatsAppDataset
        workspace_id = context.get("workspace_id")

        datasets = WhatsAppDataset.query.filter_by(
            workspace_id=str(workspace_id)
        ).order_by(WhatsAppDataset.created_at.desc()).limit(20).all()

        if not datasets:
            return {"status": "success", "message": "No datasets found. Upload a CSV on the **Datasets** page to get started.", "data": []}

        items = []
        lines = []
        for i, d in enumerate(datasets, 1):
            item = d.to_dict()
            items.append(item)
            lines.append(f"{i}. 📋 **{d.name}** — {d.total_rows or 0} contacts ({d.source_type})")

        msg = f"**Datasets ({len(items)}):**\n" + "\n".join(lines)
        return {"status": "success", "message": msg, "data": items, "ui_action": "show_list"}


@action_registry.register(domain="dataset", action="get_info")
class GetDatasetInfoAction(BaseAction):
    name = "get_dataset_info"
    description = "Get details of a specific dataset"
    required_params = ["dataset_id"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        return "Which dataset? Provide the dataset ID."

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.drip_models import WhatsAppDataset
        workspace_id = context.get("workspace_id")

        ds = WhatsAppDataset.query.filter_by(
            id=params["dataset_id"], workspace_id=str(workspace_id)
        ).first()
        if not ds:
            return {"status": "error", "message": "Dataset not found."}

        columns = ", ".join(ds.columns) if ds.columns else "none defined"
        msg = (
            f"📋 **{ds.name}**\n"
            f"• Contacts: {ds.total_rows or 0}\n"
            f"• Columns: {columns}\n"
            f"• Source: {ds.source_type}\n"
            f"• Status: {ds.sync_status or 'synced'}"
        )
        return {"status": "success", "message": msg, "data": ds.to_dict(), "ui_action": "show_card"}


@action_registry.register(domain="dataset", action="delete")
class DeleteDatasetAction(BaseAction):
    name = "delete_dataset"
    description = "Delete a contact dataset"
    required_params = ["dataset_id"]

    def get_missing_params_prompt(self, missing: List[str]) -> str:
        return "Which dataset would you like to delete? Provide the dataset ID."

    def execute(self, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        from whatsapp.drip_models import WhatsAppDataset
        from models import db

        session = context.get("session")
        if not (session and getattr(session, "_confirmed", False)):
            return {"status": "confirmation_required", "message": f"Are you sure you want to delete dataset #{params['dataset_id']}? This cannot be undone."}

        workspace_id = context.get("workspace_id")
        ds = WhatsAppDataset.query.filter_by(
            id=params["dataset_id"], workspace_id=str(workspace_id)
        ).first()
        if not ds:
            return {"status": "error", "message": "Dataset not found."}

        name = ds.name
        db.session.delete(ds)
        db.session.commit()
        return {"status": "success", "message": f"🗑️ Dataset **{name}** deleted."}
