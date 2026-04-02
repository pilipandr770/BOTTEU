"""add basic and elite subscription plans

Revision ID: f1a2b3c4d5e6
Revises: efc6725b73e7
Create Date: 2026-04-02

"""
from alembic import op
import sqlalchemy as sa


revision = 'f1a2b3c4d5e6'
down_revision = 'efc6725b73e7'
branch_labels = None
depends_on = None


def upgrade():
    # PostgreSQL: add new enum values to the existing plan type
    # SQLite (dev): ALTER TABLE not needed since SQLite uses TEXT for enums
    connection = op.get_bind()
    if connection.dialect.name == 'postgresql':
        op.execute("ALTER TYPE plan ADD VALUE IF NOT EXISTS 'basic'")
        op.execute("ALTER TYPE plan ADD VALUE IF NOT EXISTS 'elite'")


def downgrade():
    # PostgreSQL does not support removing enum values without recreating the type.
    # Safe to leave as-is; unused values cause no harm.
    pass
