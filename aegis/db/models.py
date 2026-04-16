"""SQLAlchemy ORM models — all tables from CLAUDE.md Section 3."""

from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# Helper for TIMESTAMPTZ columns
TSTZ = TIMESTAMP(timezone=True)


# ═══════════════════════════════════════════════════════════
# PEOPLE & ORG STRUCTURE
# ═══════════════════════════════════════════════════════════


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    responsibilities: Mapped[str | None] = mapped_column(Text)
    head_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", use_alter=True))
    parent_dept_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"))
    source: Mapped[str | None] = mapped_column(
        String, default="inferred",
    )
    confidence: Mapped[float | None] = mapped_column(Float, default=0.5)

    __table_args__ = (
        CheckConstraint("source IN ('inferred','manual','teams')", name="ck_departments_source"),
    )


class Person(Base):
    __tablename__ = "people"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[list[str] | None] = mapped_column(ARRAY(Text), default=list)
    title: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text, unique=True)
    org: Mapped[str | None] = mapped_column(Text)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"))
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"))
    seniority: Mapped[str | None] = mapped_column(String, default="unknown")
    is_external: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")
    last_seen: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")
    interaction_count: Mapped[int] = mapped_column(Integer, default=0)
    cc_gravity_score: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String, default="calendar")
    confidence: Mapped[float | None] = mapped_column(Float, default=0.5)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=True)
    llm_suggestion: Mapped[dict | None] = mapped_column(JSONB)
    embedding = mapped_column(Vector(1536))

    department = relationship("Department", foreign_keys=[department_id])
    manager = relationship("Person", remote_side="Person.id", foreign_keys=[manager_id])

    __table_args__ = (
        CheckConstraint(
            "seniority IN ('executive','senior','mid','junior','unknown')",
            name="ck_people_seniority",
        ),
        CheckConstraint(
            "source IN ('calendar','email','teams','meeting','manual','backfill')",
            name="ck_people_source",
        ),
        Index("idx_people_email", "email"),
        Index("idx_people_department", "department_id"),
    )


class PersonHistory(Base):
    __tablename__ = "people_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id"), nullable=False)
    field_changed: Mapped[str] = mapped_column(Text, nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")
    change_source: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            "change_source IN ('inferred','manual')", name="ck_people_history_source"
        ),
    )


# ═══════════════════════════════════════════════════════════
# WORKSTREAMS
# ═══════════════════════════════════════════════════════════


class Workstream(Base):
    __tablename__ = "workstreams"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String, default="active")
    created_by: Mapped[str | None] = mapped_column(String, default="manual")
    confidence: Mapped[float | None] = mapped_column(Float, default=1.0)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"))
    target_date: Mapped[date | None] = mapped_column(Date)
    is_managed: Mapped[bool] = mapped_column(Boolean, default=False)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_quiet_days: Mapped[int] = mapped_column(Integer, default=14)
    split_from_id: Mapped[int | None] = mapped_column(ForeignKey("workstreams.id"))
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("workstreams.id"))
    created: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")
    updated: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")
    embedding = mapped_column(Vector(1536))

    __table_args__ = (
        CheckConstraint(
            "status IN ('active','quiet','paused','completed','archived')",
            name="ck_workstreams_status",
        ),
        CheckConstraint(
            "created_by IN ('auto','manual')", name="ck_workstreams_created_by"
        ),
    )


class WorkstreamItem(Base):
    __tablename__ = "workstream_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    workstream_id: Mapped[int] = mapped_column(ForeignKey("workstreams.id"), nullable=False)
    item_type: Mapped[str] = mapped_column(String, nullable=False)
    item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    relevance_score: Mapped[float | None] = mapped_column(Float, default=1.0)
    linked_at: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")
    linked_by: Mapped[str | None] = mapped_column(String, default="auto")

    __table_args__ = (
        UniqueConstraint("workstream_id", "item_type", "item_id", name="uq_workstream_item"),
        CheckConstraint(
            "item_type IN ('meeting','email','chat_message','action_item',"
            "'decision','commitment','email_ask','chat_ask')",
            name="ck_workstream_items_type",
        ),
        CheckConstraint("linked_by IN ('auto','manual')", name="ck_workstream_items_linked_by"),
        Index("idx_workstream_items_ws", "workstream_id"),
        Index("idx_workstream_items_item", "item_type", "item_id"),
    )


class WorkstreamStakeholder(Base):
    __tablename__ = "workstream_stakeholders"

    workstream_id: Mapped[int] = mapped_column(
        ForeignKey("workstreams.id"), primary_key=True
    )
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id"), primary_key=True)
    role: Mapped[str | None] = mapped_column(String, default="contributor")

    __table_args__ = (
        CheckConstraint(
            "role IN ('owner','lead','contributor','informed')",
            name="ck_workstream_stakeholders_role",
        ),
    )


class WorkstreamMilestone(Base):
    __tablename__ = "workstream_milestones"

    id: Mapped[int] = mapped_column(primary_key=True)
    workstream_id: Mapped[int] = mapped_column(ForeignKey("workstreams.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    target_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str | None] = mapped_column(String, default="pending")
    created: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")
    updated: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','in_progress','completed')",
            name="ck_workstream_milestones_status",
        ),
    )


# ═══════════════════════════════════════════════════════════
# MEETINGS
# ═══════════════════════════════════════════════════════════


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    start_time: Mapped[datetime] = mapped_column(TSTZ, nullable=False)
    end_time: Mapped[datetime] = mapped_column(TSTZ, nullable=False)
    duration: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String, default="scheduled")
    transcript_status: Mapped[str | None] = mapped_column(String, default="pending")
    meeting_type: Mapped[str | None] = mapped_column(String, default="virtual")
    is_excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    calendar_event_id: Mapped[str | None] = mapped_column(Text, unique=True)
    online_meeting_url: Mapped[str | None] = mapped_column(Text)
    recurring_series_id: Mapped[str | None] = mapped_column(Text)
    instance_number: Mapped[int | None] = mapped_column(Integer)
    organizer_email: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    transcript_text: Mapped[str | None] = mapped_column(Text)
    screen_context: Mapped[dict | None] = mapped_column(JSONB)
    last_extracted_at: Mapped[datetime | None] = mapped_column(TSTZ)
    processing_status: Mapped[str | None] = mapped_column(String, default="pending")
    processing_error: Mapped[str | None] = mapped_column(Text)
    sentiment: Mapped[str | None] = mapped_column(String)
    embedding = mapped_column(Vector(1536))

    attendees = relationship("MeetingAttendee", back_populates="meeting")

    __table_args__ = (
        CheckConstraint(
            "status IN ('scheduled','in_progress','completed')", name="ck_meetings_status"
        ),
        CheckConstraint(
            "transcript_status IN ('pending','captured','partial','no_audio','processing','failed')",
            name="ck_meetings_transcript_status",
        ),
        CheckConstraint(
            "meeting_type IN ('virtual','in_person','hybrid','solo_block')",
            name="ck_meetings_type",
        ),
        CheckConstraint(
            "processing_status IN ('pending','processing','completed','failed')",
            name="ck_meetings_processing_status",
        ),
        CheckConstraint(
            "sentiment IN ('positive','neutral','tense','negative','urgent')",
            name="ck_meetings_sentiment",
        ),
        Index("idx_meetings_start", "start_time"),
        Index("idx_meetings_calendar_event", "calendar_event_id"),
        Index("idx_meetings_series", "recurring_series_id"),
    )


class MeetingAttendee(Base):
    __tablename__ = "meeting_attendees"

    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id"), primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id"), primary_key=True)

    meeting = relationship("Meeting", back_populates="attendees")
    person = relationship("Person")


# ═══════════════════════════════════════════════════════════
# EMAILS
# ═══════════════════════════════════════════════════════════


class Email(Base):
    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    graph_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    subject: Mapped[str | None] = mapped_column(Text)
    sender_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"))
    recipients: Mapped[dict | None] = mapped_column(JSONB)
    datetime_: Mapped[datetime] = mapped_column("datetime", TSTZ, nullable=False)
    body_text: Mapped[str | None] = mapped_column(Text)
    body_preview: Mapped[str | None] = mapped_column(Text)
    thread_id: Mapped[str | None] = mapped_column(Text)
    is_read: Mapped[bool | None] = mapped_column(Boolean)
    importance: Mapped[str | None] = mapped_column(Text)
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False)
    email_class: Mapped[str | None] = mapped_column(String, default="human")
    triage_class: Mapped[str | None] = mapped_column(String)
    triage_score: Mapped[float | None] = mapped_column(Float)
    intent: Mapped[str | None] = mapped_column(String)
    requires_response: Mapped[bool | None] = mapped_column(Boolean)
    response_status: Mapped[str | None] = mapped_column(String)
    summary: Mapped[str | None] = mapped_column(Text)
    last_extracted_at: Mapped[datetime | None] = mapped_column(TSTZ)
    processing_status: Mapped[str | None] = mapped_column(String, default="pending")
    processing_error: Mapped[str | None] = mapped_column(Text)
    sentiment: Mapped[str | None] = mapped_column(String)
    embedding = mapped_column(Vector(1536))

    sender = relationship("Person", foreign_keys=[sender_id])

    __table_args__ = (
        CheckConstraint(
            "email_class IN ('human','automated','newsletter')", name="ck_emails_class"
        ),
        CheckConstraint(
            "triage_class IN ('substantive','contextual','noise')", name="ck_emails_triage"
        ),
        CheckConstraint(
            "intent IN ('request','fyi','decision_needed','follow_up',"
            "'question','response','scheduling')",
            name="ck_emails_intent",
        ),
        CheckConstraint(
            "response_status IN ('pending','replied','no_action_needed','overdue')",
            name="ck_emails_response_status",
        ),
        CheckConstraint(
            "processing_status IN ('pending','processing','completed','failed')",
            name="ck_emails_processing_status",
        ),
        CheckConstraint(
            "sentiment IN ('positive','neutral','tense','negative','urgent')",
            name="ck_emails_sentiment",
        ),
        Index("idx_emails_datetime", "datetime"),
        Index("idx_emails_thread", "thread_id"),
        Index("idx_emails_sender", "sender_id"),
    )


# ═══════════════════════════════════════════════════════════
# EXTRACTED ENTITIES (declared before email_asks/chat_asks for FKs)
# ═══════════════════════════════════════════════════════════


class ActionItem(Base):
    __tablename__ = "action_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"))
    source_meeting_id: Mapped[int | None] = mapped_column(ForeignKey("meetings.id"))
    source_email_id: Mapped[int | None] = mapped_column(ForeignKey("emails.id"))
    source_chat_message_id: Mapped[int | None] = mapped_column(ForeignKey("chat_messages.id", use_alter=True))
    deadline: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String, default="open")
    created: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")
    updated: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")
    embedding = mapped_column(Vector(1536))

    __table_args__ = (
        CheckConstraint(
            "status IN ('open','in_progress','completed','stale')",
            name="ck_action_items_status",
        ),
        Index("idx_action_items_status", "status"),
        Index("idx_action_items_assignee", "assignee_id"),
    )


class EmailAsk(Base):
    __tablename__ = "email_asks"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"), nullable=False)
    thread_id: Mapped[str | None] = mapped_column(Text)
    ask_type: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    requester_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"))
    target_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"))
    deadline: Mapped[str | None] = mapped_column(Text)
    urgency: Mapped[str | None] = mapped_column(String, default="medium")
    status: Mapped[str | None] = mapped_column(String, default="open")
    resolved_by_email_id: Mapped[int | None] = mapped_column(ForeignKey("emails.id"))
    linked_action_item_id: Mapped[int | None] = mapped_column(ForeignKey("action_items.id"))
    linked_meeting_id: Mapped[int | None] = mapped_column(ForeignKey("meetings.id"))
    created: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")
    updated: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")
    embedding = mapped_column(Vector(1536))

    __table_args__ = (
        CheckConstraint(
            "ask_type IN ('deliverable','decision','follow_up','question',"
            "'approval','review','info_request')",
            name="ck_email_asks_type",
        ),
        CheckConstraint(
            "urgency IN ('high','medium','low')", name="ck_email_asks_urgency"
        ),
        CheckConstraint(
            "status IN ('open','in_progress','completed','stale')",
            name="ck_email_asks_status",
        ),
        Index("idx_email_asks_status", "status"),
        Index("idx_email_asks_target", "target_id"),
    )


# ═══════════════════════════════════════════════════════════
# TEAMS
# ═══════════════════════════════════════════════════════════


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    graph_team_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)


class TeamChannel(Base):
    __tablename__ = "team_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    graph_channel_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)


class TeamMembership(Base):
    __tablename__ = "team_memberships"

    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id"), primary_key=True)
    role: Mapped[str | None] = mapped_column(Text)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    graph_message_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    chat_id: Mapped[str | None] = mapped_column(Text)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("team_channels.id"))
    sender_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"))
    datetime_: Mapped[datetime] = mapped_column("datetime", TSTZ, nullable=False)
    body_text: Mapped[str | None] = mapped_column(Text)
    body_preview: Mapped[str | None] = mapped_column(Text)
    thread_root_id: Mapped[str | None] = mapped_column(Text)
    linked_meeting_id: Mapped[int | None] = mapped_column(ForeignKey("meetings.id"))
    noise_filtered: Mapped[bool] = mapped_column(Boolean, default=False)
    triage_class: Mapped[str | None] = mapped_column(String)
    triage_score: Mapped[float | None] = mapped_column(Float)
    intent: Mapped[str | None] = mapped_column(String)
    requires_response: Mapped[bool | None] = mapped_column(Boolean)
    summary: Mapped[str | None] = mapped_column(Text)
    last_extracted_at: Mapped[datetime | None] = mapped_column(TSTZ)
    processing_status: Mapped[str | None] = mapped_column(String, default="pending")
    processing_error: Mapped[str | None] = mapped_column(Text)
    sentiment: Mapped[str | None] = mapped_column(String)
    embedding = mapped_column(Vector(1536))

    __table_args__ = (
        CheckConstraint(
            "source_type IN ('teams_chat','teams_channel')", name="ck_chat_messages_source"
        ),
        CheckConstraint(
            "triage_class IN ('substantive','contextual','noise')",
            name="ck_chat_messages_triage",
        ),
        CheckConstraint(
            "intent IN ('request','fyi','decision_needed','follow_up','question','response')",
            name="ck_chat_messages_intent",
        ),
        CheckConstraint(
            "processing_status IN ('pending','processing','completed','failed')",
            name="ck_chat_messages_processing_status",
        ),
        CheckConstraint(
            "sentiment IN ('positive','neutral','tense','negative','urgent')",
            name="ck_chat_messages_sentiment",
        ),
        Index("idx_chat_messages_datetime", "datetime"),
    )


class ChatAsk(Base):
    __tablename__ = "chat_asks"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("chat_messages.id"), nullable=False)
    ask_type: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    requester_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"))
    target_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"))
    deadline: Mapped[str | None] = mapped_column(Text)
    urgency: Mapped[str | None] = mapped_column(String, default="medium")
    status: Mapped[str | None] = mapped_column(String, default="open")
    resolved_by_message_id: Mapped[int | None] = mapped_column(ForeignKey("chat_messages.id"))
    linked_action_item_id: Mapped[int | None] = mapped_column(ForeignKey("action_items.id"))
    created: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")
    updated: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")
    embedding = mapped_column(Vector(1536))

    __table_args__ = (
        CheckConstraint(
            "ask_type IN ('deliverable','decision','follow_up','question',"
            "'approval','review','info_request')",
            name="ck_chat_asks_type",
        ),
        CheckConstraint("urgency IN ('high','medium','low')", name="ck_chat_asks_urgency"),
        CheckConstraint(
            "status IN ('open','in_progress','completed','stale')",
            name="ck_chat_asks_status",
        ),
        Index("idx_chat_asks_status", "status"),
    )


# ═══════════════════════════════════════════════════════════
# MORE EXTRACTED ENTITIES
# ═══════════════════════════════════════════════════════════


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("people.id"))
    source_meeting_id: Mapped[int | None] = mapped_column(ForeignKey("meetings.id"))
    source_email_id: Mapped[int | None] = mapped_column(ForeignKey("emails.id"))
    datetime_: Mapped[datetime] = mapped_column("datetime", TSTZ, server_default="now()")
    embedding = mapped_column(Vector(1536))


class Commitment(Base):
    __tablename__ = "commitments"

    id: Mapped[int] = mapped_column(primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    committer_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"))
    recipient_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"))
    source_meeting_id: Mapped[int | None] = mapped_column(ForeignKey("meetings.id"))
    source_email_id: Mapped[int | None] = mapped_column(ForeignKey("emails.id"))
    deadline: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String, default="open")
    created: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")

    __table_args__ = (
        CheckConstraint(
            "status IN ('open','completed','overdue','broken')",
            name="ck_commitments_status",
        ),
    )


class Dependency(Base):
    __tablename__ = "dependencies"

    id: Mapped[int] = mapped_column(primary_key=True)
    blocker_workstream_id: Mapped[int | None] = mapped_column(ForeignKey("workstreams.id"))
    blocked_workstream_id: Mapped[int | None] = mapped_column(ForeignKey("workstreams.id"))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String, default="active")
    identified_date: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")

    __table_args__ = (
        CheckConstraint("status IN ('active','resolved')", name="ck_dependencies_status"),
    )


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    embedding = mapped_column(Vector(1536))


# ═══════════════════════════════════════════════════════════
# TOPIC JUNCTION TABLES
# ═══════════════════════════════════════════════════════════


class MeetingTopic(Base):
    __tablename__ = "meeting_topics"

    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id"), primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"), primary_key=True)


class EmailTopic(Base):
    __tablename__ = "email_topics"

    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"), primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"), primary_key=True)


class ChatMessageTopic(Base):
    __tablename__ = "chat_message_topics"

    chat_message_id: Mapped[int] = mapped_column(
        ForeignKey("chat_messages.id"), primary_key=True
    )
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"), primary_key=True)


# ═══════════════════════════════════════════════════════════
# SYSTEM TABLES
# ═══════════════════════════════════════════════════════════


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_type: Mapped[str] = mapped_column(String, nullable=False)
    triggered_by_type: Mapped[str | None] = mapped_column(String)
    triggered_by_id: Mapped[int | None] = mapped_column(Integer)
    recipient_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"))
    channel: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(Text)
    chat_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String, default="pending_review")
    created: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")
    sent_at: Mapped[datetime | None] = mapped_column(TSTZ)

    __table_args__ = (
        CheckConstraint(
            "draft_type IN ('nudge','recap','response','follow_up')",
            name="ck_drafts_type",
        ),
        CheckConstraint(
            "triggered_by_type IN ('action_item','email_ask','chat_ask','meeting')",
            name="ck_drafts_triggered_by",
        ),
        CheckConstraint(
            "channel IN ('email','teams_chat')", name="ck_drafts_channel"
        ),
        CheckConstraint(
            "status IN ('pending_review','sent','discarded','edited')",
            name="ck_drafts_status",
        ),
    )


class Briefing(Base):
    __tablename__ = "briefings"

    id: Mapped[int] = mapped_column(primary_key=True)
    briefing_type: Mapped[str] = mapped_column(String, nullable=False)
    related_meeting_id: Mapped[int | None] = mapped_column(ForeignKey("meetings.id"))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")

    __table_args__ = (
        CheckConstraint(
            "briefing_type IN ('morning','monday','friday','meeting_prep')",
            name="ck_briefings_type",
        ),
    )


class VoiceProfile(Base):
    __tablename__ = "voice_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    auto_profile: Mapped[str | None] = mapped_column(Text)
    custom_rules: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    edit_history: Mapped[dict | None] = mapped_column(JSONB)
    last_learned_at: Mapped[datetime | None] = mapped_column(TSTZ)
    updated: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")


class DashboardCache(Base):
    __tablename__ = "dashboard_cache"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    messages: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="'[]'")
    created: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")
    last_active: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")


class AdminSetting(Base):
    __tablename__ = "admin_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    updated: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")


class SentimentAggregation(Base):
    __tablename__ = "sentiment_aggregations"

    id: Mapped[int] = mapped_column(primary_key=True)
    scope_type: Mapped[str] = mapped_column(String, nullable=False)
    scope_id: Mapped[str] = mapped_column(Text, nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    avg_score: Mapped[float] = mapped_column(Float, nullable=False)
    interaction_count: Mapped[int] = mapped_column(Integer, nullable=False)
    trend: Mapped[str | None] = mapped_column(String)
    computed_at: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")

    __table_args__ = (
        UniqueConstraint("scope_type", "scope_id", "period_start", name="uq_sentiment_scope"),
        CheckConstraint(
            "scope_type IN ('person','relationship','department','cross_department','workstream')",
            name="ck_sentiment_scope_type",
        ),
        CheckConstraint("trend IN ('up','down','flat')", name="ck_sentiment_trend"),
    )


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    graph_attachment_id: Mapped[str | None] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    is_inline: Mapped[bool] = mapped_column(Boolean, default=False)
    created: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")

    __table_args__ = (
        CheckConstraint(
            "source_type IN ('email','chat_message')", name="ck_attachments_source"
        ),
    )


class SystemHealth(Base):
    __tablename__ = "system_health"

    service: Mapped[str] = mapped_column(Text, primary_key=True)
    last_success: Mapped[datetime | None] = mapped_column(TSTZ)
    last_error: Mapped[datetime | None] = mapped_column(TSTZ)
    last_error_message: Mapped[str | None] = mapped_column(Text)
    items_processed_last_hour: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str | None] = mapped_column(String, default="healthy")
    updated: Mapped[datetime] = mapped_column(TSTZ, server_default="now()")

    __table_args__ = (
        CheckConstraint(
            "status IN ('healthy','degraded','down')", name="ck_system_health_status"
        ),
    )


class LLMUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    task: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    calls: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (
        UniqueConstraint("date", "model", "task", name="uq_llm_usage_daily"),
    )
