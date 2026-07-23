"""add strategy_trade table

Revision ID: ac44a74d20ed
Revises: c961b0ff84e1
Create Date: 2026-07-23 14:34:15.183488
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'ac44a74d20ed'
down_revision = 'c961b0ff84e1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'strategy_trade',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('strategy_id', sa.String(), nullable=False),
        sa.Column('symbol', sa.String(), nullable=False),
        sa.Column('side', sa.String(), nullable=False),
        sa.Column('entry_order_id', sa.String(), nullable=False),
        sa.Column('entry_quantity', sa.Float(), nullable=False),
        sa.Column('entry_price', sa.Float(), nullable=False),
        sa.Column('reason', sa.String(), nullable=True),
        sa.Column('exit_order_id', sa.String(), nullable=True),
        sa.Column('exit_quantity', sa.Float(), nullable=True),
        sa.Column('exit_price', sa.Float(), nullable=True),
        sa.Column('exit_reason', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_strategy_trade_created_at'), 'strategy_trade', ['created_at'], unique=False)
    op.create_index(op.f('ix_strategy_trade_strategy_id'), 'strategy_trade', ['strategy_id'], unique=False)
    op.create_index(op.f('ix_strategy_trade_symbol'), 'strategy_trade', ['symbol'], unique=False)
    op.create_index(op.f('ix_strategy_trade_entry_order_id'), 'strategy_trade', ['entry_order_id'], unique=False)
    op.create_index(op.f('ix_strategy_trade_exit_order_id'), 'strategy_trade', ['exit_order_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_strategy_trade_exit_order_id'), table_name='strategy_trade')
    op.drop_index(op.f('ix_strategy_trade_entry_order_id'), table_name='strategy_trade')
    op.drop_index(op.f('ix_strategy_trade_symbol'), table_name='strategy_trade')
    op.drop_index(op.f('ix_strategy_trade_strategy_id'), table_name='strategy_trade')
    op.drop_index(op.f('ix_strategy_trade_created_at'), table_name='strategy_trade')
    op.drop_table('strategy_trade')
