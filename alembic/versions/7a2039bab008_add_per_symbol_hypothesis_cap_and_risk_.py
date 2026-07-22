"""add per-symbol hypothesis cap and risk_gate_config table

Revision ID: 7a2039bab008
Revises: a722f3ec8a05
Create Date: 2026-07-22 18:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '7a2039bab008'
down_revision = 'a722f3ec8a05'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'anticipatory_loop_config',
        sa.Column('max_open_hypotheses_per_symbol', sa.Integer(), nullable=False, server_default='2'),
    )
    op.create_table(
        'risk_gate_config',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('use_defaults', sa.Boolean(), nullable=False),
        sa.Column('max_capital_per_position_pct', sa.Float(), nullable=False),
        sa.Column('max_total_exposure_pct', sa.Float(), nullable=False),
        sa.Column('stop_loss_pct', sa.Float(), nullable=False),
        sa.Column('max_daily_drawdown_pct', sa.Float(), nullable=False),
        sa.Column('max_consecutive_losses_per_day', sa.Integer(), nullable=False),
        sa.Column('allow_overnight_positions', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('risk_gate_config')
    op.drop_column('anticipatory_loop_config', 'max_open_hypotheses_per_symbol')
