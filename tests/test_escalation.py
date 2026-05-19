"""
Exhaustive escalation-system tests.

`test_services.py` already covers a few cadence cases. This file is the
escalation-specific audit — every branch of `compute_next_ping_at` and its
sibling `current_ping_interval_minutes`, plus the helper utilities
(`is_at_escalation_floor`, `reschedule_next_ping`, `format_interval`).

What we're checking, branch by branch:

1.  Non-live states never ping (ON_HOLD / COMPLETE / ARCHIVED / DELETED).
2.  REASSIGNED *does* ping like ACTIVE (post-acceptance live state).
3.  Missing priority → None.
4.  No deadline → base cadence, forever.
5.  Outside the escalation window → base.
6.  Boundary at the escalation window edge — exactly at start → escalates.
7.  Inside the window, stages 0..N produce base / rate^stages.
8.  Floor clamps interval correctly.
9.  `_MAX_STAGES` clamp prevents overflow at absurd stage counts.
10. SYSTEM_MIN_PING_INTERVAL_MINUTES is enforced even if level's floor is < 1.
11. `escalation_rate <= 1.0` doesn't decelerate pings (defensive guard).
12. Past deadline → uses base cadence WHEN escalation is disabled (so the
    Stop-escalation button actually slows things down even when overdue).
13. Past deadline + escalation_enabled → floor cadence.
14. `is_at_escalation_floor` only fires when both conditions hold.
15. `current_ping_interval_minutes` matches `compute_next_ping_at`'s interval.
16. `reschedule_next_ping` drops pending pings and queues a fresh one.
17. `format_interval` renders sensibly across the m/h/d ranges.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture(scope="function")
def db_session():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp.name}"
    os.environ["DRY_RUN_PINGS"] = "true"
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test"
    os.environ["SLACK_SIGNING_SECRET"] = "test-secret"

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    for mod_name in list(sys.modules):
        if mod_name == "app" or mod_name.startswith("app."):
            del sys.modules[mod_name]
    from app import config
    config.get_settings.cache_clear()

    from app.db import Base, SessionLocal, engine
    from app import models  # noqa: F401
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def env(db_session):
    """A workspace, an onboarded user, and a tunable priority level.

    Default values are chosen so escalation behaviour is easy to reason about:
      base = 60m, floor = 10m, rate = 2.0, escalation window = 24h.
    """
    from app.models import PriorityLevel, User, Workspace
    ws = Workspace(slack_team_id="T", bot_token="xoxb")
    db_session.add(ws); db_session.flush()
    u = User(
        workspace_id=ws.id, slack_user_id="U",
        signed_in_at=datetime.now(timezone.utc),
    )
    db_session.add(u); db_session.flush()
    p = PriorityLevel(
        user_id=u.id, name="Std",
        base_ping_interval_minutes=60,
        escalation_trigger_hours_before_deadline=24,
        max_ping_frequency_minutes=10,
        escalation_rate=2.0,
        is_system_default=True,
    )
    db_session.add(p); db_session.commit()
    return {"db": db_session, "ws": ws, "user": u, "pri": p}


def _make(env, *, deadline=None, state=None, escalation_enabled=True):
    """Create an ACTIVE commitment with the env priority."""
    from app.models import CaptureSource, Commitment, CommitmentState
    db = env["db"]
    c = Commitment(
        user_id=env["user"].id, workspace_id=env["user"].workspace_id,
        text="x", source=CaptureSource.SLASH_COMMAND,
        priority_level_id=env["pri"].id,
        deadline=deadline,
        state=state or CommitmentState.ACTIVE,
        escalation_enabled=escalation_enabled,
    )
    db.add(c); db.flush(); db.commit()
    return c


# ---------------------------------------------------------------------------
# 1-3 — silent states + missing inputs
# ---------------------------------------------------------------------------

class TestSilentStates:
    @pytest.mark.parametrize("state_name", [
        "ON_HOLD", "COMPLETE", "ARCHIVED", "DELETED",
    ])
    def test_non_live_states_do_not_ping(self, env, state_name):
        from app.models import CommitmentState
        from app.services.pings import compute_next_ping_at
        c = _make(env, state=getattr(CommitmentState, state_name))
        assert compute_next_ping_at(
            c, env["pri"], last_ping_at=None, db=env["db"],
        ) is None

    def test_REASSIGNED_pings_like_active(self, env):
        """Bob's accepted commitment is in REASSIGNED — must keep pinging."""
        from app.models import CommitmentState
        from app.services.pings import compute_next_ping_at
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        c = _make(env, state=CommitmentState.REASSIGNED)
        nxt = compute_next_ping_at(
            c, env["pri"], last_ping_at=None, now=now, db=env["db"],
        )
        assert nxt == now + timedelta(minutes=60)  # base, no deadline

    def test_missing_level_silences(self, env):
        from app.services.pings import compute_next_ping_at
        c = _make(env)
        assert compute_next_ping_at(
            c, None, last_ping_at=None, db=env["db"],
        ) is None


# ---------------------------------------------------------------------------
# 4-7 — pre-escalation + window boundary
# ---------------------------------------------------------------------------

class TestPreEscalation:
    def test_no_deadline_uses_base(self, env):
        from app.services.pings import compute_next_ping_at
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        c = _make(env)
        assert compute_next_ping_at(
            c, env["pri"], last_ping_at=None, now=now, db=env["db"],
        ) == now + timedelta(minutes=60)

    def test_outside_escalation_window_uses_base(self, env):
        from app.services.pings import compute_next_ping_at
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        c = _make(env, deadline=now + timedelta(days=3))
        assert compute_next_ping_at(
            c, env["pri"], last_ping_at=None, now=now, db=env["db"],
        ) == now + timedelta(minutes=60)

    def test_window_start_is_inclusive(self, env):
        """At exactly `deadline - 24h`, escalation kicks in (stages=0 → base)."""
        from app.services.pings import compute_next_ping_at
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        # Deadline exactly 24h away → escalation_starts_at == now.
        c = _make(env, deadline=now + timedelta(hours=24))
        nxt = compute_next_ping_at(
            c, env["pri"], last_ping_at=None, now=now, db=env["db"],
        )
        # Inside the window with stages=0 yields base — same as outside, but
        # via a different code path. The next ping would accelerate.
        assert nxt == now + timedelta(minutes=60)


# ---------------------------------------------------------------------------
# 7-8 — escalation curve and floor
# ---------------------------------------------------------------------------

class TestEscalationCurve:
    def _insert_sent_pings(self, env, c, when, count):
        """Plant `count` already-sent pings just past the escalation start."""
        from app.models import Ping
        db = env["db"]
        for i in range(count):
            db.add(Ping(
                commitment_id=c.id, scheduled_for=when,
                sent_at=when + timedelta(seconds=i),
            ))
        db.flush()

    def test_stages_progression_base_rate_floor(self, env):
        """rate=2, base=60m, floor=10m:
          stages=0 → 60m, 1 → 30m, 2 → 15m, 3 → 7.5m floored to 10m
        """
        from app.services.pings import compute_next_ping_at
        db, pri = env["db"], env["pri"]
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        deadline = now + timedelta(hours=12)
        escalation_start = deadline - timedelta(hours=24)
        c = _make(env, deadline=deadline)

        assert compute_next_ping_at(
            c, pri, last_ping_at=None, now=now, db=db,
        ) == now + timedelta(minutes=60)

        self._insert_sent_pings(env, c, escalation_start, 1)
        assert compute_next_ping_at(
            c, pri, last_ping_at=now, now=now, db=db,
        ) == now + timedelta(minutes=30)

        self._insert_sent_pings(env, c, escalation_start, 1)
        assert compute_next_ping_at(
            c, pri, last_ping_at=now, now=now, db=db,
        ) == now + timedelta(minutes=15)

        # stages=3 → 60/8 = 7.5 → floored to 10.
        self._insert_sent_pings(env, c, escalation_start, 1)
        assert compute_next_ping_at(
            c, pri, last_ping_at=now, now=now, db=db,
        ) == now + timedelta(minutes=10)

    def test_floor_held_indefinitely(self, env):
        from app.services.pings import compute_next_ping_at
        from app.models import Ping
        db = env["db"]
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        deadline = now + timedelta(hours=1)  # well inside window
        c = _make(env, deadline=deadline)
        escalation_start = deadline - timedelta(hours=24)
        # 20 sent pings → far past floor.
        for i in range(20):
            db.add(Ping(
                commitment_id=c.id, scheduled_for=escalation_start,
                sent_at=escalation_start + timedelta(seconds=i),
            ))
        db.flush()
        nxt = compute_next_ping_at(c, env["pri"], last_ping_at=now, now=now, db=db)
        assert nxt == now + timedelta(minutes=10)  # the floor

    def test_max_stages_clamp_protects_against_overflow(self, env):
        """Pathological: thousands of pings inside the window. Must not
        raise OverflowError on `rate ** stages` and must just return floor.
        """
        from app.services.pings import compute_next_ping_at
        from app.models import Ping
        db = env["db"]
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        deadline = now + timedelta(minutes=30)
        escalation_start = deadline - timedelta(hours=24)
        c = _make(env, deadline=deadline)
        # _MAX_STAGES = 32 in pings.py; jump well past it.
        for i in range(200):
            db.add(Ping(
                commitment_id=c.id, scheduled_for=escalation_start,
                sent_at=escalation_start + timedelta(seconds=i),
            ))
        db.flush()
        # Must not raise; falls back to floor.
        nxt = compute_next_ping_at(c, env["pri"], last_ping_at=now, now=now, db=db)
        assert nxt == now + timedelta(minutes=10)


# ---------------------------------------------------------------------------
# 9-11 — system min, defensive rate
# ---------------------------------------------------------------------------

class TestSystemFloor:
    def test_system_min_enforced_even_below_level_floor(self, env):
        """Defense-in-depth: if somehow a level has max_ping_frequency=0, the
        SYSTEM floor of 1 still applies."""
        from app.services.pings import (
            _effective_floor_minutes, SYSTEM_MIN_PING_INTERVAL_MINUTES,
        )
        env["pri"].max_ping_frequency_minutes = 0   # bypass the validator
        assert _effective_floor_minutes(env["pri"]) == SYSTEM_MIN_PING_INTERVAL_MINUTES

    def test_rate_less_than_one_does_not_decelerate(self, env):
        """If someone wrote a bad rate to the DB (e.g. 0.5), escalation
        should NOT make pings slower than base. With rate clamped to >=1,
        interval = base/1^stages = base for any stages → equal to base.
        """
        from app.models import Ping
        from app.services.pings import compute_next_ping_at
        db = env["db"]
        env["pri"].escalation_rate = 0.5
        db.flush()
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        deadline = now + timedelta(hours=12)
        c = _make(env, deadline=deadline)
        escalation_start = deadline - timedelta(hours=24)
        # Even with 5 sent pings, interval should not exceed base.
        for i in range(5):
            db.add(Ping(
                commitment_id=c.id, scheduled_for=escalation_start,
                sent_at=escalation_start + timedelta(seconds=i),
            ))
        db.flush()
        nxt = compute_next_ping_at(c, env["pri"], last_ping_at=now, now=now, db=db)
        # Must be <= base. (Floor <= computed <= base.)
        assert nxt <= now + timedelta(minutes=60)


# ---------------------------------------------------------------------------
# 12-13 — overdue + escalation_enabled interaction
# ---------------------------------------------------------------------------

class TestOverdue:
    def test_overdue_with_escalation_enabled_uses_floor(self, env):
        from app.services.pings import compute_next_ping_at
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        c = _make(env, deadline=now - timedelta(hours=1))
        nxt = compute_next_ping_at(c, env["pri"], last_ping_at=now, now=now, db=env["db"])
        assert nxt == now + timedelta(minutes=10)  # floor

    def test_overdue_with_escalation_disabled_uses_base(self, env):
        """Stop-escalation button must work even when overdue — otherwise
        pressing it on a late commitment does nothing.
        """
        from app.services.pings import compute_next_ping_at
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        c = _make(
            env, deadline=now - timedelta(hours=1), escalation_enabled=False,
        )
        nxt = compute_next_ping_at(c, env["pri"], last_ping_at=now, now=now, db=env["db"])
        assert nxt == now + timedelta(minutes=60)  # base, not floor


class TestInsideWindowEscalationDisabled:
    def test_inside_window_with_escalation_disabled_uses_base(self, env):
        from app.services.pings import compute_next_ping_at
        from app.models import Ping
        db = env["db"]
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        deadline = now + timedelta(hours=12)
        c = _make(env, deadline=deadline, escalation_enabled=False)
        # Even with sent pings inside the window, base cadence wins.
        escalation_start = deadline - timedelta(hours=24)
        for i in range(3):
            db.add(Ping(
                commitment_id=c.id, scheduled_for=escalation_start,
                sent_at=escalation_start + timedelta(seconds=i),
            ))
        db.flush()
        nxt = compute_next_ping_at(c, env["pri"], last_ping_at=now, now=now, db=db)
        assert nxt == now + timedelta(minutes=60)


# ---------------------------------------------------------------------------
# 14 — is_at_escalation_floor
# ---------------------------------------------------------------------------

class TestIsAtFloor:
    def _saturate(self, env, c):
        from app.models import Ping
        db = env["db"]
        deadline = c.deadline
        escalation_start = deadline - timedelta(hours=24)
        for i in range(10):
            db.add(Ping(
                commitment_id=c.id, scheduled_for=escalation_start,
                sent_at=escalation_start + timedelta(seconds=i),
            ))
        db.flush()

    def test_floor_active_only_when_at_floor_and_enabled(self, env):
        from app.services.pings import is_at_escalation_floor
        db = env["db"]
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        # Fresh commitment in window → not at floor yet.
        c = _make(env, deadline=now + timedelta(hours=12))
        assert is_at_escalation_floor(c, env["pri"], db=db, now=now) is False
        # After saturation → at floor.
        self._saturate(env, c)
        assert is_at_escalation_floor(c, env["pri"], db=db, now=now) is True

    def test_floor_false_when_escalation_disabled(self, env):
        from app.services.pings import is_at_escalation_floor
        db = env["db"]
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        c = _make(
            env, deadline=now + timedelta(hours=12), escalation_enabled=False,
        )
        # Saturate — but escalation is off, so we're "not at floor" (button
        # should show "Resume escalation", not be hidden).
        self._saturate(env, c)
        assert is_at_escalation_floor(c, env["pri"], db=db, now=now) is False


# ---------------------------------------------------------------------------
# 15 — current_ping_interval_minutes parity
# ---------------------------------------------------------------------------

class TestCurrentIntervalParity:
    def test_parity_with_compute_next(self, env):
        """The interval current_ping_interval_minutes reports must equal the
        delta compute_next_ping_at would produce, for the same inputs."""
        from app.models import Ping
        from app.services.pings import (
            current_ping_interval_minutes, compute_next_ping_at,
        )
        db = env["db"]
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        deadline = now + timedelta(hours=12)
        c = _make(env, deadline=deadline)
        escalation_start = deadline - timedelta(hours=24)
        for i in range(2):
            db.add(Ping(
                commitment_id=c.id, scheduled_for=escalation_start,
                sent_at=escalation_start + timedelta(seconds=i),
            ))
        db.flush()

        interval = current_ping_interval_minutes(c, env["pri"], db=db, now=now)
        nxt = compute_next_ping_at(c, env["pri"], last_ping_at=now, now=now, db=db)
        assert nxt - now == timedelta(minutes=interval)


# ---------------------------------------------------------------------------
# 16 — reschedule_next_ping
# ---------------------------------------------------------------------------

class TestReschedule:
    def test_drops_pending_and_queues_one_fresh_ping(self, env):
        from app.models import Ping
        from app.services.pings import schedule_initial_ping, reschedule_next_ping
        db = env["db"]
        c = _make(env)
        schedule_initial_ping(db, c, env["pri"])
        # Add a stale pending ping too, to confirm both get cleared.
        db.add(Ping(commitment_id=c.id,
                    scheduled_for=datetime.now(timezone.utc) + timedelta(days=1)))
        db.flush()
        before = db.query(Ping).filter_by(commitment_id=c.id, sent_at=None).count()
        assert before == 2

        reschedule_next_ping(db, c, env["pri"])
        db.flush()
        after = db.query(Ping).filter_by(commitment_id=c.id, sent_at=None).count()
        assert after == 1

    def test_skips_non_live_states(self, env):
        """A reassigned/active commitment can be rescheduled; an on-hold one
        cannot — it has no business pinging."""
        from app.models import CommitmentState, Ping
        from app.services.pings import reschedule_next_ping
        db = env["db"]
        c = _make(env, state=CommitmentState.ON_HOLD)
        result = reschedule_next_ping(db, c, env["pri"])
        assert result is None
        assert db.query(Ping).filter_by(commitment_id=c.id).count() == 0


# ---------------------------------------------------------------------------
# 17 — format_interval rendering
# ---------------------------------------------------------------------------

class TestProcessDuePings:
    """End-to-end sanity for the scheduler's `process_due_pings` job.

    These guard the most fragile invariants in the ping loop:
      - REASSIGNED commitments get their queued pings DELIVERED, not silently
        consumed (regression after the recent state-model rework).
      - global_pause consumes the due ping AND schedules a next one, so
        unpausing doesn't leave the user with an empty ping queue.
    """

    def _setup_due_ping(self, env, *, state=None):
        from app.models import (
            CaptureSource, Commitment, CommitmentState, Ping, PingType,
        )
        from app.services import pings as ping_svc
        db = env["db"]
        c = Commitment(
            user_id=env["user"].id,
            workspace_id=env["user"].workspace_id,
            text="x", source=CaptureSource.SLASH_COMMAND,
            priority_level_id=env["pri"].id,
            state=state or CommitmentState.ACTIVE,
        )
        db.add(c); db.flush()
        # Plant a ping that's already due.
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        p = Ping(commitment_id=c.id, scheduled_for=past, type=PingType.BASE)
        db.add(p); db.flush()
        db.commit()
        return c, p

    def test_reassigned_commitments_get_pings_delivered(self, env):
        """The state filter must include REASSIGNED. Before the fix, an
        accepted hand-off would have its queued pings marked sent_at without
        ever firing — Bob would silently stop hearing about his commitment."""
        from app.models import CommitmentState
        c, p = self._setup_due_ping(env, state=CommitmentState.REASSIGNED)
        # Run the scheduler job directly. Dry-run mode → no Slack call needed.
        from app.scheduler import process_due_pings
        process_due_pings()
        # Reload to see the result.
        env["db"].refresh(p)
        assert p.sent_at is not None, "ping should have been delivered"
        # And a fresh ping was queued for next time.
        from app.models import Ping
        unsent = env["db"].query(Ping).filter_by(
            commitment_id=c.id, sent_at=None,
        ).count()
        assert unsent == 1, "next ping should be queued"

    def test_global_pause_consumes_and_reschedules(self, env):
        """When global_pause is on, the current ping gets consumed silently
        AND the next ping is queued, so unpausing doesn't leave a black hole."""
        env["user"].global_pause = True
        env["db"].commit()
        c, p = self._setup_due_ping(env)

        from app.scheduler import process_due_pings
        process_due_pings()
        env["db"].refresh(p)
        assert p.sent_at is not None, "paused ping is still consumed"
        from app.models import Ping
        unsent = env["db"].query(Ping).filter_by(
            commitment_id=c.id, sent_at=None,
        ).count()
        assert unsent == 1, "queue must stay primed during pause"


class TestAutoResumeBeforeDeadline:
    """Held commitments must wake up when their deadline gets close, per the
    user's `auto_resume_hours_before_deadline` setting. Otherwise a user
    who put something on hold could miss the deadline silently.
    """

    def test_held_commitment_resumes_when_deadline_inside_window(self, env):
        """User has auto_resume = 24h. A held commitment with a deadline
        20h from now (inside the window) should auto-resume on the sweep."""
        from app.models import CommitmentState, EditSource
        from app.services import commitments as svc
        db = env["db"]
        env["user"].auto_resume_hours_before_deadline = 24
        db.commit()

        c = _make(env, deadline=datetime.now(timezone.utc) + timedelta(hours=20))
        svc.put_on_hold(db, c, resume_at=None, source=EditSource.DASHBOARD)
        db.commit()
        assert c.state == CommitmentState.ON_HOLD

        from app.scheduler import auto_resume_on_hold
        auto_resume_on_hold()
        db.refresh(c)
        assert c.state == CommitmentState.ACTIVE

    def test_held_commitment_stays_when_deadline_outside_window(self, env):
        """Deadline is 3 days away, auto_resume = 24h — should NOT resume."""
        from app.models import CommitmentState, EditSource
        from app.services import commitments as svc
        db = env["db"]
        env["user"].auto_resume_hours_before_deadline = 24
        db.commit()

        c = _make(env, deadline=datetime.now(timezone.utc) + timedelta(days=3))
        svc.put_on_hold(db, c, resume_at=None, source=EditSource.DASHBOARD)
        db.commit()

        from app.scheduler import auto_resume_on_hold
        auto_resume_on_hold()
        db.refresh(c)
        assert c.state == CommitmentState.ON_HOLD

    def test_zero_disables_the_feature(self, env):
        """auto_resume_hours_before_deadline = 0 → don't auto-resume even
        if the deadline is close."""
        from app.models import CommitmentState, EditSource
        from app.services import commitments as svc
        db = env["db"]
        env["user"].auto_resume_hours_before_deadline = 0
        db.commit()

        c = _make(env, deadline=datetime.now(timezone.utc) + timedelta(hours=1))
        svc.put_on_hold(db, c, resume_at=None, source=EditSource.DASHBOARD)
        db.commit()

        from app.scheduler import auto_resume_on_hold
        auto_resume_on_hold()
        db.refresh(c)
        assert c.state == CommitmentState.ON_HOLD

    def test_prior_state_restored_by_deadline_resume(self, env):
        """If a REASSIGNED commitment is held, the deadline-driven resume
        brings it back to REASSIGNED, not ACTIVE."""
        from app.models import CommitmentState, EditSource
        from app.services import commitments as svc
        db = env["db"]
        env["user"].auto_resume_hours_before_deadline = 24
        db.commit()

        c = _make(
            env,
            deadline=datetime.now(timezone.utc) + timedelta(hours=12),
            state=CommitmentState.REASSIGNED,
        )
        svc.put_on_hold(db, c, resume_at=None, source=EditSource.DASHBOARD)
        db.commit()
        assert c.state == CommitmentState.ON_HOLD
        assert c.prior_state == CommitmentState.REASSIGNED

        from app.scheduler import auto_resume_on_hold
        auto_resume_on_hold()
        db.refresh(c)
        assert c.state == CommitmentState.REASSIGNED


class TestFormatInterval:
    @pytest.mark.parametrize("minutes,expected", [
        (None, "—"),
        (0.5, "every <1m"),
        (1, "every 1m"),
        (30, "every 30m"),
        (60, "every 1h"),
        (90, "every 1.5h"),
        (60 * 24, "every 1d"),
        (60 * 24 * 2.5, "every 2.5d"),
    ])
    def test_rendering(self, minutes, expected):
        from app.services.pings import format_interval
        assert format_interval(minutes) == expected
