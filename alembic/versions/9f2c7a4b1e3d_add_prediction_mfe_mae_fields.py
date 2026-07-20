"""add prediction mfe_pct/mae_pct fields

Revision ID: 9f2c7a4b1e3d
Revises: 6b0f3a85b65d
Create Date: 2026-07-21 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '9f2c7a4b1e3d'
down_revision = '6b0f3a85b65d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable, no backfill value: existing resolved rows were scored
    # before this field existed and their intermediate bars were never
    # kept, so there's no honest excursion value to backfill -- NULL means
    # "not available," not zero.
    op.add_column('prediction', sa.Column('mfe_pct', sa.Float(), nullable=True))
    op.add_column('prediction', sa.Column('mae_pct', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('prediction', 'mae_pct')
    op.drop_column('prediction', 'mfe_pct')
