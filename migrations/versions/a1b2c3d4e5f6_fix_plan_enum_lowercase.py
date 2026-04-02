"""fix plan enum to use all-lowercase values

Revision ID: a1b2c3d4e5f6
Revises: f1a2b3c4d5e6
Create Date: 2026-04-02

"""
from alembic import op
import sqlalchemy as sa

revision = 'a1b2c3d4e5f6'
down_revision = 'f1a2b3c4d5e6'
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if connection.dialect.name != 'postgresql':
        return

    # 1. Convert existing uppercase values to lowercase using a temp text column
    op.execute("ALTER TABLE subscriptions ALTER COLUMN plan TYPE TEXT")

    op.execute("UPDATE subscriptions SET plan = 'free'  WHERE plan = 'FREE'")
    op.execute("UPDATE subscriptions SET plan = 'pro'   WHERE plan = 'PRO'")
    op.execute("UPDATE subscriptions SET plan = 'basic' WHERE plan = 'BASIC'")
    op.execute("UPDATE subscriptions SET plan = 'elite' WHERE plan = 'ELITE'")

    # 2. Drop the old enum type
    op.execute("DROP TYPE IF EXISTS plan")

    # 3. Create the new enum type with all-lowercase values
    op.execute("CREATE TYPE plan AS ENUM ('free', 'basic', 'pro', 'elite')")

    # 4. Restore the column type back to the enum
    op.execute(
        "ALTER TABLE subscriptions "
        "ALTER COLUMN plan TYPE plan USING plan::plan"
    )


def downgrade():
    connection = op.get_bind()
    if connection.dialect.name != 'postgresql':
        return

    op.execute("ALTER TABLE subscriptions ALTER COLUMN plan TYPE TEXT")

    op.execute("UPDATE subscriptions SET plan = 'FREE'  WHERE plan = 'free'")
    op.execute("UPDATE subscriptions SET plan = 'PRO'   WHERE plan = 'pro'")
    op.execute("UPDATE subscriptions SET plan = 'BASIC' WHERE plan = 'basic'")
    op.execute("UPDATE subscriptions SET plan = 'ELITE' WHERE plan = 'elite'")

    op.execute("DROP TYPE IF EXISTS plan")
    op.execute("CREATE TYPE plan AS ENUM ('FREE', 'PRO', 'basic', 'elite')")

    op.execute(
        "ALTER TABLE subscriptions "
        "ALTER COLUMN plan TYPE plan USING plan::plan"
    )
