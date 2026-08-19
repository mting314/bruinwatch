"""Initial BruinWatch schema.

Replaces the previous MongoDB document store. Normalized catalog plus an
append-only enrollment_data time series, modelled on hotseat.io's schema.

Revision ID: 0001
Revises:
Create Date: 2026-08-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "terms",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(8), nullable=False),
        sa.Column("name", sa.String(64), nullable=False, server_default=""),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("catalog_synced_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_terms_code", "terms", ["code"], unique=True)

    op.create_table(
        "subject_areas",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
    )
    op.create_index("ix_subject_areas_code", "subject_areas", ["code"], unique=True)

    op.create_table(
        "courses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "subject_area_id", sa.Integer(), sa.ForeignKey("subject_areas.id"), nullable=False
        ),
        sa.Column("number", sa.String(16), nullable=False),
        sa.Column("title", sa.String(256), nullable=False, server_default=""),
        sa.Column("description", sa.Text()),
        sa.Column("units", sa.String(16)),
        sa.UniqueConstraint("subject_area_id", "number", name="uq_course"),
    )
    op.create_index("ix_courses_subject_area_id", "courses", ["subject_area_id"])

    op.create_table(
        "course_terms",
        sa.Column("course_id", sa.Integer(), sa.ForeignKey("courses.id"), primary_key=True),
        sa.Column("term_id", sa.Integer(), sa.ForeignKey("terms.id"), primary_key=True),
        sa.Column(
            "section_indices",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{%}",
        ),
    )

    op.create_table(
        "sections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("registrar_id", sa.String(16), nullable=False),
        sa.Column("term_id", sa.Integer(), sa.ForeignKey("terms.id"), nullable=False),
        sa.Column("course_id", sa.Integer(), sa.ForeignKey("courses.id"), nullable=False),
        sa.Column("section_label", sa.String(32), nullable=False, server_default=""),
        sa.Column("format", sa.String(16), nullable=False, server_default=""),
        sa.Column("index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("days", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("times", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("locations", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column(
            "instructors", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"
        ),
        sa.Column("units", sa.String(16), nullable=False, server_default=""),
        sa.Column("enrollment_status", sa.String(32), nullable=False, server_default="Unknown"),
        sa.Column("enrollment_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enrollment_capacity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("waitlist_status", sa.String(32), nullable=False, server_default="Unknown"),
        sa.Column("waitlist_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("waitlist_capacity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("website", sa.Text()),
        sa.Column("final_start", sa.DateTime(timezone=True)),
        sa.Column("final_end", sa.DateTime(timezone=True)),
        sa.Column("summer_session", sa.String(8)),
        sa.Column("summer_duration_weeks", sa.Integer()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # The natural key the scraper upserts on.
        sa.UniqueConstraint("registrar_id", "term_id", name="uq_section"),
    )
    op.create_index("ix_sections_registrar_id", "sections", ["registrar_id"])
    op.create_index("ix_sections_term_id", "sections", ["term_id"])
    op.create_index("ix_sections_course_id", "sections", ["course_id"])
    op.create_index("ix_sections_course_term", "sections", ["course_id", "term_id"])

    op.create_table(
        "enrollment_data",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "section_id",
            sa.Integer(),
            sa.ForeignKey("sections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("enrollment_status", sa.String(32), nullable=False),
        sa.Column("enrollment_count", sa.Integer(), nullable=False),
        sa.Column("enrollment_capacity", sa.Integer(), nullable=False),
        sa.Column("waitlist_status", sa.String(32), nullable=False),
        sa.Column("waitlist_count", sa.Integer(), nullable=False),
        sa.Column("waitlist_capacity", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # The access pattern is always "this section, in time order".
    op.create_index(
        "ix_enrollment_data_section_created", "enrollment_data", ["section_id", "created_at"]
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("discord_id", sa.BigInteger(), nullable=False),
        sa.Column("dm_greeted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("default_term_id", sa.Integer(), sa.ForeignKey("terms.id")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_users_discord_id", "users", ["discord_id"], unique=True)

    op.create_table(
        "subscriptions",
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column(
            "section_id",
            sa.Integer(),
            sa.ForeignKey("sections.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("notify", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notify_below_spots", sa.Integer()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # The poller reads DISTINCT section_id from here every tick.
    op.create_index("ix_subscriptions_section_id", "subscriptions", ["section_id"])

    op.create_table(
        "aliases",
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("alias", sa.String(32), primary_key=True),
        sa.Column("target", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "section_id",
            sa.Integer(),
            sa.ForeignKey("sections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("previous_status", sa.String(32), nullable=False),
        sa.Column("new_status", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False, server_default="status_change"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    # Partial index: the drain only ever looks for unsent rows, and the table
    # is append-only, so this stays small no matter how much history piles up.
    op.create_index(
        "ix_outbox_unsent",
        "notification_outbox",
        ["created_at"],
        postgresql_where=sa.text("sent_at IS NULL"),
    )

    op.create_table(
        "backfill_progress",
        sa.Column("term_code", sa.String(8), primary_key=True),
        sa.Column("subject_area_code", sa.String(16), primary_key=True),
        sa.Column("courses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sections", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "completed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "enrollment_appointments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("term_id", sa.Integer(), sa.ForeignKey("terms.id"), nullable=False),
        sa.Column("pass_name", sa.String(32), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("term_id", "pass_name", "start_at", name="uq_appointment"),
    )
    op.create_index("ix_enrollment_appointments_term_id", "enrollment_appointments", ["term_id"])


def downgrade() -> None:
    for table in (
        "enrollment_appointments",
        "backfill_progress",
        "notification_outbox",
        "aliases",
        "subscriptions",
        "users",
        "enrollment_data",
        "sections",
        "course_terms",
        "courses",
        "subject_areas",
        "terms",
    ):
        op.drop_table(table)
