"""Migration chain sanity — renders the Alembic history without a database.

Walks the script directory (no DB connection) to assert the revisions form a
single linear chain from base to head. This does not exercise Postgres DDL; a
real upgrade against Postgres is run separately (see README / Migrations).
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.db.models import FundHolding, SecurityIdentifier
from app.services import capabilities as capabilities_service

_REPO_ROOT = Path(__file__).resolve().parent.parent

_HEAD = "0019"
_CHAIN = [
    "0019",
    "0018",
    "0017",
    "0016",
    "0015",
    "0014",
    "0013",
    "0012",
    "0011",
    "0010",
    "0009",
    "0008",
    "0007",
    "0006",
    "0005",
    "0004",
    "0003",
    "0002",
    "0001",
]


def _script_directory() -> ScriptDirectory:
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(cfg)


def test_migration_chain_is_linear_to_head() -> None:
    script = _script_directory()
    # walk_revisions yields head -> base.
    revisions = [rev.revision for rev in script.walk_revisions()]
    assert revisions == _CHAIN


def test_single_head() -> None:
    script = _script_directory()
    assert script.get_heads() == [_HEAD]


def test_each_revision_has_upgrade_and_downgrade() -> None:
    script = _script_directory()
    for rev in script.walk_revisions():
        module = rev.module
        assert hasattr(module, "upgrade")
        assert hasattr(module, "downgrade")


def test_capabilities_migration_head_matches_alembic_head() -> None:
    # The capabilities payload advertises the migration head; keep it honest.
    assert capabilities_service.MIGRATION_HEAD == _HEAD


def test_security_identifier_scheme_value_index_declared() -> None:
    """Regression guard for the previously-noted Alembic autogenerate drift.

    Migration 0003 creates ``ix_security_identifiers_scheme_value`` but the ORM
    model had not declared it, so every autogenerate wanted to drop it. The
    model now declares the index; this test fails if it is removed again.
    """
    index_names = {ix.name for ix in SecurityIdentifier.__table__.indexes}
    assert "ix_security_identifiers_scheme_value" in index_names


def test_fund_holding_identity_unique_constraint_declared() -> None:
    constraint_names = {c.name for c in FundHolding.__table__.constraints}
    assert "uq_fund_holding_identity" in constraint_names
