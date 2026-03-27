"""add ai_consultations table

Revision ID: b3a1f7c92d01
Revises: 0da70cea5aa9
Create Date: 2026-03-27 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b3a1f7c92d01'
down_revision = '0da70cea5aa9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('ai_consultations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('bot_id', sa.Integer(), nullable=True),
        sa.Column('symbol', sa.String(length=20), nullable=False),
        sa.Column('market_regime', sa.String(length=30), nullable=True),
        sa.Column('recommended_algorithm', sa.String(length=50), nullable=True),
        sa.Column('recommended_params', sa.JSON(), nullable=True),
        sa.Column('recommended_timeframe', sa.String(length=10), nullable=True),
        sa.Column('confidence_score', sa.Integer(), nullable=True),
        sa.Column('reasoning', sa.Text(), nullable=True),
        sa.Column('signal_matrix', sa.JSON(), nullable=True),
        sa.Column('backtest_results', sa.JSON(), nullable=True),
        sa.Column('applied', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('(CURRENT_TIMESTAMP)')),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['bot_id'], ['bots.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('ai_consultations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ai_consultations_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_ai_consultations_bot_id'), ['bot_id'], unique=False)


def downgrade():
    with op.batch_alter_table('ai_consultations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ai_consultations_bot_id'))
        batch_op.drop_index(batch_op.f('ix_ai_consultations_user_id'))
    op.drop_table('ai_consultations')
