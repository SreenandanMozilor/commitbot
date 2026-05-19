"""
Reassignment service — transfer ownership of a commitment between users.

State machine (commitment-side):
    ACTIVE → REASSIGNED → ACTIVE
                        ↑ status = ACCEPTED  (owner_id := new owner)
                        ↑ status = DECLINED  (owner_id unchanged)
                        ↑ status = EXPIRED   (owner_id unchanged)
                        ↑ status = CANCELLED (owner_id unchanged)

Invariants enforced here:
  - Only the current owner can request or cancel.
  - Only the target can accept or decline.
  - Target must be onboarded (signed_in_at set) and in the same workspace.
  - No self-reassign.
  - At most one PENDING reassignment per commitment at a time.
  - Idempotent: accept/decline/cancel on a non-PENDING row is a no-op.

Pings:
  - On request:  pending pings deleted; REASSIGNED state means
    compute_next_ping_at returns None, so nothing else fires.
  - On accept:   commitment moves under target, priority remapped to target's
    default; ensure a pending ping exists with the new cadence.
  - On decline/cancel/expire: rollback to ACTIVE under original owner,
    re-arm a ping.

Audit log:
  Every transition writes a `CommitmentEdit` so the dashboard's edit history
  carries the story.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Commitment,
    CommitmentEdit,
    CommitmentState,
    EditSource,
    Ping,
    PriorityLevel,
    Reassignment,
    ReassignmentStatus,
    User,
    Workspace,
)
from app.services import pings as ping_svc

log = logging.getLogger(__name__)

# Per spec, the recipient has 24h to act before we expire the request.
REASSIGNMENT_TTL_HOURS = 24


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def find_target_in_workspace(
    db: Session, *, workspace_id: str, slack_user_id: str,
) -> Optional[User]:
    return db.execute(
        select(User).where(
            User.workspace_id == workspace_id,
            User.slack_user_id == slack_user_id,
        )
    ).scalar_one_or_none()


def pending_for_commitment(db: Session, commitment_id: str) -> Optional[Reassignment]:
    return db.execute(
        select(Reassignment).where(
            Reassignment.commitment_id == commitment_id,
            Reassignment.status == ReassignmentStatus.PENDING,
        )
    ).scalar_one_or_none()


def list_outgoing_pending(db: Session, *, owner_id: str) -> list[Reassignment]:
    """Reassignments this user has requested that haven't resolved yet."""
    return list(db.execute(
        select(Reassignment)
        .join(Commitment, Commitment.id == Reassignment.commitment_id)
        .where(
            Commitment.user_id == owner_id,
            Reassignment.status == ReassignmentStatus.PENDING,
        )
        .order_by(Reassignment.initiated_at.desc())
    ).scalars().all())


def list_incoming_pending(
    db: Session, *, workspace_id: str, slack_user_id: str,
) -> list[Reassignment]:
    """Reassignments addressed to this Slack user that haven't resolved yet."""
    return list(db.execute(
        select(Reassignment)
        .join(Commitment, Commitment.id == Reassignment.commitment_id)
        .where(
            Reassignment.to_slack_user_id == slack_user_id,
            Reassignment.status == ReassignmentStatus.PENDING,
            Commitment.workspace_id == workspace_id,
        )
        .order_by(Reassignment.initiated_at.desc())
    ).scalars().all())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_priority_for(db: Session, user: User) -> Optional[PriorityLevel]:
    return db.execute(
        select(PriorityLevel).where(
            PriorityLevel.user_id == user.id,
            PriorityLevel.is_system_default.is_(True),
            PriorityLevel.deleted_at.is_(None),
        )
    ).scalar_one_or_none()


def _bump(c: Commitment, source: EditSource) -> None:
    c.version = (c.version or 1) + 1
    c.last_writer = source.value


def _log(
    db: Session, c: Commitment, source: EditSource, field: str,
    old: object, new: object,
) -> None:
    db.add(CommitmentEdit(
        commitment_id=c.id,
        source=source,
        field=field,
        old_value=str(old) if old is not None else None,
        new_value=str(new) if new is not None else None,
    ))


def _delete_pending_pings(db: Session, commitment_id: str) -> None:
    db.execute(
        Ping.__table__.delete().where(
            Ping.commitment_id == commitment_id,
            Ping.sent_at.is_(None),
        )
    )


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

def request_reassignment(
    db: Session,
    *,
    commitment: Commitment,
    target_slack_user_id: str,
    source: EditSource,
    note: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Reassignment:
    """Owner asks for the commitment to be transferred to `target_slack_user_id`.

    The owner is implicit (the commitment's current `user_id`). Validation
    here matches everything we need to safely enter the REASSIGNED state.
    """
    now = now or datetime.now(timezone.utc)

    target_slack_user_id = (target_slack_user_id or "").strip()
    if not target_slack_user_id:
        raise ValueError("Pick someone to hand this off to.")

    if commitment.user is None:
        raise ValueError("Commitment has no owner — can't reassign.")
    if commitment.user.slack_user_id == target_slack_user_id:
        raise ValueError("You can't reassign a commitment to yourself.")

    # Check "already pending" before the generic state check — both will fire
    # for a REASSIGNED commitment, and "there's already a pending request"
    # is the actionable answer for that case.
    if pending_for_commitment(db, commitment.id) is not None:
        raise ValueError(
            "There's already a pending reassignment for this commitment. "
            "Cancel it first if you want to send to someone else."
        )

    # ACTIVE and REASSIGNED are both "live" states — both can be handed off.
    # ON_HOLD must be resumed first (the "already-pending" check above caught
    # the limbo case specifically; this handles manual holds).
    if commitment.state not in (
        CommitmentState.ACTIVE, CommitmentState.REASSIGNED,
    ):
        raise ValueError(
            "Only active commitments can be reassigned. "
            f"This one is '{commitment.state.value}'."
        )

    target = find_target_in_workspace(
        db, workspace_id=commitment.workspace_id,
        slack_user_id=target_slack_user_id,
    )
    if target is None or target.signed_in_at is None:
        raise ValueError(
            "That person hasn't signed in to CommitBot yet. "
            "Ask them to visit the dashboard once before you reassign to them."
        )

    note_clean = (note or "").strip() or None
    if note_clean and len(note_clean) > 500:
        raise ValueError("Reassignment note must be 500 characters or fewer.")

    r = Reassignment(
        commitment_id=commitment.id,
        from_user_id=commitment.user_id,
        to_slack_user_id=target_slack_user_id,
        initiated_at=now,
        expires_at=now + timedelta(hours=REASSIGNMENT_TTL_HOURS),
        status=ReassignmentStatus.PENDING,
        note=note_clean,
    )
    db.add(r)
    db.flush()

    # Park the commitment as ON_HOLD while we wait for the recipient. We
    # explicitly clear on_hold_resume_at so the auto-resume sweep ignores it
    # — the 24h timer lives on `Reassignment.expires_at` instead.
    # Stash the prior state (ACTIVE or REASSIGNED) so if Carol declines the
    # commitment goes back to where Bob had it, not always to ACTIVE.
    _log(db, commitment, source, "state",
         commitment.state.value, CommitmentState.ON_HOLD.value)
    _log(db, commitment, source, "reassignment_requested", None, target_slack_user_id)
    commitment.prior_state = commitment.state
    commitment.state = CommitmentState.ON_HOLD
    commitment.on_hold_resume_at = None
    _bump(commitment, source)
    _delete_pending_pings(db, commitment.id)

    return r


def cancel_reassignment(
    db: Session,
    *,
    reassignment: Reassignment,
    actor: User,
    source: EditSource,
    now: Optional[datetime] = None,
) -> Reassignment:
    """Owner withdraws a pending request. Commitment returns to ACTIVE."""
    if reassignment.status != ReassignmentStatus.PENDING:
        return reassignment  # idempotent

    c = db.get(Commitment, reassignment.commitment_id)
    if c is None:
        raise ValueError("Commitment no longer exists.")
    if actor.id != c.user_id:
        raise ValueError("Only the current owner can cancel a reassignment.")

    now = now or datetime.now(timezone.utc)
    reassignment.status = ReassignmentStatus.CANCELLED
    reassignment.decided_at = now

    _log(db, c, source, "reassignment_cancelled",
         reassignment.to_slack_user_id, None)
    _restore_to_active(db, c, source)
    return reassignment


def accept_reassignment(
    db: Session,
    *,
    reassignment: Reassignment,
    actor: User,
    source: EditSource,
    now: Optional[datetime] = None,
) -> Reassignment:
    """Recipient agrees — ownership moves to them, priority remapped to their
    default, a fresh ping is queued under the new cadence."""
    if reassignment.status != ReassignmentStatus.PENDING:
        return reassignment

    if actor.slack_user_id != reassignment.to_slack_user_id:
        raise ValueError("Only the named recipient can accept this reassignment.")
    if actor.signed_in_at is None:
        # Belt-and-braces — request_reassignment already enforces this, but
        # in case the user later got de-onboarded somehow.
        raise ValueError("You need to sign in once before accepting a commitment.")

    c = db.get(Commitment, reassignment.commitment_id)
    if c is None:
        raise ValueError("Commitment no longer exists.")
    if c.workspace_id != actor.workspace_id:
        raise ValueError("That reassignment isn't for your workspace.")

    now = now or datetime.now(timezone.utc)
    old_user_id = c.user_id
    new_default = _default_priority_for(db, actor)

    _log(db, c, source, "owner", old_user_id, actor.id)
    if new_default and c.priority_level_id != new_default.id:
        _log(db, c, source, "priority_level_id",
             c.priority_level_id, new_default.id)
        c.priority_level_id = new_default.id

    # State goes to REASSIGNED — a live, pingable state that signals this
    # commitment originated from a hand-off. Functionally equivalent to
    # ACTIVE for pinging/editing/completing; the distinction is purely
    # navigational (separate dashboard tab, separate Home section).
    _log(db, c, source, "state",
         c.state.value, CommitmentState.REASSIGNED.value)
    c.user_id = actor.id
    c.state = CommitmentState.REASSIGNED
    c.on_hold_resume_at = None
    c.prior_state = None  # accepted — the "where to return to" trail ends here
    _bump(c, source)

    reassignment.status = ReassignmentStatus.ACCEPTED
    reassignment.decided_at = now

    _delete_pending_pings(db, c.id)
    db.flush()
    ping_svc.schedule_initial_ping(db, c, new_default)
    return reassignment


def decline_reassignment(
    db: Session,
    *,
    reassignment: Reassignment,
    actor: User,
    source: EditSource,
    now: Optional[datetime] = None,
) -> Reassignment:
    """Recipient says no — commitment goes back to the original owner."""
    if reassignment.status != ReassignmentStatus.PENDING:
        return reassignment

    if actor.slack_user_id != reassignment.to_slack_user_id:
        raise ValueError("Only the named recipient can decline this reassignment.")

    c = db.get(Commitment, reassignment.commitment_id)
    if c is None:
        raise ValueError("Commitment no longer exists.")

    now = now or datetime.now(timezone.utc)
    reassignment.status = ReassignmentStatus.DECLINED
    reassignment.decided_at = now

    _log(db, c, source, "reassignment_declined",
         reassignment.to_slack_user_id, None)
    _restore_to_active(db, c, source)
    return reassignment


def expire_due(
    db: Session,
    *,
    now: Optional[datetime] = None,
) -> list[Reassignment]:
    """Scheduler entry point — flip any PENDING rows whose 24h is up."""
    now = now or datetime.now(timezone.utc)
    due = list(db.execute(
        select(Reassignment).where(
            Reassignment.status == ReassignmentStatus.PENDING,
            Reassignment.expires_at <= now,
        )
    ).scalars().all())

    for r in due:
        c = db.get(Commitment, r.commitment_id)
        if c is None:
            # Commitment was hard-deleted; just stamp the reassignment.
            r.status = ReassignmentStatus.EXPIRED
            r.decided_at = now
            continue
        r.status = ReassignmentStatus.EXPIRED
        r.decided_at = now
        # Scheduler isn't tied to a user action — log under SLACK as the source
        # since timers conceptually belong to the Slack side of the system.
        _log(db, c, EditSource.SLACK, "reassignment_expired",
             r.to_slack_user_id, None)
        _restore_to_active(db, c, EditSource.SLACK)
    return due


# ---------------------------------------------------------------------------
# Internal: rollback to ACTIVE
# ---------------------------------------------------------------------------

def _restore_to_active(db: Session, c: Commitment, source: EditSource) -> None:
    """Common path for decline / cancel / expire: roll the commitment out of
    the ON_HOLD limbo back to its prior live state — ACTIVE in the simple
    case, REASSIGNED if Bob was the owner re-reassigning to Carol and
    Carol declined. Then re-arm a ping.
    """
    if c.state in (CommitmentState.ON_HOLD, CommitmentState.REASSIGNED):
        target = c.prior_state or CommitmentState.ACTIVE
        _log(db, c, source, "state", c.state.value, target.value)
        c.state = target
        c.prior_state = None
        c.on_hold_resume_at = None
        _bump(c, source)
    level = db.get(PriorityLevel, c.priority_level_id) if c.priority_level_id else None
    db.flush()
    ping_svc.ensure_pending_ping(db, c, level)
