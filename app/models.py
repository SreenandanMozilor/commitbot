"""
ORM models for CommitBot.

Each table corresponds to a real responsibility in the revised spec.
Soft-delete is used where the spec requires recoverability (Bin, Archive).
Hard-delete only happens via the 48hr purge sweep in scheduler.py.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, time, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db import Base

import re as _re

# Hex-color whitelist enforced model-side so EVERY write path — service
# layer, direct ORM construction in init_db.py / slack_app.py, future
# Alembic seed scripts — produces a value that's safe to interpolate
# into the dashboard's inline CSS. Defense in depth alongside the
# `safe_css_color` Jinja filter and the service-layer validation in
# `commit_svc.create_priority_level`.
_HEX_COLOR_RE = _re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class CommitmentState(str, enum.Enum):
    """
    Lifecycle state of a commitment. Orthogonal to outcome — a COMPLETE
    commitment can be either SUCCESS or FAILED; an ARCHIVED commitment
    carries the outcome from when it was completed; a DELETED commitment is
    always FAILED unless it was completed first.

    REASSIGNED here means "live, being worked on by a new owner after an
    accepted hand-off" — *not* "awaiting acceptance." The 24-hour limbo
    while a reassignment is pending is modelled as ON_HOLD (no auto-resume,
    Reassignment.expires_at carries the timer).
    """
    ACTIVE = "active"
    ON_HOLD = "on_hold"          # paused; manual hold OR awaiting reassignment
    REASSIGNED = "reassigned"    # live under new owner after accepted hand-off
    COMPLETE = "complete"
    ARCHIVED = "archived"        # soft, completed-only
    DELETED = "deleted"          # in Bin, 48hr purge


class CommitmentOutcome(str, enum.Enum):
    """How a commitment ended up. Set on every terminal transition.

    SUCCESS = there's a completed_at AND it was at or before the deadline
              (or there was no deadline).
    FAILED  = anything else: never completed, or completed late.
    """
    SUCCESS = "success"
    FAILED = "failed"


class CaptureSource(str, enum.Enum):
    SLASH_COMMAND = "slash_command"        # /commit ... (replaces "Lightning Bolt")
    GLOBAL_SHORTCUT = "global_shortcut"    # shortcut launcher → modal
    NOTATION = "notation"                  # [[commit @person]] etc.
    MESSAGE_SHORTCUT = "message_shortcut"  # right-click → "Mark as commitment"
    DASHBOARD = "dashboard"                # created directly in web UI
    AGENT = "agent"                        # auto-detected by the agentic classifier


class ReassignmentStatus(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class PingType(str, enum.Enum):
    BASE = "base"
    ESCALATION = "escalation"
    DIGEST = "digest"


class EditSource(str, enum.Enum):
    SLACK = "slack"
    DASHBOARD = "dashboard"


# ---------------------------------------------------------------------------
# Workspace + User
# ---------------------------------------------------------------------------

class Workspace(Base):
    """A single Slack workspace install."""
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    slack_team_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(255))
    bot_token: Mapped[str] = mapped_column(String(255))  # encrypt in prod; plain for dev MVP
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    users: Mapped[list["User"]] = relationship(back_populates="workspace", cascade="all, delete-orphan")


class User(Base):
    """
    A user inside a workspace. Slack identity is the primary unique key
    (a single human in two workspaces = two User rows; this is intentional and
    matches Slack's workspace-scoped identity model).
    """
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("workspace_id", "slack_user_id", name="uq_user_workspace_slack"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    slack_user_id: Mapped[str] = mapped_column(String(32), index=True)
    email: Mapped[Optional[str]] = mapped_column(String(320))
    display_name: Mapped[Optional[str]] = mapped_column(String(255))

    # Preferences (F7, F8, F10)
    start_of_day: Mapped[time] = mapped_column(Time, default=time(9, 0))
    # Per-user retention. After this many days, COMPLETE commitments are
    # auto HARD-DELETED (row removed from the DB; not soft-deleted to the
    # bin). Special case: when set to 0, the sweep archives them instead —
    # the safe option for users who don't want anything actually purged.
    # The sweep runs hourly in scheduler.py.
    auto_delete_completed_after_days: Mapped[int] = mapped_column(Integer, default=30)
    # Auto-resume ON_HOLD commitments when their deadline gets this close.
    # Independent of `on_hold_resume_at` — if either trigger fires, the
    # commitment goes back to its prior state. 0 = off. Default 24h so
    # held commitments don't silently miss their deadlines.
    auto_resume_hours_before_deadline: Mapped[int] = mapped_column(Integer, default=24)
    global_pause: Mapped[bool] = mapped_column(Boolean, default=False)
    reaction_signal_enabled: Mapped[bool] = mapped_column(Boolean, default=False)   # F10 default OFF
    threaded_confirm_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    tz: Mapped[str] = mapped_column(String(64), default="UTC")
    # When non-zero, an ON_HOLD commitment auto-resumes when its deadline
    # is within this many hours. Default 24h. Set to 0 to disable, in which
    # case held commitments only wake up via explicit Resume (or their own
    # on_hold_resume_at if a snooze set one).
    auto_resume_hours_before_deadline: Mapped[int] = mapped_column(
        Integer, default=24,
    )

    # --- Agentic commitment capture ---
    # When True, every message we see from this user in channels the bot is
    # in is buffered and periodically classified by the LLM agent.
    # High-confidence candidates auto-become commitments with a 1h Undo
    # window on the Home tab. Defaults OFF — the agent is opt-in, mirroring
    # the rest of the system's "data they can't see is worse than no data"
    # ethos.
    agent_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Per-user override of the system AGENT_CONFIDENCE_FLOOR. Stored as an
    # integer 0..100 (percent) for simplicity. NULL = use the system floor.
    agent_confidence_floor_pct: Mapped[Optional[int]] = mapped_column(Integer)

    # Set the first time the user completes Sign-in-with-Slack on the
    # dashboard. NULL means they were auto-provisioned by a bot interaction
    # (or seeded for demo) and haven't proven ownership yet — the Slack
    # capture paths refuse to log commitments for un-onboarded users so we
    # don't accumulate data they can never see.
    signed_in_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    workspace: Mapped[Workspace] = relationship(back_populates="users")
    priority_levels: Mapped[list["PriorityLevel"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    notations: Mapped[list["Notation"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    commitments: Mapped[list["Commitment"]] = relationship(back_populates="user", cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# Per-user configuration
# ---------------------------------------------------------------------------

class PriorityLevel(Base):
    """User-defined priority. No fixed set imposed (per the spec)."""
    __tablename__ = "priority_levels"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    name: Mapped[str] = mapped_column(String(64))
    color: Mapped[str] = mapped_column(String(16), default="#888888")
    base_ping_interval_minutes: Mapped[int] = mapped_column(Integer, default=240)        # 4h
    escalation_trigger_hours_before_deadline: Mapped[int] = mapped_column(Integer, default=24)
    max_ping_frequency_minutes: Mapped[int] = mapped_column(Integer, default=30)         # floor
    escalation_rate: Mapped[float] = mapped_column(default=2.0)                          # multiplier per stage

    daily_digest_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    daily_digest_time: Mapped[time] = mapped_column(Time, default=time(9, 0))

    is_system_default: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="priority_levels")

    @validates("color")
    def _validate_color(self, _key: str, value: object) -> str:
        """Reject anything that isn't a strict `#abc` / `#aabbcc` hex value.

        Runs on every assignment, including the implicit one during ORM
        construction (`PriorityLevel(color="…")`). Keeps the CSS-injection
        surface closed even when callers bypass the service layer.
        """
        s = "" if value is None else str(value).strip()
        if not _HEX_COLOR_RE.match(s):
            raise ValueError(
                f"Invalid priority color {value!r} — must be hex like #aabbcc."
            )
        return s


class Notation(Base):
    """Custom commitment notation patterns. Cap of 5 enforced in service layer."""
    __tablename__ = "notations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    pattern: Mapped[str] = mapped_column(String(128))   # regex; validator rejects '?' delimiters (F11)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    user: Mapped[User] = relationship(back_populates="notations")


# ---------------------------------------------------------------------------
# Commitment + related
# ---------------------------------------------------------------------------

class Commitment(Base):
    """
    A single commitment. Owner = `user_id` (whose Tab 1 this lives in).
    Tab 2 ("owed to me") is computed: commitments where the current user
    appears in CommitmentRecipient.is_current.
    """
    __tablename__ = "commitments"
    __table_args__ = (
        # Dedup key — F13 fix. NULLs (dashboard-created) are allowed and not unique.
        UniqueConstraint(
            "workspace_id", "slack_channel_id", "slack_message_ts",
            name="uq_commitment_slack_message",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)

    text: Mapped[str] = mapped_column(Text)
    source: Mapped[CaptureSource] = mapped_column(SAEnum(CaptureSource))

    slack_channel_id: Mapped[Optional[str]] = mapped_column(String(32))
    slack_message_ts: Mapped[Optional[str]] = mapped_column(String(32))
    bot_confirm_ts: Mapped[Optional[str]] = mapped_column(String(32))  # threaded reply we can edit (F3)

    deadline: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    priority_level_id: Mapped[Optional[str]] = mapped_column(ForeignKey("priority_levels.id"))

    state: Mapped[CommitmentState] = mapped_column(SAEnum(CommitmentState), default=CommitmentState.ACTIVE, index=True)
    # Terminal classification. NULL while the commitment is still in flight
    # (ACTIVE / ON_HOLD / REASSIGNED). Set whenever transitioning into
    # COMPLETE / ARCHIVED / DELETED. See services.commitments.compute_outcome.
    outcome: Mapped[Optional[CommitmentOutcome]] = mapped_column(
        SAEnum(CommitmentOutcome), index=True,
    )
    on_hold_resume_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # When transitioning into ON_HOLD (manual snooze OR reassignment limbo),
    # we stash the state we came from. On resume / decline / cancel / expire
    # we restore to it instead of always going to ACTIVE — so a REASSIGNED
    # commitment that gets snoozed comes back as REASSIGNED, not ACTIVE.
    prior_state: Mapped[Optional[CommitmentState]] = mapped_column(
        SAEnum(CommitmentState),
    )
    escalation_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    delivery_mode: Mapped[str] = mapped_column(String(16), default="individual")  # 'individual' | 'digest'

    # Dependency link — "Blocked: waiting on X". Schema-only Phase 2 hook;
    # no service code reads or writes it today, no UI surfaces it. Kept so
    # the migration path is empty when we eventually wire up the feature.
    blocked_by_id: Mapped[Optional[str]] = mapped_column(ForeignKey("commitments.id"))

    # Conflict-resolution (F9)
    version: Mapped[int] = mapped_column(Integer, default=1)
    last_writer: Mapped[Optional[str]] = mapped_column(String(32))  # 'slack' | 'dashboard'

    # --- Agent provenance (only set when source == CaptureSource.AGENT) ---
    # Confidence the classifier reported for this capture, stored as 0.0..1.0.
    # Surfaced in the 'Recently auto-captured' Home section so the user can
    # eyeball "the agent was 0.93 sure, fine" vs "0.78, double-check this."
    agent_confidence: Mapped[Optional[float]] = mapped_column()
    # Short rationale the model returned. Kept under ~280 chars so it fits
    # in a Block Kit context line. NULL for non-agent captures.
    agent_rationale: Mapped[Optional[str]] = mapped_column(Text)

    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # for 48hr Bin
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    user: Mapped[User] = relationship(back_populates="commitments")
    recipients: Mapped[list["CommitmentRecipient"]] = relationship(
        back_populates="commitment", cascade="all, delete-orphan"
    )
    edits: Mapped[list["CommitmentEdit"]] = relationship(
        back_populates="commitment", cascade="all, delete-orphan"
    )
    pings: Mapped[list["Ping"]] = relationship(
        back_populates="commitment", cascade="all, delete-orphan"
    )
    reassignments: Mapped[list["Reassignment"]] = relationship(
        back_populates="commitment", cascade="all, delete-orphan",
    )


class CommitmentRecipient(Base):
    """
    Who this commitment is OWED TO. Multi-recipient supported.
    `is_current` implements the pointer system from the spec — on reassignment,
    the old recipient row sets is_current=False and a new row with is_current=True is inserted.
    """
    __tablename__ = "commitment_recipients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    commitment_id: Mapped[str] = mapped_column(ForeignKey("commitments.id", ondelete="CASCADE"), index=True)
    # Holds either a real Slack user ID (e.g. "U12345") for chat-captured
    # commitments, or a free-text name (e.g. "Priya") for ones logged from the
    # dashboard. 128 chars is generous for a display name.
    recipient_slack_user_id: Mapped[Optional[str]] = mapped_column(String(128))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    commitment: Mapped[Commitment] = relationship(back_populates="recipients")


class CommitmentEdit(Base):
    """Audit log of edits — surfaces 'edited via Slack' / 'edited via dashboard' label."""
    __tablename__ = "commitment_edits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    commitment_id: Mapped[str] = mapped_column(ForeignKey("commitments.id", ondelete="CASCADE"), index=True)
    source: Mapped[EditSource] = mapped_column(SAEnum(EditSource))
    field: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[Optional[str]] = mapped_column(Text)
    new_value: Mapped[Optional[str]] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    commitment: Mapped[Commitment] = relationship(back_populates="edits")


class Reassignment(Base):
    """A pending or resolved reassignment of a commitment."""
    __tablename__ = "reassignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    commitment_id: Mapped[str] = mapped_column(ForeignKey("commitments.id", ondelete="CASCADE"), index=True)

    from_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    to_slack_user_id: Mapped[str] = mapped_column(String(32))  # may not be a User row yet

    note: Mapped[Optional[str]] = mapped_column(Text)
    # The DM we sent the recipient. Stashed so we can `chat.update` it when
    # the request is resolved (accept / decline / cancel / expire) — retires
    # the buttons and shows the final outcome inline.
    notice_channel_id: Mapped[Optional[str]] = mapped_column(String(32))
    notice_message_ts: Mapped[Optional[str]] = mapped_column(String(32))

    initiated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[ReassignmentStatus] = mapped_column(SAEnum(ReassignmentStatus), default=ReassignmentStatus.PENDING, index=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    commitment: Mapped["Commitment"] = relationship(back_populates="reassignments")


class Ping(Base):
    """A scheduled or sent reminder for a commitment."""
    __tablename__ = "pings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    commitment_id: Mapped[str] = mapped_column(ForeignKey("commitments.id", ondelete="CASCADE"), index=True)

    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    type: Mapped[PingType] = mapped_column(SAEnum(PingType), default=PingType.BASE)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    action_taken: Mapped[Optional[str]] = mapped_column(String(32))   # done | snooze_2h | snooze_tomorrow | stop_escalation
    action_taken_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    commitment: Mapped[Commitment] = relationship(back_populates="pings")


# ---------------------------------------------------------------------------
# Agentic capture
# ---------------------------------------------------------------------------

class AgentMessageBuffer(Base):
    """
    Lightweight rolling buffer of Slack messages we plan to feed to the
    classifier. Populated by the message event handler when the sender has
    `agent_enabled=True`; drained by the periodic scan job.

    Why a buffer instead of pulling history on demand:
      - The bot is already subscribed to message events for channels it's in,
        so we don't need extra `conversations.history` scope.
      - We classify in batches, which is dramatically cheaper than one LLM
        call per message.
      - `processed_at` makes the sweep idempotent — re-running the job won't
        re-classify rows it's already looked at.

    Retention: pruned by the scheduler after `AGENT_BUFFER_RETENTION_DAYS`,
    regardless of whether they were classified — we don't keep raw message
    text indefinitely.
    """
    __tablename__ = "agent_message_buffer"
    __table_args__ = (
        # Dedup: a single Slack message should buffer once per user. The
        # Slack event API can re-deliver under retry; this keeps us honest.
        UniqueConstraint(
            "user_id", "slack_channel_id", "slack_message_ts",
            name="uq_agent_buffer_message",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True,
    )
    slack_channel_id: Mapped[str] = mapped_column(String(32))
    slack_message_ts: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True,
    )
    # When non-null, the agent has already examined this row in a batch.
    # The classifier's verdict isn't stored here — if it was a commitment,
    # the row in `commitments` is the record. Negative classifications
    # leave no trace beyond `processed_at`.
    processed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), index=True,
    )
