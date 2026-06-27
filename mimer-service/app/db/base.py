"""Declarative base for all ORM models.

Importing this module (and `app.db.models`) registers every table on
`Base.metadata`, which Alembic and the test bootstrap both rely on.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""
