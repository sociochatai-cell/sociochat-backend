"""
Template Rewriter - AI-Powered Template Optimization
====================================================

Uses Gemini to rewrite templates while preserving:
- All placeholders ({{1}}, {{2}}, etc.)
- Original intent
- Category compliance

Includes "Explain Why" mode for transparency.
"""

import re
import os
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
import google.generativeai as genai


@dataclass
class RewriteResult:
    """Result of template rewrite."""
    rewritten_body: str
    changes_made: List[str] = field(default_factory=list)
    preserved_variables: List[str] = field(default_factory=list)
    intent_warning: Optional[str] = None
    success: bool = True
    error: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "rewritten_body": self.rewritten_body,
            "changes_made": self.changes_made,
            "preserved_variables": self.preserved_variables,
            "intent_warning": self.intent_warning,
            "success": self.success,
            "error": self.error,
        }


class RewriteMode:
    """Available rewrite modes."""
    NEUTRAL_UTILITY = "neutral_utility"  # Remove promotional tone
    CLEAR_MARKETING = "clear_marketing"  # Add appropriate CTAs
    STRICT_AUTHENTICATION = "strict_authentication"  # OTP-only format


class TemplateRewriter:
    """
    AI-powered template rewriter using Gemini.
    
    Safety Rules:
    - ✅ Preserve {{1}}, {{2}} placeholders exactly
    - ✅ No markdown (**, _, *)
    - ✅ Rewrite tone only, never change intent
    - ❌ Never convert Marketing → Utility if promotional
    """
    
    def __init__(self, workspace_id: Optional[str] = None):
        """Initialize Gemini client.

        When a ``workspace_id`` is supplied, the tenant's own Gemini API key is
        used (so the tenant is billed on their own AI quota). The resolver falls
        back to the global env key, so for T0000 / unset / no-workspace this is
        byte-identical to reading GEMINI_API_KEY directly.
        """
        # Lazy import keeps the tenant package out of the import-time graph.
        from tenant.integration import get_tenant_ai_config
        cfg = get_tenant_ai_config(workspace_id=workspace_id)
        api_key = cfg.gemini_api_key
        if api_key:
            genai.configure(api_key=api_key)
            self.model = genai.GenerativeModel("gemini-3.1-flash-lite")
        else:
            self.model = None
    
    def rewrite(
        self,
        body: str,
        target_category: str,
        mode: str = RewriteMode.NEUTRAL_UTILITY,
        current_category: Optional[str] = None,
    ) -> RewriteResult:
        """
        Rewrite template body for target category.
        
        Args:
            body: Original template body
            target_category: Target category (UTILITY, MARKETING, AUTHENTICATION)
            mode: Rewrite mode
            current_category: Current/detected category (for intent warnings)
        
        Returns:
            RewriteResult with rewritten body and changes explanation
        """
        if not self.model:
            return RewriteResult(
                rewritten_body=body,
                success=False,
                error="AI service not configured. Set GEMINI_API_KEY environment variable."
            )
        
        # Extract variables to preserve
        variables = re.findall(r'\{\{\d+\}\}', body)
        preserved_variables = list(set(variables))
        
        # Check for intent mismatch that can't be fixed
        if current_category == "MARKETING" and target_category == "UTILITY":
            # Analyze if this is truly promotional
            promo_keywords = ["sale", "discount", "offer", "deal", "buy now", "limited time"]
            has_promo = any(k in body.lower() for k in promo_keywords)
            if has_promo:
                return RewriteResult(
                    rewritten_body=body,
                    intent_warning=(
                        "This content contains promotional intent that cannot be safely "
                        "converted to Utility. Utility templates must be user-triggered "
                        "and non-promotional. Consider keeping it as Marketing."
                    ),
                    success=False,
                    error="Intent mismatch - promotional content cannot become Utility"
                )
        
        # Build the prompt
        prompt = self._build_prompt(body, target_category, mode, preserved_variables)
        
        try:
            response = self.model.generate_content(prompt)
            result_text = response.text.strip()
            
            # Parse the response
            rewritten_body, changes_made = self._parse_response(result_text, body)
            
            # Verify variables are preserved
            new_variables = re.findall(r'\{\{\d+\}\}', rewritten_body)
            if set(new_variables) != set(preserved_variables):
                # Variables were modified - restore original
                return RewriteResult(
                    rewritten_body=body,
                    preserved_variables=preserved_variables,
                    success=False,
                    error="AI modified variables. Original preserved for safety."
                )
            
            # Remove any markdown that AI might have added
            rewritten_body = self._remove_markdown(rewritten_body)
            
            return RewriteResult(
                rewritten_body=rewritten_body,
                changes_made=changes_made,
                preserved_variables=preserved_variables,
                success=True,
            )
            
        except Exception as e:
            return RewriteResult(
                rewritten_body=body,
                preserved_variables=preserved_variables,
                success=False,
                error=f"AI rewrite failed: {str(e)}"
            )
    
    def _build_prompt(
        self,
        body: str,
        target_category: str,
        mode: str,
        variables: List[str],
    ) -> str:
        """Build the prompt for Gemini."""
        
        category_guidelines = {
            "UTILITY": """
UTILITY templates must:
- Be transactional/operational only (order updates, confirmations, reminders)
- Have neutral, professional tone
- NO promotional language (no "sale", "offer", "discount", "buy now")
- NO emojis
- NO call-to-action marketing verbs
""",
            "MARKETING": """
MARKETING templates should:
- Have clear, engaging promotional content
- Include appropriate call-to-action
- Can use emojis sparingly (1-3)
- Be compelling but not spammy
""",
            "AUTHENTICATION": """
AUTHENTICATION templates must:
- Focus ONLY on verification/OTP
- Be extremely concise
- NO promotional content
- NO emojis
- Format: "[Brand] Your code is {{1}}. Valid for X minutes."
""",
        }
        
        mode_instructions = {
            RewriteMode.NEUTRAL_UTILITY: "Rewrite to remove ALL promotional language while preserving the core informational content.",
            RewriteMode.CLEAR_MARKETING: "Rewrite to be more engaging with a clear call-to-action.",
            RewriteMode.STRICT_AUTHENTICATION: "Rewrite to focus ONLY on the verification code, removing any extra content.",
        }
        
        prompt = f"""You are a WhatsApp template optimization expert. Rewrite this template for {target_category} category.

ORIGINAL TEMPLATE:
{body}

CATEGORY REQUIREMENTS:
{category_guidelines.get(target_category, "")}

REWRITE INSTRUCTION:
{mode_instructions.get(mode, mode_instructions[RewriteMode.NEUTRAL_UTILITY])}

CRITICAL RULES:
1. PRESERVE ALL VARIABLES EXACTLY: {', '.join(variables) if variables else 'None'}
2. DO NOT add any markdown formatting (**, __, ~~, *, _)
3. Keep the same language as the original
4. Preserve the core informational content
5. Do NOT change the fundamental purpose/intent

RESPOND IN THIS EXACT FORMAT:
REWRITTEN:
[Your rewritten template here]

CHANGES:
- [Change 1]
- [Change 2]
- [Change 3]
"""
        return prompt
    
    def _parse_response(self, response: str, original: str) -> tuple:
        """Parse Gemini response into rewritten body and changes."""
        changes_made = []
        rewritten_body = original  # Default to original
        
        # Extract rewritten section
        if "REWRITTEN:" in response:
            parts = response.split("REWRITTEN:")
            if len(parts) > 1:
                remainder = parts[1]
                if "CHANGES:" in remainder:
                    rewritten_body = remainder.split("CHANGES:")[0].strip()
                else:
                    rewritten_body = remainder.strip()
        
        # Extract changes section
        if "CHANGES:" in response:
            changes_section = response.split("CHANGES:")[1].strip()
            # Parse bullet points
            for line in changes_section.split("\n"):
                line = line.strip()
                if line.startswith("-") or line.startswith("•"):
                    change = line.lstrip("-•").strip()
                    if change:
                        changes_made.append(change)
        
        return rewritten_body, changes_made
    
    def _remove_markdown(self, text: str) -> str:
        """Remove any markdown formatting from text."""
        # Remove bold
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
        text = re.sub(r'__(.+?)__', r'\1', text)
        # Remove italic
        text = re.sub(r'\*(.+?)\*', r'\1', text)
        text = re.sub(r'_(.+?)_', r'\1', text)
        # Remove strikethrough
        text = re.sub(r'~~(.+?)~~', r'\1', text)
        return text


# ==============================================================
# Convenience Function for Routes
# ==============================================================

def rewrite_template(
    body: str,
    target_category: str,
    mode: str = RewriteMode.NEUTRAL_UTILITY,
    current_category: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Rewrite a template and return results as dict.

    Convenience wrapper for API routes.

    Pass ``workspace_id`` to bill the tenant's own Gemini key; when omitted the
    resolver falls back to the global env key (byte-identical to before).
    """
    rewriter = TemplateRewriter(workspace_id=workspace_id)
    result = rewriter.rewrite(
        body=body,
        target_category=target_category,
        mode=mode,
        current_category=current_category,
    )
    return result.to_dict()
