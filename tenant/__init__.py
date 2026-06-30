"""
Tenant package — multi-tenant white-label layer for SocioChat.

Exposes the Super Admin and Tenant Admin blueprints plus the idempotent
startup migration entry point.
"""

from tenant.superadmin_routes import superadmin_bp
from tenant.portal_routes import tenant_bp
from tenant.migrations import run_tenant_migrations

__all__ = ["superadmin_bp", "tenant_bp", "run_tenant_migrations"]
