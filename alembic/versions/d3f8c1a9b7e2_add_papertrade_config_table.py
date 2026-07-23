"""add papertrade_config table

Revision ID: d3f8c1a9b7e2
Revises: ac44a74d20ed
Create Date: 2026-07-23 16:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'd3f8c1a9b7e2'
down_revision = 'ac44a74d20ed'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'papertrade_config',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('last_cycle_at', sa.DateTime(), nullable=True),
        sa.Column('strategy', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('papertrade_config')
