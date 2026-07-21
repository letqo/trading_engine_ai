"""add index on prediction.news_headline for dedup lookups

Revision ID: c1a4e8f7b2d9
Revises: 9f2c7a4b1e3d
Create Date: 2026-07-21 12:00:00.000000
"""

from __future__ import annotations

from alembic import op


revision = 'c1a4e8f7b2d9'
down_revision = '9f2c7a4b1e3d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # predict-loop now checks this column every cycle (registry.
    # headline_already_predicted) before spending a Claude call on a
    # headline it's already analyzed -- see JOURNAL.md 2026-07-21.
    op.create_index(op.f('ix_prediction_news_headline'), 'prediction', ['news_headline'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_prediction_news_headline'), table_name='prediction')
