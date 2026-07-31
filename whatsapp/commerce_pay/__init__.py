# whatsapp/commerce_pay/__init__.py
"""
Commerce payments (PayU) — per-business in-chat payment links.
==============================================================
SELF-CONTAINED / REMOVABLE feature. Currently gated to the internal SocioChat
tenant (T0000) only — white-label tenants do not see it.

Each business (workspace) connects its OWN PayU merchant credentials, so its
customers' money settles to that business. This module holds ONLY the
credential-connect layer for now; link generation + webhook come next.

To remove later: unregister `commerce_pay_bp` in app.py, drop the
`workspace_payment_configs` table, and delete this folder + the frontend
Payments tab.
"""

from .routes import commerce_pay_bp
from .schema import ensure_commerce_pay_schema

__all__ = ["commerce_pay_bp", "ensure_commerce_pay_schema"]
