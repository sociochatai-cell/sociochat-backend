"""
Canonical database instance — re-exports the shared SQLAlchemy `db` from models.
"""

from models import db, Base  # noqa: F401
