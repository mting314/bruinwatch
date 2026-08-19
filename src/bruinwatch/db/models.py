"""SQLAlchemy models.

Schema shape follows hotseat.io's: a normalized catalog
(``subject_areas -> courses -> sections``) plus an append-only
``enrollment_data`` time series keyed on ``(section_id, created_at)``.

Two tables carry most of the design weight:

``subscriptions``
    Many users to many sections. The poller reads *distinct* section IDs from
    here, so polling cost scales with the number of watched sections, not with
    subscribers x sections as it did when each user's watchlist was fetched
    independently.

``notification_outbox``
    Change events are written transactionally alongside the section update and
    delivered by a separate drain job, so a crash mid-fan-out can neither drop
    nor duplicate a DM.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now() -> Mapped[dt.datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Term(Base):
    __tablename__ = "terms"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    #: Index in the registrar's dropdown, 0 = newest. Term codes do not sort
    #: chronologically as strings (within a year the suffixes run W, S, F but
    #: sort F, S, W), so ordering by `code` is wrong. Always order by this.
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: The term the registrar itself has selected.
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Whether the poller should spend requests on this term at all.
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    catalog_synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _now()


class SubjectArea(Base):
    __tablename__ = "subject_areas"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))

    courses: Mapped[list[Course]] = relationship(back_populates="subject_area")


class Course(Base):
    __tablename__ = "courses"
    __table_args__ = (UniqueConstraint("subject_area_id", "number", name="uq_course"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    subject_area_id: Mapped[int] = mapped_column(ForeignKey("subject_areas.id"), index=True)
    number: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(256), default="")
    description: Mapped[str | None] = mapped_column(Text)
    units: Mapped[str | None] = mapped_column(String(16))

    subject_area: Mapped[SubjectArea] = relationship(back_populates="courses")


class CourseTerm(Base):
    """Which courses are offered in which term, and under which listing indices."""

    __tablename__ = "course_terms"

    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), primary_key=True)
    term_id: Mapped[int] = mapped_column(ForeignKey("terms.id"), primary_key=True)
    section_indices: Mapped[list[str]] = mapped_column(ARRAY(String), default=lambda: ["%"])


class Section(Base):
    __tablename__ = "sections"
    __table_args__ = (
        UniqueConstraint("registrar_id", "term_id", name="uq_section"),
        Index("ix_sections_course_term", "course_id", "term_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    registrar_id: Mapped[str] = mapped_column(String(16), index=True)
    term_id: Mapped[int] = mapped_column(ForeignKey("terms.id"), index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)

    section_label: Mapped[str] = mapped_column(String(32), default="")
    format: Mapped[str] = mapped_column(String(16), default="")
    index: Mapped[int] = mapped_column(Integer, default=0)
    days: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    times: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    locations: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    instructors: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    #: Wider than it looks like it needs: variable-unit courses carry values
    #: like "4.0/6.0 Alternate" (17 chars). See migration 0002.
    units: Mapped[str] = mapped_column(String(32), default="")

    enrollment_status: Mapped[str] = mapped_column(String(32), default="Unknown")
    enrollment_count: Mapped[int] = mapped_column(Integer, default=0)
    enrollment_capacity: Mapped[int] = mapped_column(Integer, default=0)
    waitlist_status: Mapped[str] = mapped_column(String(32), default="Unknown")
    waitlist_count: Mapped[int] = mapped_column(Integer, default=0)
    waitlist_capacity: Mapped[int] = mapped_column(Integer, default=0)

    website: Mapped[str | None] = mapped_column(Text)
    final_start: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    final_end: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    summer_session: Mapped[str | None] = mapped_column(String(8))
    summer_duration_weeks: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[dt.datetime] = _now()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    course: Mapped[Course] = relationship()
    term: Mapped[Term] = relationship()


class EnrollmentDatum(Base):
    """One observation of a section's enrollment. Append-only."""

    __tablename__ = "enrollment_data"
    __table_args__ = (Index("ix_enrollment_data_section_created", "section_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    section_id: Mapped[int] = mapped_column(ForeignKey("sections.id", ondelete="CASCADE"))

    enrollment_status: Mapped[str] = mapped_column(String(32))
    enrollment_count: Mapped[int] = mapped_column(Integer)
    enrollment_capacity: Mapped[int] = mapped_column(Integer)
    waitlist_status: Mapped[str] = mapped_column(String(32))
    waitlist_count: Mapped[int] = mapped_column(Integer)
    waitlist_capacity: Mapped[int] = mapped_column(Integer)

    created_at: Mapped[dt.datetime] = _now()


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    #: Replaces scanning the user's entire DM history on every command.
    dm_greeted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    default_term_id: Mapped[int | None] = mapped_column(ForeignKey("terms.id"))
    created_at: Mapped[dt.datetime] = _now()


class Subscription(Base):
    __tablename__ = "subscriptions"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    section_id: Mapped[int] = mapped_column(
        ForeignKey("sections.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    notify: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Also notify when spots left drops to or below this, not just on a status
    #: flip. NULL disables the threshold.
    notify_below_spots: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = _now()


class Alias(Base):
    """Per-user shorthand for a subject area, e.g. ``CS`` -> ``COM SCI``."""

    __tablename__ = "aliases"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    alias: Mapped[str] = mapped_column(String(32), primary_key=True)
    target: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[dt.datetime] = _now()


class NotificationOutbox(Base):
    """A pending "your class changed" DM.

    Written in the same transaction as the section update; drained separately.
    """

    __tablename__ = "notification_outbox"
    # Partial index: the drain only ever asks for unsent rows, so this stays
    # small however much delivered history accumulates.
    __table_args__ = (
        Index(
            "ix_outbox_unsent",
            "created_at",
            postgresql_where=text("sent_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    section_id: Mapped[int] = mapped_column(ForeignKey("sections.id", ondelete="CASCADE"))
    previous_status: Mapped[str] = mapped_column(String(32))
    new_status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(32), default="status_change")
    created_at: Mapped[dt.datetime] = _now()
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class BackfillProgress(Base):
    """One completed (term, subject) unit of a historical backfill.

    A full backfill is hours of polite scraping and will be interrupted, so
    progress is recorded explicitly rather than inferred from whether rows
    happen to exist -- a subject can legitimately yield zero courses, and that
    is a completed unit, not a missing one.
    """

    __tablename__ = "backfill_progress"

    term_code: Mapped[str] = mapped_column(String(8), primary_key=True)
    subject_area_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    courses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sections: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_at: Mapped[dt.datetime] = _now()


class EnrollmentAppointment(Base):
    """A registrar enrollment pass window.

    Drives adaptive polling: sub-minute latency only matters while students can
    actually act on a notification.
    """

    __tablename__ = "enrollment_appointments"
    __table_args__ = (UniqueConstraint("term_id", "pass_name", "start_at", name="uq_appointment"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    term_id: Mapped[int] = mapped_column(ForeignKey("terms.id"), index=True)
    pass_name: Mapped[str] = mapped_column(String(32))
    start_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
