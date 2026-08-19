"""Widen sections.units.

Real catalog data exceeds the original 16 characters: variable-unit courses
carry strings like ``"4.0/6.0 Alternate"`` (17) alongside ``"2.0-4.0 Variable"``
(exactly 16). A backfill of Fall 2023 aborted on the first one.

A survey of ~280 courses across two terms put every other bounded string column
comfortably inside its limit -- longest course title 147/256, subject area name
54/128, section label 6/32 -- so only this column moves.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "sections",
        "units",
        existing_type=sa.String(16),
        type_=sa.String(32),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Lossy: any value longer than 16 characters would be rejected. Truncate
    # first so the downgrade cannot fail on real data.
    op.execute("UPDATE sections SET units = LEFT(units, 16)")
    op.alter_column(
        "sections",
        "units",
        existing_type=sa.String(32),
        type_=sa.String(16),
        existing_nullable=False,
    )
