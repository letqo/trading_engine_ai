"""add trade_rejected/trade_rejection_reason to prediction and hypothesis

Revision ID: 4dd240b2722e
Revises: 7a2039bab008
Create Date: 2026-07-23 09:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '4dd240b2722e'
down_revision = '7a2039bab008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('prediction', sa.Column('trade_rejected', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('prediction', sa.Column('trade_rejection_reason', sa.String(), nullable=True))
    op.create_index(op.f('ix_prediction_trade_rejected'), 'prediction', ['trade_rejected'])

    op.add_column('hypothesis', sa.Column('trade_rejected', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('hypothesis', sa.Column('trade_rejection_reason', sa.String(), nullable=True))
    op.create_index(op.f('ix_hypothesis_trade_rejected'), 'hypothesis', ['trade_rejected'])


def downgrade() -> None:
    op.drop_index(op.f('ix_hypothesis_trade_rejected'), table_name='hypothesis')
    op.drop_column('hypothesis', 'trade_rejection_reason')
    op.drop_column('hypothesis', 'trade_rejected')

    op.drop_index(op.f('ix_prediction_trade_rejected'), table_name='prediction')
    op.drop_column('prediction', 'trade_rejection_reason')
    op.drop_column('prediction', 'trade_rejected')
