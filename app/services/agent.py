"""
Agentic commitment-capture orchestration.

End-to-end flow:

  1. `buffer_message`   — called from the Slack `message` event handler when
                          the sender has `agent_enabled=True`. Idempotent: a
                          unique constraint on (user, channel, ts) means
                          retry-storms don't double-buffer.

  2. `scan_user`        — drain an opted-in user's unprocessed buffer in
                          batches, call the LLM classifier, and persist any
                          high-confidence verdicts as real commitments via
                          `commit_svc.create_commitment(source=AGENT)`. Marks
                          every buffer row as processed regardless of verdict
                          so we never re-classify them.

  3. `scan_all`         — fan out `scan_user` across every user with the
                          agent turned on. Runs from the scheduler.

  4. `undo_agent_capture` — hard-delete a fresh AGENT commitment. Bypasses
                          the bin because an agent false-positive shouldn't
                          live in the user's "failed commitments" trail.

  5. `prune_buffer`     — drop buffer rows older than the retention window.

The agent goes through the existing service-layer `create_commitment` so
the audit log (`CommitmentEdit`), dedup (workspace+channel+ts unique
constraint), and ping scheduling all happen by the same code path that
the slash command + notation + shortcut already use.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    AgentMessageBuffer,
    CaptureSource,
    Commitment,
    CommitmentState,
    PriorityLevel,
    User,
)
from app.services import commitments as commit_svc
from app.services import pings as ping_svc
from app.services.llm import (
    ClassifiedCandidate,
    HarvestedMessage,
    LLMProvider,
    get_provider,
)

log = logging.getLogger(__name__)

# Same shape as the slack_app extractor — re-implemented here to avoid an
# import cycle (slack_app already imports this module's callers). Picks up
# Slack's `<@U123>` substitutions and plain `@name` tokens.
_MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]+)?>")
_PLAIN_MENTION_RE = re.compile(r"(?<![\w<])@([A-Za-z][\w\-.]{0,63})")

# Cap how many messages go in one batched classify call. Each verdict
# object is ~80 tokens; AnthropicProvider asks for 4k max_tokens, so 30
# messages leaves comfortable headroom even with verbose rationales.
_MAX_BATCH_SIZE = 30


def _extract_mentions(text: str) -> list[str]:
    text = text or ""
    seen: set[str] = set()
    out: list[str] = []
    for m in _MENTION_RE.finditer(text):
        v = m.group(1)
        if v not in seen:
            seen.add(v)
            out.append(v)
    cleaned = _MENTION_RE.sub(" ", text)
    for m in _PLAIN_MENTION_RE.finditer(cleaned):
        v = m.group(1)
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _parse_deadline_hint(hint: Optional[str]) -> Optional[datetime]:
    """Parse the LLM's optional ISO deadline guess. Returns None for any
    parse failure — we'd rather have no deadline than the wrong one."""
    if not hint or not isinstance(hint, str):
        return None
    try:
        # Accept "2026-05-21T17:00:00Z" → fromisoformat needs the Z swapped.
        normalized = hint.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Guardrail: refuse deadlines in the past or absurdly far in the
    # future. The user can always set one manually if the model's guess
    # was useless.
    now = datetime.now(timezone.utc)
    if dt < now - timedelta(minutes=1):
        return None
    if dt > now + timedelta(days=365):
        return None
    return dt


# ---------------------------------------------------------------------------
# Buffering
# ---------------------------------------------------------------------------

def buffer_message(
    db: Session,
    *,
    user: User,
    channel_id: str,
    message_ts: str,
    text: str,
) -> Optional[AgentMessageBuffer]:
    """Append a message to the agent buffer for later classification.

    Returns the buffered row, or None when we declined to buffer (agent
    off, empty text, duplicate). The Slack handler is fire-and-forget so
    the return value is mostly for tests.
    """
    if not user.agent_enabled:
        return None
    text = (text or "").strip()
    if not text:
        return None
    # Don't bother buffering the bot's own posts — those echo back as
    # bot_message subtypes anyway, but the slack handler already filters
    # them, so this is purely belt-and-braces.
    if text.startswith(":white_check_mark:") or text.startswith(":bookmark_tabs:"):
        return None

    row = AgentMessageBuffer(
        user_id=user.id,
        slack_channel_id=channel_id,
        slack_message_ts=message_ts,
        text=text[:2000],  # cap; LLM truncates per-message anyway
    )
    # Wrap in a SAVEPOINT so a duplicate-key IntegrityError doesn't roll
    # back the outer transaction the slack handler is composing (e.g.
    # the notation-capture commitment it just inserted). Only this insert
    # gets discarded on dup.
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return None
    return row


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _effective_floor(user: User) -> float:
    """Per-user override of the system confidence floor, both clamped."""
    s = get_settings()
    floor = s.agent_confidence_floor
    if user.agent_confidence_floor_pct is not None:
        floor = max(0.0, min(1.0, user.agent_confidence_floor_pct / 100.0))
    return floor


def scan_user(
    db: Session,
    user: User,
    *,
    provider: Optional[LLMProvider] = None,
    limit: int = _MAX_BATCH_SIZE,
) -> list[Commitment]:
    """Classify the user's unprocessed buffer rows and persist verdicts.

    Returns the list of newly-created commitments (may be empty). Buffer
    rows are marked `processed_at` regardless of verdict — negatives leave
    no trace beyond that timestamp.

    Honors `AGENT_DRY_RUN`: still calls the classifier and logs the
    verdicts, but does not write commitments or mark rows processed (so
    you can rerun and observe deterministic behavior in dev).
    """
    settings = get_settings()
    provider = provider or get_provider()

    if not user.agent_enabled:
        return []

    rows = db.execute(
        select(AgentMessageBuffer)
        .where(
            AgentMessageBuffer.user_id == user.id,
            AgentMessageBuffer.processed_at.is_(None),
        )
        .order_by(AgentMessageBuffer.created_at.asc())
        .limit(limit)
    ).scalars().all()
    if not rows:
        return []

    harvested = [
        HarvestedMessage(
            id=r.id, text=r.text,
            sent_at=r.created_at, channel_id=r.slack_channel_id,
        )
        for r in rows
    ]
    log.info(
        "agent scan: user=%s buffer=%d provider=%s dry_run=%s",
        user.id, len(rows), provider.name, settings.agent_dry_run,
    )

    verdicts = provider.classify(harvested)
    by_id: dict[str, ClassifiedCandidate] = {v.message_id: v for v in verdicts}
    floor = _effective_floor(user)

    created: list[Commitment] = []
    now = datetime.now(timezone.utc)
    for r in rows:
        v = by_id.get(r.id)
        if v is None:
            # Provider didn't return a verdict for this row (truncation,
            # parse error). Leave it unprocessed so the next sweep retries.
            log.debug("agent scan: no verdict for buffer row %s", r.id)
            continue

        if settings.agent_dry_run:
            log.info(
                "agent scan [DRY-RUN]: row=%s commit=%s conf=%.2f rationale=%r",
                r.id, v.is_commitment, v.confidence, v.rationale,
            )
            continue

        # Per-row SAVEPOINT: an unexpected failure on row N (a race with a
        # manual scan colliding on the unique (workspace, channel, ts)
        # constraint, a flaky DB) must not roll back rows 0..N-1 in the
        # outer transaction. ValueError from create_commitment is "this
        # row is bad" — handled inline, savepoint commits with
        # processed_at set so we don't re-classify it next sweep.
        try:
            with db.begin_nested():
                r.processed_at = now

                if not v.is_commitment or v.confidence < floor:
                    continue

                recipients = _extract_mentions(r.text)
                deadline = _parse_deadline_hint(v.deadline_hint)

                try:
                    c = commit_svc.create_commitment(
                        db,
                        owner=user,
                        text=r.text,
                        source=CaptureSource.AGENT,
                        slack_channel_id=r.slack_channel_id,
                        slack_message_ts=r.slack_message_ts,
                        deadline=deadline,
                        recipient_slack_user_ids=recipients,
                    )
                except ValueError as e:
                    log.warning("agent capture rejected for row=%s: %s", r.id, e)
                    continue

                # If `create_commitment` returned an existing row (slash-
                # command or notation got there first via the dedup key),
                # don't stomp its provenance. Only stamp agent metadata on
                # fresh AGENT captures.
                if c.source == CaptureSource.AGENT and c.agent_confidence is None:
                    c.agent_confidence = v.confidence
                    c.agent_rationale = (v.rationale or "")[:280]
                    level = (
                        db.get(PriorityLevel, c.priority_level_id)
                        if c.priority_level_id else None
                    )
                    ping_svc.schedule_initial_ping(db, c, level)
                    created.append(c)
        except Exception:
            # Savepoint rolled back — processed_at clear, no commitment
            # written. Row stays unprocessed and the next sweep retries.
            log.exception("agent capture failed unexpectedly for row=%s", r.id)
            continue

    return created


def scan_all(db: Session, *, provider: Optional[LLMProvider] = None) -> dict[str, list[Commitment]]:
    """Run `scan_user` for every user with the agent enabled.

    Returns {user_id: [new_commitments]}. Slack-side side effects (e.g.
    refreshing Home tabs after new captures appear) are the caller's
    responsibility — the service stays Slack-ignorant.
    """
    provider = provider or get_provider()
    users = db.execute(
        select(User).where(User.agent_enabled.is_(True))
    ).scalars().all()
    out: dict[str, list[Commitment]] = {}
    for u in users:
        try:
            created = scan_user(db, u, provider=provider)
        except Exception:
            log.exception("agent scan_user failed for user=%s", u.id)
            continue
        if created:
            out[u.id] = created
    return out


# ---------------------------------------------------------------------------
# Undo + recent-capture surface
# ---------------------------------------------------------------------------

def is_within_undo_window(c: Commitment) -> bool:
    """True if the commitment is a recent AGENT capture eligible for the
    inline 'Undo' affordance. After the window, the user can still soft-
    delete from the dashboard, but the one-click button retires."""
    s = get_settings()
    if c.source != CaptureSource.AGENT:
        return False
    created = c.created_at
    if created is None:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (
        datetime.now(timezone.utc) - created
        <= timedelta(minutes=s.agent_undo_window_minutes)
    )


def recent_agent_captures(
    db: Session,
    *,
    owner: User,
    within_minutes: Optional[int] = None,
) -> list[Commitment]:
    """ACTIVE/REASSIGNED commitments captured by the agent in the recent
    past. Used by Home + dashboard to render the 'Recently auto-captured'
    section with Undo buttons.
    """
    s = get_settings()
    window = within_minutes if within_minutes is not None else s.agent_undo_window_minutes
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window)
    return db.execute(
        select(Commitment).where(
            Commitment.user_id == owner.id,
            Commitment.source == CaptureSource.AGENT,
            Commitment.created_at >= cutoff,
            Commitment.state.in_([CommitmentState.ACTIVE, CommitmentState.REASSIGNED]),
        ).order_by(Commitment.created_at.desc())
    ).scalars().all()


def undo_agent_capture(db: Session, c: Commitment) -> bool:
    """Hard-delete an agent capture. Returns True if removed.

    Rationale: a false-positive isn't a "failed commitment," it's a
    classification error. Soft-deleting would mark it FAILED in outcome
    stats and pollute the user's history. Hard-delete erases it from the
    record — same way `/commit` posts get retracted when text validation
    fails.

    Refuses to undo:
      - non-agent captures (the user means soft_delete instead)
      - agent captures past their undo window (use soft_delete)
    """
    if c.source != CaptureSource.AGENT:
        return False
    if not is_within_undo_window(c):
        return False
    log.info("undoing agent capture %s (confidence=%s)", c.id, c.agent_confidence)
    commit_svc.hard_delete(db, c)
    return True


# ---------------------------------------------------------------------------
# Buffer maintenance
# ---------------------------------------------------------------------------

def prune_buffer(db: Session, *, retention_days: Optional[int] = None) -> int:
    """Delete buffer rows older than the retention window. Returns the
    number of rows removed."""
    s = get_settings()
    days = retention_days if retention_days is not None else s.agent_buffer_retention_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = db.execute(
        delete(AgentMessageBuffer).where(AgentMessageBuffer.created_at < cutoff)
    )
    return result.rowcount or 0


# ---------------------------------------------------------------------------
# Convenience for tests / dashboard counters
# ---------------------------------------------------------------------------

def pending_buffer_count(db: Session, user: User) -> int:
    return db.execute(
        select(func.count(AgentMessageBuffer.id)).where(
            AgentMessageBuffer.user_id == user.id,
            AgentMessageBuffer.processed_at.is_(None),
        )
    ).scalar_one()
