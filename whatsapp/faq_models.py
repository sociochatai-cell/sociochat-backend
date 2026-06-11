"""
WhatsApp FAQ Knowledge Base Models
===================================

Database models for FAQ (Frequently Asked Questions) management.

Features:
- Question/answer pairs with keywords
- Automatic keyword extraction
- Match counting and analytics
- Multi-tenant isolation by workspace/account
"""

from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from models import db
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import JSON as SAJSON


class WhatsAppFAQ(db.Model):
    """
    FAQ entry for automatic question answering.
    
    FAQs are matched before AI fallback, providing fast
    responses for common questions without API calls.
    """
    __tablename__ = "whatsapp_faqs"
    
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.String(36), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey("whatsapp_accounts.id"), nullable=False, index=True)
    
    # Question and answer
    question = db.Column(db.Text, nullable=False)
    answer = db.Column(db.Text, nullable=False)
    
    # Keywords for matching (auto-generated or manual)
    keywords = db.Column(SAJSON().with_variant(JSONB, "postgresql"), nullable=False, default=list)
    
    # Optional category for organization
    category = db.Column(db.String(64), nullable=True)
    
    # Matching settings
    match_type = db.Column(db.String(32), nullable=False, default="keywords")  # keywords, exact, contains
    match_threshold = db.Column(db.Float, nullable=False, default=0.3)  # For keyword matching (0-1), lowered from 0.6
    
    # Status
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    priority = db.Column(db.Integer, nullable=False, default=100)  # Lower = higher priority
    
    # Analytics
    match_count = db.Column(db.Integer, nullable=False, default=0)
    last_matched_at = db.Column(db.DateTime(timezone=True), nullable=True)
    
    # Timestamps
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )
    
    # Relationships
    account = db.relationship("WhatsAppAccount", backref="faqs")
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize FAQ to dictionary."""
        return {
            "id": self.id,
            "account_id": self.account_id,
            "question": self.question,
            "answer": self.answer,
            "keywords": self.keywords or [],
            "category": self.category,
            "match_type": self.match_type,
            "match_threshold": self.match_threshold,
            "is_active": self.is_active,
            "priority": self.priority,
            "match_count": self.match_count,
            "last_matched_at": self.last_matched_at.isoformat() + "Z" if self.last_matched_at else None,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
        }
    
    def increment_match_count(self):
        """Increment match counter and update last matched timestamp."""
        self.match_count = (self.match_count or 0) + 1
        self.last_matched_at = datetime.now(timezone.utc)


# ============================================================
# FAQ Matching Logic
# ============================================================

def extract_keywords_from_question(question: str) -> List[str]:
    """
    Extract keywords from a question for matching.
    
    Simple implementation - can be enhanced with NLP or Gemini.
    """
    import re
    
    # Common stop words to filter out
    stop_words = {
        'a', 'an', 'the', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'must', 'shall', 'can', 'need', 'dare',
        'ought', 'used', 'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by',
        'from', 'up', 'about', 'into', 'through', 'during', 'before', 'after',
        'above', 'below', 'between', 'under', 'again', 'further', 'then',
        'once', 'here', 'there', 'when', 'where', 'why', 'how', 'all', 'each',
        'few', 'more', 'most', 'other', 'some', 'such', 'no', 'nor', 'not',
        'only', 'own', 'same', 'so', 'than', 'too', 'very', 's', 't', 'just',
        'don', 'now', 'what', 'which', 'who', 'whom', 'this', 'that', 'these',
        'those', 'am', 'or', 'and', 'but', 'if', 'because', 'as', 'until',
        'while', 'it', 'its', 'i', 'me', 'my', 'myself', 'we', 'our', 'ours',
        'you', 'your', 'yours', 'he', 'him', 'his', 'she', 'her', 'hers',
        'they', 'them', 'their', 'theirs', 'hi', 'hello', 'hey', 'please',
        'thanks', 'thank', 'okay', 'ok', 'yes', 'no', 'yeah', 'yep', 'nope'
    }
    
    # Clean and tokenize
    text = question.lower()
    text = re.sub(r'[^\w\s]', ' ', text)  # Remove punctuation
    words = text.split()
    
    # Filter keywords
    keywords = []
    for word in words:
        word = word.strip()
        if word and len(word) >= 2 and word not in stop_words:
            keywords.append(word)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_keywords = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            unique_keywords.append(kw)
    
    return unique_keywords


def calculate_keyword_match_score(message: str, faq_keywords: List[str]) -> float:
    """
    Calculate match score between message and FAQ keywords.
    
    Returns:
        Score between 0 and 1
    """
    if not faq_keywords:
        return 0.0
    
    message_keywords = set(extract_keywords_from_question(message))
    faq_keywords_set = set(kw.lower() for kw in faq_keywords)
    
    if not message_keywords:
        return 0.0
    
    # Calculate overlap
    matches = message_keywords.intersection(faq_keywords_set)
    
    # Score is based on how many FAQ keywords were matched
    # (not based on message keywords - we want to match FAQs that cover the message)
    score = len(matches) / len(faq_keywords_set)
    
    return score


def find_matching_faq(
    workspace_id: str,
    account_id: int,
    message: str,
    min_score: float = 0.5
) -> Optional[WhatsAppFAQ]:
    """
    Find the best matching FAQ for a message.
    
    Args:
        workspace_id: Workspace ID
        account_id: Account ID
        message: Incoming message text
        min_score: Minimum match score (0-1)
        
    Returns:
        Best matching FAQ or None
    """
    # Get active FAQs for this account
    faqs = WhatsAppFAQ.query.filter_by(
        workspace_id=workspace_id,
        account_id=account_id,
        is_active=True
    ).order_by(WhatsAppFAQ.priority.asc()).all()
    
    if not faqs:
        return None
    
    message_lower = message.lower().strip()
    best_match = None
    best_score = 0.0
    
    for faq in faqs:
        score = 0.0
        
        if faq.match_type == "exact":
            # Exact match (case-insensitive)
            if message_lower == faq.question.lower().strip():
                score = 1.0
        
        elif faq.match_type == "contains":
            # Message contains the question text
            if faq.question.lower().strip() in message_lower:
                score = 0.9
        
        else:  # keywords (default)
            # Keyword-based matching
            keywords = faq.keywords or []
            if keywords:
                score = calculate_keyword_match_score(message, keywords)
        
        # Use FAQ's threshold if set
        threshold = faq.match_threshold if faq.match_threshold else min_score
        
        if score >= threshold and score > best_score:
            best_score = score
            best_match = faq
    
    return best_match
