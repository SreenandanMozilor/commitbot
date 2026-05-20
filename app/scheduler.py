"""
Background scheduler.

Seven recurring jobs:

  1. `process_due_pings`           — every 60s. Finds Ping rows where
     scheduled_for <= now and sent_at IS NULL, delivers them (or logs in
     dry-run), and schedules the next ping using the cadence calculator.
     Includes both ACTIVE and REASSIGNED commitments (the two live states).

  2. `purge_bin`                   — every hour. Hard-deletes commitments
     soft-deleted >48h ago.

  3. `auto_resume_on_hold`         — every 5 min. Returns On-Hold commitments
     to Active when their resume_at has passed. Skips ON_HOLD rows that are
     actually in reassignment-limbo.

  4. `expire_reassignments`        — every 5 min. Flips PENDING reassignments
     past their 24h window to EXPIRED and rolls the commitment back to ACTIVE.

  5. `auto_delete_old_completed`   — every hour. Per-user retention sweep
     for COMPLETE commitments: hard-delete if older than X days, or archive
     them all if X==0.

  6. `scan_for_commitments`        — every AGENT_SCAN_INTERVAL_MINUTES.
     For every user with `agent_enabled`, drains their buffered messages
     through the LLM classifier and persists high-confidence captures.

  7. `prune_agent_buffer`          — daily. Drops buffered messages older
     than AGENT_BUFFER_RETENTION_DAYS so we don't keep raw message text
     beyond what the agent needs.

Runs in-process via APScheduler's `BackgroundScheduler` (thread pool). The
sync SQLAlchemy code is happy in threads, and `BackgroundScheduler` keeps the
async FastAPI event loop free.

For production scale, swap to a Celery/RQ worker pulling from a queue. The
domain code in `services/` doesn't depend on the scheduler so the move is
mechanical.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.models import Commitment, CommitmentState, Ping, PriorityLevel, User
from app.services import pings as ping_svc
from app.services import reassignments as reassign_svc

log = logging.getLogger(__name__)
settings = get_settings()

scheduler = BackgroundScheduler(timezone="UTC")

# The Slack web client. Injected by `start_scheduler()` so the scheduler can
# deliver pings without importing slack_app directly (avoids an import cycle
# at module load time).
_slack_client: Optional[Any] = None


def _get_client() -> Optional[Any]:
    """Return the injected client or None (caller falls back to dry-run logging)."""
    return _slack_client


def process_due_pings() -> None:
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        due = db.execute(
            select(Ping)
            .where(Ping.sent_at.is_(None), Ping.scheduled_for <= now)
            .limit(100)
        ).scalars().all()

        _live = (CommitmentState.ACTIVE, CommitmentState.REASSIGNED)
        for p in due:
            c = db.get(Commitment, p.commitment_id)
            # Skip pings whose commitment has moved out of a live state.
            # REASSIGNED is included — after Bob accepts, the commitment is
            # being worked on under him and must keep pinging.
            if c is None or c.state not in _live:
                p.sent_at = now  # mark consumed so it doesn't re-fire
                continue

            level = db.get(PriorityLevel, c.priority_level_id) if c.priority_level_id else None

            # Respect global pause without sending — but DO schedule the next
            # ping so the queue stays primed. Otherwise unpausing leaves the
            # user with no queued pings until they manually edit a commitment.
            if c.user.global_pause:
                p.sent_at = now
                db.flush()
                next_at = ping_svc.compute_next_ping_at(
                    c, level, last_ping_at=p.sent_at, db=db,
                )
                if next_at is not None:
                    db.add(Ping(commitment_id=c.id, scheduled_for=next_at))
                continue

            ping_svc.deliver_ping(db, p, c, slack_client=_get_client())

            # Flush so the just-sent ping is visible to the count query inside
            # compute_next_ping_at — otherwise `stages` is off by one and the
            # cadence accelerates one step later than the priority configures.
            db.flush()

            # Schedule the next ping based on this one's actual send time.
            next_at = ping_svc.compute_next_ping_at(
                c, level, last_ping_at=p.sent_at, db=db,
            )
            if next_at is not None:
                db.add(Ping(commitment_id=c.id, scheduled_for=next_at))


def purge_bin() -> None:
    """Hard-delete commitments soft-deleted more than 48 hours ago."""
    from app.services import commitments as commit_svc
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    with session_scope() as db:
        stale = db.execute(
            select(Commitment).where(
                Commitment.state == CommitmentState.DELETED,
                Commitment.deleted_at <= cutoff,
            )
        ).scalars().all()
        for c in stale:
            log.info("Purging commitment %s from Bin (deleted at %s)", c.id, c.deleted_at)
            commit_svc.hard_delete(db, c)


def auto_delete_old_completed() -> None:
    """
    Sweep: act on each user's COMPLETE commitments per their retention.

      X > 0 → hard-delete (row removed from the DB) any commitment whose
              completed_at is older than X days. The original "auto-delete"
              intent: actually shed data.
      X = 0 → archive ALL their COMPLETE commitments instead. Same broom,
              gentler outcome — items move to the ARCHIVED tab and stay
              forever. The "safe default" for users who never want their
              history thrown away.

    Runs hourly; the cadence isn't load-bearing since this only catches
    rows the user already finished.
    """
    from app.models import EditSource, User
    from app.services import commitments as commit_svc

    now = datetime.now(timezone.utc)
    with session_scope() as db:
        users = db.execute(select(User)).scalars().all()
        for u in users:
            days = u.auto_delete_completed_after_days
            if days > 0:
                cutoff = now - timedelta(days=days)
                stale = db.execute(
                    select(Commitment).where(
                        Commitment.user_id == u.id,
                        Commitment.state == CommitmentState.COMPLETE,
                        Commitment.completed_at.is_not(None),
                        Commitment.completed_at <= cutoff,
                    )
                ).scalars().all()
                for c in stale:
                    log.info(
                        "Auto-deleting commitment %s (completed_at=%s, threshold=%dd)",
                        c.id, c.completed_at, days,
                    )
                    db.delete(c)
            else:
                # X == 0 → archive instead. We sweep regardless of age:
                # the user has opted into "keep my Complete tab tidy, but
                # don't actually destroy anything." Within at-most-one-hour
                # of a commitment being marked done, it lands in Archived.
                stale = db.execute(
                    select(Commitment).where(
                        Commitment.user_id == u.id,
                        Commitment.state == CommitmentState.COMPLETE,
                    )
                ).scalars().all()
                for c in stale:
                    try:
                        commit_svc.archive(db, c, source=EditSource.DASHBOARD)
                    except ValueError:
                        log.warning("auto-archive refused for %s", c.id)


def expire_reassignments() -> None:
    """
    Flip PENDING reassignments past their 24h window to EXPIRED, restore
    commitments to ACTIVE under their original owner, and DM both parties:
      - The original owner is told the recipient never responded.
      - The recipient's pending DM (if we kept the message_ts) is rewritten
        to retire the buttons.
    """
    with session_scope() as db:
        expired = reassign_svc.expire_due(db)
        # Gather the info needed for post-commit Slack notifications before
        # the session goes away — pulling these inside the with-block keeps
        # the data alive after detach.
        notices: list[dict] = []
        for r in expired:
            c = db.get(Commitment, r.commitment_id)
            owner = db.get(User, r.from_user_id) if r.from_user_id else None
            notices.append({
                "reassignment_id": r.id,
                "to_slack_user_id": r.to_slack_user_id,
                "notice_channel_id": r.notice_channel_id,
                "notice_message_ts": r.notice_message_ts,
                "owner_slack_user_id": owner.slack_user_id if owner else None,
                "owner_team_id": owner.workspace.slack_team_id if owner else None,
                "commitment_text": c.text if c else "(deleted)",
            })

    client = _get_client()
    if not client or settings.dry_run_pings:
        for n in notices:
            log.info("Reassignment expired [dry-run]: %s", n)
        return

    # Side-effecting Slack calls outside the DB transaction. Best-effort.
    from app.slack_app import (
        notify_reassignment_expired_owner,
        retire_reassignment_dm,
        _refresh_home,
    )
    for n in notices:
        try:
            retire_reassignment_dm(
                client,
                channel=n["notice_channel_id"],
                ts=n["notice_message_ts"],
                final_text=(
                    f":hourglass: This reassignment of *{n['commitment_text']}* "
                    "expired without a response."
                ),
            )
        except Exception:
            log.exception("retire_reassignment_dm failed for %s", n["reassignment_id"])
        try:
            if n["owner_slack_user_id"]:
                notify_reassignment_expired_owner(
                    client,
                    owner_slack_user_id=n["owner_slack_user_id"],
                    commitment_text=n["commitment_text"],
                    target_slack_user_id=n["to_slack_user_id"],
                )
        except Exception:
            log.exception("notify owner of expiry failed for %s", n["reassignment_id"])
        try:
            if n["owner_team_id"] and n["owner_slack_user_id"]:
                _refresh_home(
                    client,
                    team_id=n["owner_team_id"],
                    slack_user_id=n["owner_slack_user_id"],
                )
            if n["to_slack_user_id"] and n["owner_team_id"]:
                _refresh_home(
                    client, team_id=n["owner_team_id"],
                    slack_user_id=n["to_slack_user_id"],
                )
        except Exception:
            log.exception("home refresh after expiry failed")


def auto_resume_on_hold() -> None:
    """
    Wake ON_HOLD commitments back up under two independent triggers:

      1. **Explicit resume_at**: the user picked "Snooze 2h" or
         "Snooze tomorrow"; we resume when that time arrives.
      2. **Deadline approaching**: per-user
         `auto_resume_hours_before_deadline` (default 24h). If a held
         commitment's deadline is closer than that many hours, we resume
         it so the user isn't silently caught out by a deadline they'd
         shelved. Set to 0 in settings to disable.

    Both triggers respect prior_state (REASSIGNED stays REASSIGNED).
    Skips reassignment limbo — those have their own 24h timer.
    """
    from app.models import Reassignment, ReassignmentStatus, User
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        # Trigger 1 — explicit resume_at past.
        by_resume_at = db.execute(
            select(Commitment).where(
                Commitment.state == CommitmentState.ON_HOLD,
                Commitment.on_hold_resume_at.is_not(None),
                Commitment.on_hold_resume_at <= now,
            )
        ).scalars().all()

        # Trigger 2 — deadline within the user's auto-resume window.
        # We can't put the per-user `auto_resume_hours_before_deadline`
        # inside an SQL comparison cleanly (it differs by row), so we
        # filter in Python after a cheap shortlist query.
        candidates = db.execute(
            select(Commitment).join(User, User.id == Commitment.user_id).where(
                Commitment.state == CommitmentState.ON_HOLD,
                Commitment.deadline.is_not(None),
                Commitment.deadline > now,  # haven't blown past it yet
                User.auto_resume_hours_before_deadline > 0,
            )
        ).scalars().all()
        by_deadline = []
        for c in candidates:
            hours = c.user.auto_resume_hours_before_deadline
            # SQLite gives back naive datetimes — coerce to UTC so the
            # comparison with `now` (aware) is well-defined.
            dl = c.deadline if c.deadline.tzinfo else c.deadline.replace(tzinfo=timezone.utc)
            if dl > now and dl - timedelta(hours=hours) <= now:
                by_deadline.append(c)

        # Dedupe (a commitment can match both triggers).
        seen_ids: set[str] = set()
        ready: list[Commitment] = []
        for c in (*by_resume_at, *by_deadline):
            if c.id in seen_ids:
                continue
            seen_ids.add(c.id)
            ready.append(c)

        for c in ready:
            # Defensive: skip anything in reassignment limbo. The request
            # flow nulls resume_at and has its own 24h timer, but
            # deadline-based resume could pick it up — explicitly skip.
            has_pending = db.execute(
                select(Reassignment.id).where(
                    Reassignment.commitment_id == c.id,
                    Reassignment.status == ReassignmentStatus.PENDING,
                ).limit(1)
            ).first() is not None
            if has_pending:
                continue
            c.state = c.prior_state or CommitmentState.ACTIVE
            c.prior_state = None
            c.on_hold_resume_at = None
            level = db.get(PriorityLevel, c.priority_level_id) if c.priority_level_id else None
            ping_svc.ensure_pending_ping(db, c, level)


def scan_for_commitments() -> None:
    """Drain every opted-in user's buffer through the LLM classifier.

    Side effects beyond persistence (refreshing Home tabs for users with
    fresh captures) are handled here so the agent service stays
    Slack-ignorant. Best-effort: a Home refresh failure for one user
    never blocks another.
    """
    from app.services import agent as agent_svc

    with session_scope() as db:
        results = agent_svc.scan_all(db)
        # Gather (team_id, slack_user_id) pairs to refresh while the rows
        # are still attached.
        refresh_targets: list[tuple[str, str]] = []
        for user_id, created in results.items():
            if not created:
                continue
            owner = db.get(User, user_id)
            if owner is None or owner.workspace is None:
                continue
            refresh_targets.append(
                (owner.workspace.slack_team_id, owner.slack_user_id),
            )

    if not refresh_targets:
        return
    client = _get_client()
    if not client or settings.dry_run_pings:
        log.info("agent scan refresh [dry-run]: %d users", len(refresh_targets))
        return
    from app.slack_app import _refresh_home
    for team_id, slack_user_id in refresh_targets:
        try:
            _refresh_home(client, team_id=team_id, slack_user_id=slack_user_id)
        except Exception:
            log.exception(
                "Home refresh after agent scan failed for %s/%s",
                team_id, slack_user_id,
            )


def prune_agent_buffer() -> None:
    """Drop agent message buffer rows older than the retention window."""
    from app.services import agent as agent_svc

    with session_scope() as db:
        removed = agent_svc.prune_buffer(db)
        if removed:
            log.info("agent buffer prune: removed %d rows", removed)


def start_scheduler(slack_client: Optional[Any] = None) -> None:
    """
    Start the scheduler. Pass a slack_bolt WebClient (or equivalent) so the
    ping job can call chat.postMessage. If None, deliver_ping falls through
    to its dry-run path regardless of DRY_RUN_PINGS.
    """
    global _slack_client
    _slack_client = slack_client

    if scheduler.running:
        return
    scheduler.add_job(process_due_pings, "interval", seconds=60, id="process_due_pings", replace_existing=True)
    scheduler.add_job(purge_bin, "interval", hours=1, id="purge_bin", replace_existing=True)
    scheduler.add_job(auto_resume_on_hold, "interval", minutes=5, id="auto_resume", replace_existing=True)
    scheduler.add_job(expire_reassignments, "interval", minutes=5, id="expire_reassignments", replace_existing=True)
    scheduler.add_job(auto_delete_old_completed, "interval", hours=1, id="auto_delete_completed", replace_existing=True)

    # Agentic capture. Floor the interval at 1 minute so a misconfigured
    # AGENT_SCAN_INTERVAL_MINUTES=0 doesn't tight-loop the scheduler.
    scan_interval = max(1, settings.agent_scan_interval_minutes)
    scheduler.add_job(
        scan_for_commitments, "interval", minutes=scan_interval,
        id="agent_scan", replace_existing=True,
    )
    scheduler.add_job(
        prune_agent_buffer, "interval", hours=24,
        id="agent_prune_buffer", replace_existing=True,
    )
    scheduler.start()
    log.info("Scheduler started (dry_run=%s, client=%s, agent_dry_run=%s)",
             settings.dry_run_pings, "yes" if _slack_client else "no",
             settings.agent_dry_run)


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
