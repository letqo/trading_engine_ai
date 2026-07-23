"""add manual_trade table

Revision ID: c961b0ff84e1
Revises: 4dd240b2722e
Create Date: 2026-07-23 11:26:44.379717
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'c961b0ff84e1'
down_revision = '4dd240b2722e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'manual_trade',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('submitted_by', sa.String(), nullable=False),
        sa.Column('symbol', sa.String(), nullable=False),
        sa.Column('side', sa.String(), nullable=False),
        sa.Column('requested_quantity', sa.Float(), nullable=False),
        sa.Column('note', sa.String(), nullable=True),
        sa.Column('traded_order_id', sa.String(), nullable=True),
        sa.Column('traded_quantity', sa.Float(), nullable=True),
        sa.Column('exit_order_id', sa.String(), nullable=True),
        sa.Column('trade_rejected', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('trade_rejection_reason', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_manual_trade_created_at'), 'manual_trade', ['created_at'], unique=False)
    op.create_index(op.f('ix_manual_trade_symbol'), 'manual_trade', ['symbol'], unique=False)
    op.create_index(op.f('ix_manual_trade_traded_order_id'), 'manual_trade', ['traded_order_id'], unique=False)
    op.create_index(op.f('ix_manual_trade_trade_rejected'), 'manual_trade', ['trade_rejected'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_manual_trade_trade_rejected'), table_name='manual_trade')
    op.drop_index(op.f('ix_manual_trade_traded_order_id'), table_name='manual_trade')
    op.drop_index(op.f('ix_manual_trade_symbol'), table_name='manual_trade')
    op.drop_index(op.f('ix_manual_trade_created_at'), table_name='manual_trade')
    op.drop_table('manual_trade')
