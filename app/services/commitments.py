"""
Commitment service layer.

Centralises every state transition and field mutation so the rules from the
revised spec (F7-F9 conflict resolution, F8 on-hold precedence, F13 dedup,
F11 notation validation) live in one place and the audit log (CommitmentEdit)
is always written consistently.

Routes (HTTP) and Slack handlers both call into here — neither does field-level
mutation directly.

v0.3.0 additions:
  - set_escalation_enabled (B2: stop-esc through the service layer)
  - reopen (B7: complete → active path for accidental completions)
  - _ensure_editable guard (B8: prevent field edits on non-editable states)
  - soft_delete_priority_level repoints commitments (B15)
  - create_commitment resolves dead priority_ids to the user's default
"""
from __future__ import annotations

import re
from datetime import datetime, time, timezone
from typing import Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import (
    CaptureSource,
    Commitment,
    CommitmentEdit,
    CommitmentOutcome,
    CommitmentRecipient,
    CommitmentState,
    EditSource,
    PriorityLevel,
    ReassignmentStatus,
    User,
)


# --- Constants -------------------------------------------------------------

MAX_TEXT_LEN = 1000
MAX_NOTATIONS_PER_USER = 5

# Mutation is only allowed on these states. Completed/Archived/Deleted commitments
# are immutable except via state-transition routes (restore, reopen, archive…).
# REASSIGNED is included — once Bob has accepted, the commitment is live again
# under him and should behave like ACTIVE for field edits.
_EDITABLE_STATES = {
    CommitmentState.ACTIVE,
    CommitmentState.ON_HOLD,
    CommitmentState.REASSIGNED,
}


def compute_outcome(c: Commitment) -> CommitmentOutcome:
    """Classify a commitment as SUCCESS or FAILED.

    Rule:
      - FAILED if there's no completed_at (the user gave up or it expired).
      - SUCCESS if completed_at exists and it's at-or-before the deadline,
        OR the commitment had no deadline at all.
      - FAILED otherwise (i.e. completed but late).
    """
    if c.completed_at is None:
        return CommitmentOutcome.FAILED
    if c.deadline is None:
        return CommitmentOutcome.SUCCESS
    completed_at = c.completed_at
    deadline = c.deadline
    # Defensive: SQLite can hand us naive datetimes; treat them as UTC so the
    # comparison is well-defined either way.
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=timezone.utc)
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return (
        CommitmentOutcome.SUCCESS if completed_at <= deadline
        else CommitmentOutcome.FAILED
    )


# --- Notation validation (F11) ----------------------------------------------

_BANNED_DELIM_CHARS = set("?")
_REQUIRED_HINT_TOKENS = (
    "[[", "]]", "\\[\\[", "\\]\\]",
    "<<", ">>", "\\<\\<", "\\>\\>",
    "!commit", "/commit", "\\!commit",
)


def validate_notation_pattern(pattern: str) -> None:
    """Raise ValueError if the pattern is too prose-collision-prone."""
    if not pattern or len(pattern) > 128:
        raise ValueError("Pattern must be 1-128 characters.")
    if any(c in _BANNED_DELIM_CHARS for c in pattern):
        raise ValueError("'?' is not permitted as a notation delimiter (too common in normal prose).")
    if not any(token in pattern for token in _REQUIRED_HINT_TOKENS):
        raise ValueError(
            "Pattern must include an unambiguous delimiter token: "
            "one of [[ ]], << >>, !commit, or /commit."
        )
    try:
        re.compile(pattern)
    except re.error as e:
        raise ValueError(f"Pattern is not a valid regex: {e}") from e


# --- Text validation --------------------------------------------------------

def _clean_text(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("Commitment text is required.")
    if len(cleaned) > MAX_TEXT_LEN:
        raise ValueError(f"Commitment text must be {MAX_TEXT_LEN} characters or fewer.")
    return cleaned


# --- State guards (B8) ------------------------------------------------------

def _ensure_editable(c: Commitment) -> None:
    if c.state not in _EDITABLE_STATES:
        raise ValueError(
            f"Can't edit a commitment in state '{c.state.value}'. "
            "Restore or reopen it first."
        )
    # ON_HOLD with a PENDING reassignment is "limbo while a teammate decides."
    # Letting Alice change the text/deadline mid-flight would be deceptive —
    # Bob agreed to one thing, would inherit another. Cancel first to edit.
    if c.state == CommitmentState.ON_HOLD:
        if any(
            r.status == ReassignmentStatus.PENDING
            for r in (c.reassignments or [])
        ):
            raise ValueError(
                "This commitment is awaiting a reassignment response. "
                "Cancel the pending reassignment before editing it."
            )


# --- Creation ---------------------------------------------------------------

def find_existing_by_slack_message(
    db: Session, *, workspace_id: str, channel_id: str, message_ts: str
) -> Optional[Commitment]:
    return db.execute(
        select(Commitment).where(
            Commitment.workspace_id == workspace_id,
            Commitment.slack_channel_id == channel_id,
            Commitment.slack_message_ts == message_ts,
        )
    ).scalar_one_or_none()


def _resolve_priority(
    db: Session, owner: User, priority_level_id: Optional[str]
) -> Optional[str]:
    """Return a usable priority id: the given one if live, else the user's default."""
    if priority_level_id:
        pl = db.get(PriorityLevel, priority_level_id)
        if pl is not None and pl.user_id == owner.id and pl.deleted_at is None:
            return pl.id
    default = db.execute(
        select(PriorityLevel).where(
            PriorityLevel.user_id == owner.id,
            PriorityLevel.is_system_default.is_(True),
            PriorityLevel.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    return default.id if default else None


def create_commitment(
    db: Session,
    *,
    owner: User,
    text: str,
    source: CaptureSource,
    slack_channel_id: Optional[str] = None,
    slack_message_ts: Optional[str] = None,
    deadline: Optional[datetime] = None,
    priority_level_id: Optional[str] = None,
    recipient_slack_user_ids: Sequence[str] = (),
) -> Commitment:
    text = _clean_text(text)

    if slack_channel_id and slack_message_ts:
        existing = find_existing_by_slack_message(
            db,
            workspace_id=owner.workspace_id,
            channel_id=slack_channel_id,
            message_ts=slack_message_ts,
        )
        if existing is not None:
            return existing

    if deadline is not None and deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)

    priority_level_id = _resolve_priority(db, owner, priority_level_id)

    c = Commitment(
        user_id=owner.id,
        workspace_id=owner.workspace_id,
        text=text,
        source=source,
        slack_channel_id=slack_channel_id,
        slack_message_ts=slack_message_ts,
        deadline=deadline,
        priority_level_id=priority_level_id,
        state=CommitmentState.ACTIVE,
    )
    db.add(c)
    db.flush()

    for sid in recipient_slack_user_ids:
        db.add(CommitmentRecipient(commitment_id=c.id, recipient_slack_user_id=sid, is_current=True))

    db.flush()
    return c


# --- State transitions ------------------------------------------------------

def mark_done(db: Session, c: Commitment, *, source: EditSource) -> Commitment:
    if c.state == CommitmentState.COMPLETE:
        return c
    _log_edit(db, c, source, "state", c.state.value, CommitmentState.COMPLETE.value)
    c.state = CommitmentState.COMPLETE
    c.completed_at = datetime.now(timezone.utc)
    new_outcome = compute_outcome(c)
    if c.outcome != new_outcome:
        _log_edit(db, c, source, "outcome",
                  c.outcome.value if c.outcome else None, new_outcome.value)
        c.outcome = new_outcome
    _bump_version(c, source)

    # If the owner opted into "X=0 → archive immediately on completion",
    # short-circuit straight to ARCHIVED — they don't want a Complete tab.
    # The hourly auto-delete sweep would catch this within an hour anyway;
    # doing it inline means no delay between done-click and archival.
    if c.user is not None and c.user.auto_delete_completed_after_days == 0:
        archive(db, c, source=source)
    return c


def reopen(db: Session, c: Commitment, *, source: EditSource) -> Commitment:
    """B7: Reopen a completed commitment (back to Active). Clears `completed_at`
    and any outcome — the commitment is in flight again."""
    if c.state != CommitmentState.COMPLETE:
        return c
    _log_edit(db, c, source, "state", c.state.value, CommitmentState.ACTIVE.value)
    c.state = CommitmentState.ACTIVE
    c.completed_at = None
    if c.outcome is not None:
        _log_edit(db, c, source, "outcome", c.outcome.value, None)
        c.outcome = None
    _bump_version(c, source)
    return c


def put_on_hold(
    db: Session,
    c: Commitment,
    *,
    resume_at: Optional[datetime],
    source: EditSource,
) -> Commitment:
    if resume_at is not None and resume_at.tzinfo is None:
        resume_at = resume_at.replace(tzinfo=timezone.utc)
    # Only stash prior_state on entry — if we're already ON_HOLD, leave the
    # prior_state alone so successive holds don't lose the original state.
    if c.state != CommitmentState.ON_HOLD:
        c.prior_state = c.state
    _log_edit(db, c, source, "state", c.state.value, CommitmentState.ON_HOLD.value)
    c.state = CommitmentState.ON_HOLD
    c.on_hold_resume_at = resume_at
    _bump_version(c, source)
    return c


def resume(db: Session, c: Commitment, *, source: EditSource, manual: bool = False) -> Commitment:
    """
    F8: Manual resume always wins. Restores the commitment to its pre-hold
    state — usually ACTIVE, but REASSIGNED if it was a Bob-accepted
    commitment that got snoozed. Clears `prior_state` and `on_hold_resume_at`.
    """
    if c.state != CommitmentState.ON_HOLD:
        return c
    target = c.prior_state or CommitmentState.ACTIVE
    _log_edit(db, c, source, "state", c.state.value, target.value)
    c.state = target
    c.prior_state = None
    c.on_hold_resume_at = None
    _bump_version(c, source)
    return c


def archive(db: Session, c: Commitment, *, source: EditSource) -> Commitment:
    """
    Archive a commitment (long-term file-away). Allowed from:
      - COMPLETE: the normal "I finished this, file it" path.
      - DELETED: file-from-bin shortcut, so the user can keep something out
        of the 48h purge without first restoring + completing it.
    Active / On-Hold / already-Archived states must transition through
    Complete or Delete first.

    Outcome carries forward from the COMPLETE state, or is computed fresh
    when archiving from DELETED (which may have skipped the outcome step if
    the user deleted directly from active without completing).
    """
    if c.state in (CommitmentState.COMPLETE, CommitmentState.DELETED):
        _log_edit(db, c, source, "state",
                  c.state.value, CommitmentState.ARCHIVED.value)
        c.state = CommitmentState.ARCHIVED
        if c.state == CommitmentState.ARCHIVED and c.deleted_at is not None:
            # Coming up from the bin — drop the deletion stamp.
            c.deleted_at = None
        if c.outcome is None:
            new_outcome = compute_outcome(c)
            _log_edit(db, c, source, "outcome", None, new_outcome.value)
            c.outcome = new_outcome
        _bump_version(c, source)
        return c

    raise ValueError("Only completed or deleted commitments can be archived.")


def soft_delete(db: Session, c: Commitment, *, source: EditSource) -> Commitment:
    if c.state == CommitmentState.DELETED:
        return c
    _log_edit(db, c, source, "state", c.state.value, CommitmentState.DELETED.value)
    c.state = CommitmentState.DELETED
    c.deleted_at = datetime.now(timezone.utc)
    # Outcome is set on every terminal transition. If we're coming from
    # COMPLETE/ARCHIVED the outcome is already set; from ACTIVE it gets
    # computed now (almost always FAILED — the user gave up).
    if c.outcome is None:
        new_outcome = compute_outcome(c)
        _log_edit(db, c, source, "outcome", None, new_outcome.value)
        c.outcome = new_outcome
    _bump_version(c, source)
    return c


def restore_from_bin(db: Session, c: Commitment, *, source: EditSource) -> Commitment:
    if c.state != CommitmentState.DELETED:
        return c
    target = CommitmentState.COMPLETE if c.completed_at else CommitmentState.ACTIVE
    _log_edit(db, c, source, "state", c.state.value, target.value)
    c.state = target
    c.deleted_at = None
    # Restoring to ACTIVE means it's back in flight — clear the FAILED tag
    # the soft-delete stamped on it. COMPLETE restores keep their outcome.
    if target == CommitmentState.ACTIVE and c.outcome is not None:
        _log_edit(db, c, source, "outcome", c.outcome.value, None)
        c.outcome = None
    _bump_version(c, source)
    return c


# --- Field mutations (state-guarded — B8) -----------------------------------

def edit_text(db: Session, c: Commitment, new_text: str, *, source: EditSource) -> Commitment:
    _ensure_editable(c)
    new_text = _clean_text(new_text)
    old = c.text
    if old == new_text:
        return c
    _log_edit(db, c, source, "text", old, new_text)
    c.text = new_text
    _bump_version(c, source)
    return c


def set_deadline(
    db: Session,
    c: Commitment,
    deadline: Optional[datetime],
    *,
    source: EditSource,
) -> Commitment:
    _ensure_editable(c)
    if deadline is not None and deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    old = c.deadline.isoformat() if c.deadline else None
    new = deadline.isoformat() if deadline else None
    if old == new:
        return c
    _log_edit(db, c, source, "deadline", old, new)
    c.deadline = deadline
    _bump_version(c, source)
    return c


def set_priority(
    db: Session,
    c: Commitment,
    priority_level_id: Optional[str],
    *,
    source: EditSource,
) -> Commitment:
    _ensure_editable(c)
    if c.priority_level_id == priority_level_id:
        return c
    _log_edit(db, c, source, "priority_level_id", c.priority_level_id, priority_level_id)
    c.priority_level_id = priority_level_id
    _bump_version(c, source)
    return c


def set_recipients(
    db: Session,
    c: Commitment,
    recipient_ids: Sequence[str],
    *,
    source: EditSource,
) -> Commitment:
    """
    Replace the commitment's current recipients with the provided list.

    Dashboard edits are a *replacement*, not a reassignment — so old current
    rows are hard-deleted instead of having is_current flipped (which is the
    pattern reserved for the reassignment workflow).
    """
    _ensure_editable(c)
    current = [r for r in c.recipients if r.is_current]
    current_set = {r.recipient_slack_user_id for r in current if r.recipient_slack_user_id}
    new_list = [r for r in recipient_ids if r]
    new_set = set(new_list)

    if current_set == new_set:
        return c

    for r in current:
        db.delete(r)
    for sid in new_list:
        db.add(CommitmentRecipient(
            commitment_id=c.id, recipient_slack_user_id=sid, is_current=True,
        ))

    _log_edit(
        db, c, source, "recipients",
        ", ".join(sorted(current_set)) if current_set else None,
        ", ".join(sorted(new_set)) if new_set else None,
    )
    _bump_version(c, source)
    db.flush()
    return c


def set_escalation_enabled(
    db: Session,
    c: Commitment,
    enabled: bool,
    *,
    source: EditSource,
) -> Commitment:
    """B2: stop/start escalation through the service so the edit log catches it."""
    if c.escalation_enabled == enabled:
        return c
    _log_edit(db, c, source, "escalation_enabled", c.escalation_enabled, enabled)
    c.escalation_enabled = enabled
    _bump_version(c, source)
    return c


# --- Priority-level CRUD ----------------------------------------------------

def create_priority_level(
    db: Session,
    *,
    user: User,
    name: str,
    color: str = "#888888",
    base_ping_interval_minutes: int = 240,
    escalation_trigger_hours_before_deadline: int = 24,
    max_ping_frequency_minutes: int = 30,
    escalation_rate: float = 2.0,
    is_system_default: bool = False,
) -> PriorityLevel:
    name = (name or "").strip()
    if not name:
        raise ValueError("Priority level name is required.")
    # The system enforces an absolute floor on how frequently a commitment can
    # ping, regardless of how aggressively escalation is configured. Keep this
    # in sync with pings.SYSTEM_MIN_PING_INTERVAL_MINUTES.
    if base_ping_interval_minutes < 1 or max_ping_frequency_minutes < 1:
        raise ValueError("Ping intervals must be at least 1 minute.")
    if max_ping_frequency_minutes > base_ping_interval_minutes:
        raise ValueError("Max ping frequency must be <= base ping interval.")
    if escalation_rate < 1.0:
        raise ValueError("Escalation rate must be >= 1.0.")

    pl = PriorityLevel(
        user_id=user.id,
        name=name, color=color,
        base_ping_interval_minutes=base_ping_interval_minutes,
        escalation_trigger_hours_before_deadline=escalation_trigger_hours_before_deadline,
        max_ping_frequency_minutes=max_ping_frequency_minutes,
        escalation_rate=escalation_rate,
        is_system_default=is_system_default,
    )
    db.add(pl)
    db.flush()
    return pl


def soft_delete_priority_level(db: Session, pl: PriorityLevel) -> None:
    """
    B15: Soft-delete AND repoint every commitment using it to the user's
    default level. Otherwise the scheduler keeps firing pings using a
    soft-deleted level's cadence forever.
    """
    if pl.is_system_default:
        raise ValueError("Can't delete the default priority level.")

    default = db.execute(
        select(PriorityLevel).where(
            PriorityLevel.user_id == pl.user_id,
            PriorityLevel.is_system_default.is_(True),
            PriorityLevel.deleted_at.is_(None),
        )
    ).scalar_one_or_none()

    if default is not None:
        # synchronize_session="fetch" makes the in-session ORM objects pick up
        # the new value (so the test's `c` reflects the update without a
        # separate db.refresh / re-fetch).
        db.execute(
            update(Commitment)
            .where(Commitment.priority_level_id == pl.id)
            .values(priority_level_id=default.id)
            .execution_options(synchronize_session="fetch")
        )

    pl.deleted_at = datetime.now(timezone.utc)


# --- Internal helpers -------------------------------------------------------

def _bump_version(c: Commitment, source: EditSource) -> None:
    c.version = (c.version or 1) + 1
    c.last_writer = source.value


def _log_edit(
    db: Session, c: Commitment, source: EditSource, field: str, old: object, new: object
) -> None:
    db.add(
        CommitmentEdit(
            commitment_id=c.id,
            source=source,
            field=field,
            old_value=str(old) if old is not None else None,
            new_value=str(new) if new is not None else None,
        )
    )
