"""
Ping scheduling + delivery.

`compute_next_ping_at` is the cadence calculator: takes a commitment + its
priority level and returns when the next ping should fire. The scheduler in
`scheduler.py` calls this on a loop.

Delivery is split out so dry-run mode (`DRY_RUN_PINGS=true`) can route to
stdout without touching Slack — essential for local dev before a workspace
is wired up.

Escalation
----------
Pre-escalation: ping at `base_ping_interval_minutes` cadence.
Inside the escalation window (now >= deadline - escalation_trigger_hours):
  interval = base / (rate ** stages)
where `stages` is the count of pings already delivered inside the window
(from the DB, not estimated). The interval is floored at
`max_ping_frequency_minutes`.

Overdue (B4): if `deadline < now`, the escalation window is in the past so
`stages` could grow without bound and `rate ** stages` overflow. We clamp:
once overdue, we ping at the floor cadence — no faster, no slower.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Commitment, CommitmentState, Ping, PingType, PriorityLevel

log = logging.getLogger(__name__)
settings = get_settings()

# Cap on stages so `rate ** stages` can't blow up. With rate=2 and cap=32 the
# divisor is 4 billion, more than enough to floor any sane interval.
_MAX_STAGES = 32

# Absolute system floor — no priority level (even with escalation maxed out)
# may ping more frequently than this. Defense-in-depth on top of the per-level
# `max_ping_frequency_minutes`, which is also validated to be >= 1 at creation.
SYSTEM_MIN_PING_INTERVAL_MINUTES = 1


def _effective_floor_minutes(level: PriorityLevel) -> int:
    """Return the binding lower bound on the ping interval for this level."""
    return max(level.max_ping_frequency_minutes, SYSTEM_MIN_PING_INTERVAL_MINUTES)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Defensive: treat a naive datetime as UTC."""
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)


def _count_sent_pings_since(db: Session, commitment_id: str, since: datetime) -> int:
    return db.execute(
        select(func.count(Ping.id)).where(
            Ping.commitment_id == commitment_id,
            Ping.sent_at.is_not(None),
            Ping.sent_at >= since,
        )
    ).scalar_one()


def compute_next_ping_at(
    c: Commitment,
    level: Optional[PriorityLevel],
    *,
    last_ping_at: Optional[datetime],
    now: Optional[datetime] = None,
    db: Optional[Session] = None,
) -> Optional[datetime]:
    """
    Decide when the next ping should fire for a commitment.

    Rules:
      - Non-Active (On-Hold / Complete / Archived / Deleted / Reassigned): no ping.
      - No priority level resolvable: skip.
      - No deadline: ping at base interval.
      - Deadline already passed (B4): ping at the floor cadence (overdue mode).
      - Inside escalation window: interval = base / (rate ** stages), floored.
      - Outside the window or escalation disabled: base cadence.
    """
    now = _aware(now) or datetime.now(timezone.utc)
    last_ping_at = _aware(last_ping_at)

    # REASSIGNED is a live state — the new owner is working on it just like
    # ACTIVE. Both states ping; everything else (ON_HOLD limbo, COMPLETE,
    # ARCHIVED, DELETED) is silent.
    if c.state not in (CommitmentState.ACTIVE, CommitmentState.REASSIGNED):
        return None
    if level is None:
        return None

    base = timedelta(minutes=level.base_ping_interval_minutes)
    floor = timedelta(minutes=_effective_floor_minutes(level))

    deadline = _aware(c.deadline)

    # No deadline → just the base cadence forever.
    if deadline is None:
        return (last_ping_at or now) + base

    # Escalation disabled is a user-level "leave me alone" — it must win
    # over every escalation-flavoured shortcut below, *including* the
    # overdue branch. Otherwise pressing "Stop escalation" on a late
    # commitment would do nothing.
    if not c.escalation_enabled:
        return (last_ping_at or now) + base

    # B4: overdue. Don't try to compute "stages since the escalation window
    # opened" because that window may have started days ago. Just ping at
    # the floor cadence until the user does something.
    if deadline < now:
        return (last_ping_at or now) + floor

    escalation_starts_at = deadline - timedelta(hours=level.escalation_trigger_hours_before_deadline)

    if now < escalation_starts_at:
        return (last_ping_at or now) + base

    # Inside escalation: count actual sent pings to compute the stage.
    if db is not None:
        stages = _count_sent_pings_since(db, c.id, escalation_starts_at)
    else:
        stages = 0

    # Clamp stages to avoid (rate ** stages) overflow for very large stages.
    stages = min(stages, _MAX_STAGES)
    # Clamp rate to >= 1.0 so a misconfigured row can never *decelerate*
    # pings (rate < 1 would make `base / rate^stages` grow with stages,
    # which inverts the whole point of escalation).
    rate = max(level.escalation_rate, 1.0)
    interval = base / (rate ** stages)
    if interval < floor:
        interval = floor

    return (last_ping_at or now) + interval


def schedule_initial_ping(db: Session, c: Commitment, level: Optional[PriorityLevel]) -> Optional[Ping]:
    next_at = compute_next_ping_at(c, level, last_ping_at=None, db=db)
    if next_at is None:
        return None
    p = Ping(commitment_id=c.id, scheduled_for=next_at, type=PingType.BASE)
    db.add(p)
    db.flush()
    return p


def ensure_pending_ping(db: Session, c: Commitment, level: Optional[PriorityLevel]) -> Optional[Ping]:
    """
    Make sure an Active commitment has at least one unsent ping queued.

    Used after a resume (manual or automatic): while On-Hold, due pings get
    marked consumed without rescheduling, so a resumed commitment can end up
    with an empty ping queue and go silent. This re-arms it without
    duplicating an already-queued future ping.
    """
    existing = db.execute(
        select(Ping.id).where(Ping.commitment_id == c.id, Ping.sent_at.is_(None)).limit(1)
    ).first()
    if existing is not None:
        return None
    return schedule_initial_ping(db, c, level)


def current_ping_interval_minutes(
    c: Commitment,
    level: Optional[PriorityLevel],
    *,
    db: Optional[Session] = None,
    now: Optional[datetime] = None,
) -> Optional[float]:
    """
    Effective minutes between pings *right now* for this commitment. Returns
    None when no pings are being sent (inactive, paused, no priority).
    Mirrors the logic in `compute_next_ping_at` but yields just the interval
    so the UI can render "Pinging every X" without scheduling anything.
    """
    if c.state not in (CommitmentState.ACTIVE, CommitmentState.REASSIGNED) \
            or level is None:
        return None
    if c.user is not None and c.user.global_pause:
        return None

    now = _aware(now) or datetime.now(timezone.utc)
    base = float(level.base_ping_interval_minutes)
    floor = float(_effective_floor_minutes(level))
    deadline = _aware(c.deadline)

    if deadline is None:
        return base
    # See compute_next_ping_at: escalation disabled trumps overdue.
    if not c.escalation_enabled:
        return base
    if deadline < now:
        return floor
    escalation_starts_at = deadline - timedelta(
        hours=level.escalation_trigger_hours_before_deadline,
    )
    if now < escalation_starts_at:
        return base

    if db is not None:
        stages = _count_sent_pings_since(db, c.id, escalation_starts_at)
    else:
        stages = 0
    stages = min(stages, _MAX_STAGES)
    rate = max(level.escalation_rate, 1.0)
    interval = base / (rate ** stages)
    return max(interval, floor)


def is_at_escalation_floor(
    c: Commitment,
    level: Optional[PriorityLevel],
    *,
    db: Optional[Session] = None,
    now: Optional[datetime] = None,
) -> bool:
    """True iff escalation is currently active and can't accelerate further."""
    if level is None or not c.escalation_enabled or c.deadline is None:
        return False
    interval = current_ping_interval_minutes(c, level, db=db, now=now)
    if interval is None:
        return False
    floor = _effective_floor_minutes(level)
    # Floor is binding — escalation has nothing left to accelerate.
    return interval <= floor + 1e-6


def format_interval(minutes: Optional[float]) -> str:
    """Human-friendly cadence label. Used by the dashboard and Slack home."""
    if minutes is None:
        return "—"
    if minutes < 1:
        return "every <1m"
    if minutes < 60:
        return f"every {int(round(minutes))}m"
    hours = minutes / 60
    if hours < 24:
        if abs(hours - round(hours)) < 0.05:
            return f"every {int(round(hours))}h"
        return f"every {hours:.1f}h"
    days = hours / 24
    if abs(days - round(days)) < 0.05:
        return f"every {int(round(days))}d"
    return f"every {days:.1f}d"


def reschedule_next_ping(
    db: Session, c: Commitment, level: Optional[PriorityLevel],
) -> Optional[Ping]:
    """
    Drop any unsent pings for this commitment and queue a fresh one using the
    current priority/deadline. Call this after the user changes a commitment's
    priority level or deadline — otherwise the previously-queued ping reflects
    the old cadence (e.g. a 4h ping when the user just dialled the cadence
    down to 1m).
    """
    if c.state not in (CommitmentState.ACTIVE, CommitmentState.REASSIGNED):
        # Pending pings on non-live commitments are harmless and get consumed
        # by the scheduler; don't re-arm them here.
        return None
    db.execute(
        Ping.__table__.delete().where(
            Ping.commitment_id == c.id, Ping.sent_at.is_(None),
        )
    )
    return schedule_initial_ping(db, c, level)


def deliver_ping(db: Session, p: Ping, c: Commitment, slack_client: Optional[Any] = None) -> None:
    """Deliver a single ping. Routes to Slack DM unless dry-run or no client."""
    p.sent_at = datetime.now(timezone.utc)

    if settings.dry_run_pings or slack_client is None:
        log.info(
            "PING [dry-run] user=%s commitment=%s text=%r deadline=%s",
            c.user_id, c.id, c.text[:60], c.deadline,
        )
        return

    try:
        from app.slack_app import send_ping_dm
        send_ping_dm(slack_client, user_id=c.user.slack_user_id, commitment=c, db=db)
    except Exception:
        log.exception("Failed to deliver ping for commitment %s", c.id)
