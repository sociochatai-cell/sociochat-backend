"""
WhatsApp FAQ Routes
===================

API endpoints for FAQ knowledge base management.

Endpoints:
- GET    /api/whatsapp/accounts/{id}/faqs           - List FAQs
- POST   /api/whatsapp/accounts/{id}/faqs           - Create FAQ
- GET    /api/whatsapp/accounts/{id}/faqs/{faq_id}  - Get FAQ
- PUT    /api/whatsapp/accounts/{id}/faqs/{faq_id}  - Update FAQ
- DELETE /api/whatsapp/accounts/{id}/faqs/{faq_id}  - Delete FAQ
- POST   /api/whatsapp/accounts/{id}/faqs/bulk      - Bulk import
- POST   /api/whatsapp/accounts/{id}/faqs/test      - Test FAQ matching
"""

import logging
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request
from functools import wraps

from shared_models import db
from .models import WhatsAppAccount
from .faq_models import (
    WhatsAppFAQ,
    extract_keywords_from_question,
    find_matching_faq,
)

logger = logging.getLogger(__name__)

# Blueprint for FAQ routes
faq_bp = Blueprint('whatsapp_faq', __name__, url_prefix='/api/whatsapp/accounts')


# ============================================================
# Decorators
# ============================================================

def require_account_access(f):
    """
    Decorator to verify account access and inject account + workspace_id.
    Matches the pattern used in automation_routes.py.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        account_id = kwargs.get("account_id")

        if not account_id:
            return jsonify({"error": "Account ID required"}), 400

        # Enforce authenticated ownership of the account's workspace (fail closed).
        from tenant.context import resolve_owned_account
        account, err = resolve_owned_account(account_id)
        if err:
            return err

        kwargs["account"] = account
        kwargs["workspace_id"] = account.workspace_id

        return f(*args, **kwargs)

    return decorated_function


# ============================================================
# FAQ CRUD Endpoints
# ============================================================

@faq_bp.route('/<int:account_id>/faqs', methods=['GET'])
@require_account_access
def list_faqs(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    List all FAQs for an account.
    
    Query params:
        - category: Filter by category
        - is_active: Filter by active status
        - search: Search in questions
    """
    try:
        query = WhatsAppFAQ.query.filter_by(
            workspace_id=workspace_id,
            account_id=account_id
        )
        
        # Apply filters
        category = request.args.get("category")
        if category:
            query = query.filter_by(category=category)
        
        is_active = request.args.get("is_active")
        if is_active is not None:
            query = query.filter_by(is_active=is_active.lower() == "true")
        
        search = request.args.get("search")
        if search:
            query = query.filter(
                WhatsAppFAQ.question.ilike(f"%{search}%")
            )
        
        # Order by priority then created_at
        faqs = query.order_by(
            WhatsAppFAQ.priority.asc(),
            WhatsAppFAQ.created_at.desc()
        ).all()
        
        return jsonify({
            "success": True,
            "faqs": [faq.to_dict() for faq in faqs],
            "count": len(faqs)
        })
        
    except Exception as e:
        logger.exception(f"Error listing FAQs: {e}")
        return jsonify({"error": "Failed to list FAQs"}), 500


@faq_bp.route('/<int:account_id>/faqs', methods=['POST'])
@require_account_access
def create_faq(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    Create a new FAQ.
    
    Request body:
    {
        "question": "What are your business hours?",
        "answer": "We're open Monday-Friday, 9am-6pm.",
        "keywords": ["hours", "open", "business"],  // Optional, auto-generated if not provided
        "category": "General",  // Optional
        "match_type": "keywords",  // keywords, exact, contains
        "match_threshold": 0.6  // Optional
    }
    """
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "Request body required"}), 400
        
        question = data.get("question", "").strip()
        answer = data.get("answer", "").strip()
        
        if not question or not answer:
            return jsonify({"error": "Question and answer are required"}), 400
        
        # Auto-extract keywords if not provided
        keywords = data.get("keywords")
        if not keywords:
            keywords = extract_keywords_from_question(question)
        
        faq = WhatsAppFAQ(
            workspace_id=workspace_id,
            account_id=account_id,
            question=question,
            answer=answer,
            keywords=keywords,
            category=data.get("category"),
            match_type=data.get("match_type", "keywords"),
            match_threshold=data.get("match_threshold", 0.6),
            is_active=data.get("is_active", True),
            priority=data.get("priority", 100),
        )
        
        db.session.add(faq)
        db.session.commit()
        
        logger.info(f"Created FAQ {faq.id} for account {account_id}")
        
        return jsonify({
            "success": True,
            "faq": faq.to_dict(),
            "message": "FAQ created successfully"
        }), 201
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error creating FAQ: {e}")
        return jsonify({"error": "Failed to create FAQ"}), 500


@faq_bp.route('/<int:account_id>/faqs/<int:faq_id>', methods=['GET'])
@require_account_access
def get_faq(account_id: int, faq_id: int, account: WhatsAppAccount, workspace_id: str):
    """Get a specific FAQ."""
    try:
        faq = WhatsAppFAQ.query.filter_by(
            id=faq_id,
            workspace_id=workspace_id,
            account_id=account_id
        ).first()
        
        if not faq:
            return jsonify({"error": "FAQ not found"}), 404
        
        return jsonify({
            "success": True,
            "faq": faq.to_dict()
        })
        
    except Exception as e:
        logger.exception(f"Error getting FAQ: {e}")
        return jsonify({"error": "Failed to get FAQ"}), 500


@faq_bp.route('/<int:account_id>/faqs/<int:faq_id>', methods=['PUT'])
@require_account_access
def update_faq(account_id: int, faq_id: int, account: WhatsAppAccount, workspace_id: str):
    """Update an existing FAQ."""
    try:
        faq = WhatsAppFAQ.query.filter_by(
            id=faq_id,
            workspace_id=workspace_id,
            account_id=account_id
        ).first()
        
        if not faq:
            return jsonify({"error": "FAQ not found"}), 404
        
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body required"}), 400
        
        # Update allowed fields
        if "question" in data:
            faq.question = data["question"].strip()
            # Re-extract keywords if question changed and no keywords provided
            if "keywords" not in data:
                faq.keywords = extract_keywords_from_question(faq.question)
        
        if "answer" in data:
            faq.answer = data["answer"].strip()
        
        if "keywords" in data:
            faq.keywords = data["keywords"]
        
        if "category" in data:
            faq.category = data["category"]
        
        if "match_type" in data:
            faq.match_type = data["match_type"]
        
        if "match_threshold" in data:
            faq.match_threshold = data["match_threshold"]
        
        if "is_active" in data:
            faq.is_active = data["is_active"]
        
        if "priority" in data:
            faq.priority = data["priority"]
        
        faq.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        
        logger.info(f"Updated FAQ {faq.id}")
        
        return jsonify({
            "success": True,
            "faq": faq.to_dict(),
            "message": "FAQ updated successfully"
        })
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error updating FAQ: {e}")
        return jsonify({"error": "Failed to update FAQ"}), 500


@faq_bp.route('/<int:account_id>/faqs/<int:faq_id>', methods=['DELETE'])
@require_account_access
def delete_faq(account_id: int, faq_id: int, account: WhatsAppAccount, workspace_id: str):
    """Delete a FAQ."""
    try:
        faq = WhatsAppFAQ.query.filter_by(
            id=faq_id,
            workspace_id=workspace_id,
            account_id=account_id
        ).first()
        
        if not faq:
            return jsonify({"error": "FAQ not found"}), 404
        
        db.session.delete(faq)
        db.session.commit()
        
        logger.info(f"Deleted FAQ {faq_id}")
        
        return jsonify({
            "success": True,
            "message": "FAQ deleted successfully"
        })
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error deleting FAQ: {e}")
        return jsonify({"error": "Failed to delete FAQ"}), 500


# ============================================================
# Bulk Operations
# ============================================================

@faq_bp.route('/<int:account_id>/faqs/bulk', methods=['POST'])
@require_account_access
def bulk_import_faqs(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    Bulk import FAQs.
    
    Request body:
    {
        "faqs": [
            {"question": "...", "answer": "...", "category": "..."},
            {"question": "...", "answer": "..."}
        ],
        "overwrite": false  // If true, delete existing FAQs first
    }
    """
    try:
        data = request.get_json()
        
        if not data or not data.get("faqs"):
            return jsonify({"error": "FAQs array required"}), 400
        
        faqs_data = data["faqs"]
        overwrite = data.get("overwrite", False)
        
        if overwrite:
            # Delete existing FAQs
            WhatsAppFAQ.query.filter_by(
                workspace_id=workspace_id,
                account_id=account_id
            ).delete()
        
        created = 0
        errors = []
        
        for i, faq_data in enumerate(faqs_data):
            question = faq_data.get("question", "").strip()
            answer = faq_data.get("answer", "").strip()
            
            if not question or not answer:
                errors.append(f"Row {i+1}: Question and answer required")
                continue
            
            keywords = faq_data.get("keywords")
            if not keywords:
                keywords = extract_keywords_from_question(question)
            
            faq = WhatsAppFAQ(
                workspace_id=workspace_id,
                account_id=account_id,
                question=question,
                answer=answer,
                keywords=keywords,
                category=faq_data.get("category"),
                match_type=faq_data.get("match_type", "keywords"),
                is_active=True,
                priority=100 + i,  # Preserve order
            )
            db.session.add(faq)
            created += 1
        
        db.session.commit()
        
        logger.info(f"Bulk imported {created} FAQs for account {account_id}")
        
        return jsonify({
            "success": True,
            "created": created,
            "errors": errors,
            "message": f"Imported {created} FAQs"
        }), 201 if created > 0 else 400
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error bulk importing FAQs: {e}")
        return jsonify({"error": "Failed to import FAQs"}), 500


# ============================================================
# FAQ Testing
# ============================================================

@faq_bp.route('/<int:account_id>/faqs/test', methods=['POST'])
@require_account_access
def test_faq_matching(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    Test FAQ matching for a message.
    
    Request body:
    {
        "message": "What are your hours?"
    }
    """
    try:
        data = request.get_json()
        message = data.get("message", "").strip() if data else ""
        
        if not message:
            return jsonify({"error": "Message required"}), 400
        
        matched_faq = find_matching_faq(
            workspace_id=workspace_id,
            account_id=account_id,
            message=message
        )
        
        if matched_faq:
            return jsonify({
                "success": True,
                "matched": True,
                "faq": matched_faq.to_dict(),
                "answer": matched_faq.answer
            })
        else:
            return jsonify({
                "success": True,
                "matched": False,
                "faq": None,
                "answer": None,
                "message": "No matching FAQ found"
            })
        
    except Exception as e:
        logger.exception(f"Error testing FAQ: {e}")
        return jsonify({"error": "Failed to test FAQ matching"}), 500


# ============================================================
# FAQ Categories
# ============================================================

@faq_bp.route('/<int:account_id>/faqs/categories', methods=['GET'])
@require_account_access
def list_faq_categories(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """Get list of unique FAQ categories."""
    try:
        categories = db.session.query(WhatsAppFAQ.category).filter(
            WhatsAppFAQ.workspace_id == workspace_id,
            WhatsAppFAQ.account_id == account_id,
            WhatsAppFAQ.category.isnot(None)
        ).distinct().all()
        
        category_list = [c[0] for c in categories if c[0]]
        
        return jsonify({
            "success": True,
            "categories": category_list
        })
        
    except Exception as e:
        logger.exception(f"Error getting categories: {e}")
        return jsonify({"error": "Failed to get categories"}), 500


# ============================================================
# FAQ Rule Setup
# ============================================================

@faq_bp.route('/<int:account_id>/faqs/enable', methods=['POST'])
@require_account_access
def enable_faq_rule(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    Enable FAQ matching by creating/activating the FAQ automation rule.
    This must be called for FAQs to work in the webhook flow.
    """
    from .automation_models import WhatsAppAutomationRule
    
    try:
        # Check if FAQ rule exists
        faq_rule = WhatsAppAutomationRule.query.filter_by(
            workspace_id=workspace_id,
            account_id=account_id,
            rule_type="faq"
        ).first()
        
        if faq_rule:
            # Activate existing rule
            faq_rule.is_active = True
            faq_rule.status = "active"
        else:
            # Create new FAQ rule
            faq_rule = WhatsAppAutomationRule(
                workspace_id=workspace_id,
                account_id=account_id,
                name="FAQ Auto-Response",
                description="Automatically respond with matching FAQ answers",
                rule_type="faq",
                status="active",
                is_active=True,
                response_type="faq",
                response_config={},
                trigger_config={},
                priority=45,  # After keywords (40), before AI (50)
            )
            db.session.add(faq_rule)
        
        db.session.commit()
        
        return jsonify({
            "success": True,
            "message": "FAQ matching enabled",
            "rule_id": faq_rule.id
        })
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error enabling FAQ rule: {e}")
        return jsonify({"error": "Failed to enable FAQ matching"}), 500


@faq_bp.route('/<int:account_id>/faqs/status', methods=['GET'])
@require_account_access
def get_faq_status(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """Get FAQ status including rule activation and count."""
    from .automation_models import WhatsAppAutomationRule
    
    try:
        # Check if FAQ rule exists and is active
        faq_rule = WhatsAppAutomationRule.query.filter_by(
            workspace_id=workspace_id,
            account_id=account_id,
            rule_type="faq"
        ).first()
        
        faq_count = WhatsAppFAQ.query.filter_by(
            workspace_id=workspace_id,
            account_id=account_id,
            is_active=True
        ).count()
        
        return jsonify({
            "success": True,
            "rule_exists": faq_rule is not None,
            "rule_active": faq_rule.is_active if faq_rule else False,
            "faq_count": faq_count,
            "enabled": faq_rule is not None and faq_rule.is_active and faq_count > 0
        })
        
    except Exception as e:
        logger.exception(f"Error getting FAQ status: {e}")
        return jsonify({"error": "Failed to get FAQ status"}), 500

