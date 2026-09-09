"""API endpoints for AI Conversation Insights.

GET  /api/whatsapp/conversations/<id>/insights  — fetch insights for a chat
"""

import logging
from flask import Blueprint, jsonify, request

from .conversation_insights_models import ConversationInsight

logger = logging.getLogger(__name__)

insights_bp = Blueprint("conversation_insights", __name__, url_prefix="/api/whatsapp")


def _get_workspace_id():
    """Extract workspace_id from the request headers (same pattern as other WA routes)."""
    ws = request.headers.get("X-Workspace-Id") or request.args.get("workspace_id")
    if ws:
        try:
            return int(ws)
        except (ValueError, TypeError):
            pass
    return None


@insights_bp.route("/conversations/<int:conversation_id>/insights", methods=["GET"])
def get_conversation_insights(conversation_id: int):
    """Return the AI-collected insights for a specific conversation."""
    ws = _get_workspace_id()
    if not ws:
        return jsonify({"error": "workspace_id required"}), 400

    insight = (
        ConversationInsight.query
        .filter_by(conversation_id=conversation_id, workspace_id=ws)
        .order_by(ConversationInsight.updated_at.desc())
        .first()
    )
    if not insight:
        return jsonify({"insight": None}), 200

    return jsonify({"insight": insight.to_dict()}), 200
