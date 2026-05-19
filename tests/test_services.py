"""
Unit tests for the service layer.

These tests exercise the business rules (state transitions, conflict resolution,
notation validation, ping cadence) directly — no FastAPI, no Slack. They run in
milliseconds and protect against quiet regressions in the rules that the spec
specifically called out.

Covered:
  F8  — manual resume always wins
  F9  — version bumps on every write
  F11 — notation pattern validation
  F13 — slack-message dedup keyed on (workspace, channel, ts)
  Ping cadence — base interval, escalation acceleration, floor
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, time, timezone
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures: in-memory SQLite + a seeded workspace/user/priority
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def db_session():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp.name}"
    os.environ["DRY_RUN_PINGS"] = "true"
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test"
    os.environ["SLACK_SIGNING_SECRET"] = "test-secret"

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    # Force a clean module graph so the new DATABASE_URL takes effect.
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
def seeded(db_session):
    """Return a dict of (db, workspace, user, priority)."""
    from app.models import PriorityLevel, User, Workspace
    ws = Workspace(slack_team_id="T_TEST", bot_token="xoxb")
    db_session.add(ws); db_session.flush()
    user = User(workspace_id=ws.id, slack_user_id="U_TEST")
    db_session.add(user); db_session.flush()
    pri = PriorityLevel(
        user_id=user.id, name="Normal",
        base_ping_interval_minutes=60,
        escalation_trigger_hours_before_deadline=24,
        max_ping_frequency_minutes=10,
        escalation_rate=2.0,
        is_system_default=True,
    )
    db_session.add(pri); db_session.flush()
    db_session.commit()
    return {"db": db_session, "ws": ws, "user": user, "pri": pri}


# ---------------------------------------------------------------------------
# F11 — notation validation
# ---------------------------------------------------------------------------

class TestNotationValidation:
    def test_accepts_brackets(self):
        from app.services.commitments import validate_notation_pattern
        validate_notation_pattern(r"\[\[commit.*\]\]")

    def test_accepts_bang_commit(self):
        from app.services.commitments import validate_notation_pattern
        validate_notation_pattern(r"!commit\s+.+")

    def test_rejects_question_mark_delimiter(self):
        from app.services.commitments import validate_notation_pattern
        with pytest.raises(ValueError, match="not permitted"):
            validate_notation_pattern(r"\?commit.*\?")

    def test_rejects_plain_text_without_delimiter(self):
        from app.services.commitments import validate_notation_pattern
        with pytest.raises(ValueError, match="unambiguous delimiter"):
            validate_notation_pattern(r"will do .+ later")

    def test_rejects_empty(self):
        from app.services.commitments import validate_notation_pattern
        with pytest.raises(ValueError):
            validate_notation_pattern("")

    def test_rejects_overlong(self):
        from app.services.commitments import validate_notation_pattern
        with pytest.raises(ValueError):
            validate_notation_pattern("[[" + "a" * 200 + "]]")

    def test_rejects_invalid_regex(self):
        from app.services.commitments import validate_notation_pattern
        with pytest.raises(ValueError, match="valid regex"):
            validate_notation_pattern("[[unbalanced(")


# ---------------------------------------------------------------------------
# F13 — message dedup
# ---------------------------------------------------------------------------

class TestDedup:
    def test_same_slack_message_does_not_create_two_commitments(self, seeded):
        from app.models import CaptureSource
        from app.services.commitments import create_commitment
        db, user = seeded["db"], seeded["user"]

        c1 = create_commitment(
            db, owner=user, text="ping me later",
            source=CaptureSource.NOTATION,
            slack_channel_id="C1", slack_message_ts="1700000000.000100",
        )
        c2 = create_commitment(
            db, owner=user, text="ping me later",
            source=CaptureSource.NOTATION,
            slack_channel_id="C1", slack_message_ts="1700000000.000100",
        )
        assert c1.id == c2.id

    def test_same_ts_different_channels_are_distinct(self, seeded):
        from app.models import CaptureSource
        from app.services.commitments import create_commitment
        db, user = seeded["db"], seeded["user"]
        c1 = create_commitment(db, owner=user, text="t",
                               source=CaptureSource.NOTATION,
                               slack_channel_id="C1", slack_message_ts="1700.000001")
        c2 = create_commitment(db, owner=user, text="t",
                               source=CaptureSource.NOTATION,
                               slack_channel_id="C2", slack_message_ts="1700.000001")
        assert c1.id != c2.id


# ---------------------------------------------------------------------------
# F9 — version bumps on every write
# ---------------------------------------------------------------------------

class TestVersioning:
    def test_initial_version_is_one(self, seeded):
        from app.models import CaptureSource
        from app.services.commitments import create_commitment
        c = create_commitment(seeded["db"], owner=seeded["user"], text="x",
                              source=CaptureSource.DASHBOARD)
        assert c.version == 1
        assert c.last_writer is None  # only set on subsequent writes

    def test_each_mutation_bumps_version_and_writer(self, seeded):
        from app.models import CaptureSource, EditSource
        from app.services.commitments import (
            create_commitment, edit_text, mark_done, set_deadline,
        )
        db = seeded["db"]
        c = create_commitment(db, owner=seeded["user"], text="old",
                              source=CaptureSource.DASHBOARD)
        edit_text(db, c, "new", source=EditSource.DASHBOARD)
        assert c.version == 2
        assert c.last_writer == "dashboard"

        set_deadline(db, c, datetime.now(timezone.utc) + timedelta(days=1),
                     source=EditSource.SLACK)
        assert c.version == 3
        assert c.last_writer == "slack"

        mark_done(db, c, source=EditSource.DASHBOARD)
        assert c.version == 4

    def test_no_op_edits_do_not_bump_version(self, seeded):
        from app.models import CaptureSource, EditSource
        from app.services.commitments import create_commitment, edit_text
        db = seeded["db"]
        c = create_commitment(db, owner=seeded["user"], text="same",
                              source=CaptureSource.DASHBOARD)
        edit_text(db, c, "same", source=EditSource.DASHBOARD)
        assert c.version == 1


# ---------------------------------------------------------------------------
# F8 — manual resume always wins
# ---------------------------------------------------------------------------

class TestOnHoldPrecedence:
    def test_manual_resume_clears_hold(self, seeded):
        from app.models import CaptureSource, CommitmentState, EditSource
        from app.services.commitments import create_commitment, put_on_hold, resume
        db = seeded["db"]
        c = create_commitment(db, owner=seeded["user"], text="x",
                              source=CaptureSource.DASHBOARD)
        future = datetime.now(timezone.utc) + timedelta(days=7)
        put_on_hold(db, c, resume_at=future, source=EditSource.DASHBOARD)
        assert c.state == CommitmentState.ON_HOLD
        assert c.on_hold_resume_at == future

        resume(db, c, source=EditSource.DASHBOARD, manual=True)
        assert c.state == CommitmentState.ACTIVE
        assert c.on_hold_resume_at is None

    def test_resume_idempotent_when_not_on_hold(self, seeded):
        from app.models import CaptureSource, CommitmentState, EditSource
        from app.services.commitments import create_commitment, resume
        db = seeded["db"]
        c = create_commitment(db, owner=seeded["user"], text="x",
                              source=CaptureSource.DASHBOARD)
        before_version = c.version
        resume(db, c, source=EditSource.DASHBOARD)
        assert c.version == before_version


# ---------------------------------------------------------------------------
# Soft-delete + restore
# ---------------------------------------------------------------------------

class TestBin:
    def test_soft_delete_then_restore_active(self, seeded):
        from app.models import CaptureSource, CommitmentState, EditSource
        from app.services.commitments import create_commitment, restore_from_bin, soft_delete
        db = seeded["db"]
        c = create_commitment(db, owner=seeded["user"], text="x",
                              source=CaptureSource.DASHBOARD)
        soft_delete(db, c, source=EditSource.DASHBOARD)
        assert c.state == CommitmentState.DELETED
        assert c.deleted_at is not None
        restore_from_bin(db, c, source=EditSource.DASHBOARD)
        assert c.state == CommitmentState.ACTIVE
        assert c.deleted_at is None

    def test_restore_completed_returns_to_complete(self, seeded):
        from app.models import CaptureSource, CommitmentState, EditSource
        from app.services.commitments import (
            create_commitment, mark_done, restore_from_bin, soft_delete,
        )
        db = seeded["db"]
        c = create_commitment(db, owner=seeded["user"], text="x",
                              source=CaptureSource.DASHBOARD)
        mark_done(db, c, source=EditSource.DASHBOARD)
        soft_delete(db, c, source=EditSource.DASHBOARD)
        restore_from_bin(db, c, source=EditSource.DASHBOARD)
        assert c.state == CommitmentState.COMPLETE

    def test_archive_requires_complete(self, seeded):
        from app.models import CaptureSource, EditSource
        from app.services.commitments import archive, create_commitment
        db = seeded["db"]
        c = create_commitment(db, owner=seeded["user"], text="x",
                              source=CaptureSource.DASHBOARD)
        with pytest.raises(ValueError):
            archive(db, c, source=EditSource.DASHBOARD)


# ---------------------------------------------------------------------------
# Text & deadline validation
# ---------------------------------------------------------------------------

class TestFieldValidation:
    def test_empty_text_rejected(self, seeded):
        from app.models import CaptureSource
        from app.services.commitments import create_commitment
        db = seeded["db"]
        with pytest.raises(ValueError):
            create_commitment(db, owner=seeded["user"], text="   ",
                              source=CaptureSource.DASHBOARD)

    def test_overlong_text_rejected(self, seeded):
        from app.models import CaptureSource
        from app.services.commitments import create_commitment, MAX_TEXT_LEN
        db = seeded["db"]
        with pytest.raises(ValueError):
            create_commitment(db, owner=seeded["user"], text="x" * (MAX_TEXT_LEN + 1),
                              source=CaptureSource.DASHBOARD)

    def test_setting_naive_deadline_assumes_utc(self, seeded):
        from app.models import CaptureSource, EditSource
        from app.services.commitments import create_commitment, set_deadline
        db = seeded["db"]
        c = create_commitment(db, owner=seeded["user"], text="x",
                              source=CaptureSource.DASHBOARD)
        naive = datetime(2030, 1, 1, 12, 0, 0)
        set_deadline(db, c, naive, source=EditSource.DASHBOARD)
        assert c.deadline is not None
        assert c.deadline.tzinfo is not None


# ---------------------------------------------------------------------------
# Ping cadence
# ---------------------------------------------------------------------------

class TestPingCadence:
    def test_no_deadline_uses_base_interval(self, seeded):
        from app.models import CaptureSource
        from app.services.commitments import create_commitment
        from app.services.pings import compute_next_ping_at
        db, pri = seeded["db"], seeded["pri"]
        c = create_commitment(db, owner=seeded["user"], text="x",
                              source=CaptureSource.DASHBOARD)
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        nxt = compute_next_ping_at(c, pri, last_ping_at=None, now=now, db=db)
        assert nxt == now + timedelta(minutes=pri.base_ping_interval_minutes)

    def test_outside_escalation_window_uses_base(self, seeded):
        from app.models import CaptureSource
        from app.services.commitments import create_commitment
        from app.services.pings import compute_next_ping_at
        db, pri = seeded["db"], seeded["pri"]
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        c = create_commitment(
            db, owner=seeded["user"], text="x",
            source=CaptureSource.DASHBOARD,
            deadline=now + timedelta(days=3),  # well outside 24h window
        )
        nxt = compute_next_ping_at(c, pri, last_ping_at=None, now=now, db=db)
        assert nxt == now + timedelta(minutes=pri.base_ping_interval_minutes)

    def test_inside_escalation_accelerates_then_floors(self, seeded):
        """
        Inside the 24h escalation window with rate=2.0 base=60m floor=10m:
          stages=0 → 60m, stages=1 → 30m, stages=2 → 15m, stages=3 → 10m (floored)
        """
        from app.models import CaptureSource, Ping
        from app.services.commitments import create_commitment
        from app.services.pings import compute_next_ping_at
        db, pri = seeded["db"], seeded["pri"]
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        deadline = now + timedelta(hours=12)  # inside 24h window
        c = create_commitment(
            db, owner=seeded["user"], text="x",
            source=CaptureSource.DASHBOARD, deadline=deadline,
        )
        # stages = 0 (no sent pings yet)
        nxt = compute_next_ping_at(c, pri, last_ping_at=None, now=now, db=db)
        assert nxt == now + timedelta(minutes=60)

        # Insert one sent ping inside the window.
        escalation_start = deadline - timedelta(hours=24)
        db.add(Ping(commitment_id=c.id, scheduled_for=escalation_start,
                    sent_at=escalation_start + timedelta(minutes=1)))
        db.flush()
        nxt = compute_next_ping_at(c, pri, last_ping_at=now, now=now, db=db)
        assert nxt == now + timedelta(minutes=30)

        # Two more — stages=3 → 60/8 = 7.5m → floored to 10m.
        for offset in (2, 3):
            db.add(Ping(commitment_id=c.id, scheduled_for=escalation_start,
                        sent_at=escalation_start + timedelta(minutes=offset)))
        db.flush()
        nxt = compute_next_ping_at(c, pri, last_ping_at=now, now=now, db=db)
        assert nxt == now + timedelta(minutes=10)

    def test_escalation_disabled_keeps_base_cadence(self, seeded):
        from app.models import CaptureSource
        from app.services.commitments import create_commitment
        from app.services.pings import compute_next_ping_at
        db, pri = seeded["db"], seeded["pri"]
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        c = create_commitment(
            db, owner=seeded["user"], text="x",
            source=CaptureSource.DASHBOARD,
            deadline=now + timedelta(hours=2),  # well inside window
        )
        c.escalation_enabled = False
        db.flush()
        nxt = compute_next_ping_at(c, pri, last_ping_at=None, now=now, db=db)
        assert nxt == now + timedelta(minutes=pri.base_ping_interval_minutes)

    def test_non_active_state_returns_no_ping(self, seeded):
        from app.models import CaptureSource, CommitmentState
        from app.services.commitments import create_commitment
        from app.services.pings import compute_next_ping_at
        db, pri = seeded["db"], seeded["pri"]
        c = create_commitment(db, owner=seeded["user"], text="x",
                              source=CaptureSource.DASHBOARD)
        c.state = CommitmentState.ON_HOLD
        assert compute_next_ping_at(c, pri, last_ping_at=None, db=db) is None


# ---------------------------------------------------------------------------
# Priority level CRUD
# ---------------------------------------------------------------------------

class TestPriorityLevels:
    def test_create_validates_intervals(self, seeded):
        from app.services.commitments import create_priority_level
        db, user = seeded["db"], seeded["user"]
        with pytest.raises(ValueError):
            create_priority_level(db, user=user, name="X",
                                  base_ping_interval_minutes=10,
                                  max_ping_frequency_minutes=60)  # floor > base
        with pytest.raises(ValueError):
            create_priority_level(db, user=user, name="X", escalation_rate=0.5)
        with pytest.raises(ValueError):
            create_priority_level(db, user=user, name="")

    def test_cannot_delete_default(self, seeded):
        from app.services.commitments import soft_delete_priority_level
        with pytest.raises(ValueError):
            soft_delete_priority_level(seeded["db"], seeded["pri"])

    def test_soft_delete_sets_deleted_at(self, seeded):
        from app.services.commitments import create_priority_level, soft_delete_priority_level
        new = create_priority_level(seeded["db"], user=seeded["user"], name="Custom")
        soft_delete_priority_level(seeded["db"], new)
        assert new.deleted_at is not None

    def test_soft_delete_repoints_commitments_to_default(self, seeded):
        """B15: deleting a priority should move its commitments to the user's default."""
        from app.models import CaptureSource
        from app.services.commitments import (
            create_commitment, create_priority_level, soft_delete_priority_level,
        )
        db, user, default = seeded["db"], seeded["user"], seeded["pri"]

        urgent = create_priority_level(db, user=user, name="Urgent")
        c = create_commitment(db, owner=user, text="x",
                              source=CaptureSource.DASHBOARD,
                              priority_level_id=urgent.id)
        assert c.priority_level_id == urgent.id

        soft_delete_priority_level(db, urgent)
        db.refresh(c)
        assert c.priority_level_id == default.id


# ---------------------------------------------------------------------------
# v0.3.0 — B7 reopen, B8 edit guard, B2 escalation toggle
# ---------------------------------------------------------------------------

class TestReopen:
    def test_reopen_completed(self, seeded):
        from app.models import CaptureSource, CommitmentState, EditSource
        from app.services.commitments import create_commitment, mark_done, reopen
        db = seeded["db"]
        c = create_commitment(db, owner=seeded["user"], text="x",
                              source=CaptureSource.DASHBOARD)
        mark_done(db, c, source=EditSource.DASHBOARD)
        assert c.state == CommitmentState.COMPLETE
        assert c.completed_at is not None

        reopen(db, c, source=EditSource.DASHBOARD)
        assert c.state == CommitmentState.ACTIVE
        assert c.completed_at is None

    def test_reopen_noop_for_active(self, seeded):
        from app.models import CaptureSource, CommitmentState, EditSource
        from app.services.commitments import create_commitment, reopen
        db = seeded["db"]
        c = create_commitment(db, owner=seeded["user"], text="x",
                              source=CaptureSource.DASHBOARD)
        before = c.version
        reopen(db, c, source=EditSource.DASHBOARD)
        assert c.state == CommitmentState.ACTIVE
        assert c.version == before  # no-op


class TestEditGuard:
    """B8: field edits should be rejected on non-editable states."""

    def test_edit_completed_text_rejected(self, seeded):
        from app.models import CaptureSource, EditSource
        from app.services.commitments import create_commitment, edit_text, mark_done
        db = seeded["db"]
        c = create_commitment(db, owner=seeded["user"], text="x",
                              source=CaptureSource.DASHBOARD)
        mark_done(db, c, source=EditSource.DASHBOARD)
        with pytest.raises(ValueError, match="Can't edit"):
            edit_text(db, c, "new text", source=EditSource.DASHBOARD)

    def test_edit_archived_deadline_rejected(self, seeded):
        from app.models import CaptureSource, EditSource
        from app.services.commitments import (
            archive, create_commitment, mark_done, set_deadline,
        )
        db = seeded["db"]
        c = create_commitment(db, owner=seeded["user"], text="x",
                              source=CaptureSource.DASHBOARD)
        mark_done(db, c, source=EditSource.DASHBOARD)
        archive(db, c, source=EditSource.DASHBOARD)
        with pytest.raises(ValueError):
            set_deadline(db, c, datetime.now(timezone.utc) + timedelta(days=1),
                         source=EditSource.DASHBOARD)

    def test_edit_on_hold_text_allowed(self, seeded):
        """On-hold is editable — only complete/archived/deleted are locked."""
        from app.models import CaptureSource, EditSource
        from app.services.commitments import (
            create_commitment, edit_text, put_on_hold,
        )
        db = seeded["db"]
        c = create_commitment(db, owner=seeded["user"], text="old",
                              source=CaptureSource.DASHBOARD)
        put_on_hold(db, c, resume_at=None, source=EditSource.DASHBOARD)
        edit_text(db, c, "new", source=EditSource.DASHBOARD)
        assert c.text == "new"


class TestEscalationToggle:
    """B2: stop_escalation goes through the service layer now."""

    def test_set_escalation_enabled_logs_edit(self, seeded):
        from app.models import CaptureSource, CommitmentEdit, EditSource
        from app.services.commitments import (
            create_commitment, set_escalation_enabled,
        )
        db = seeded["db"]
        c = create_commitment(db, owner=seeded["user"], text="x",
                              source=CaptureSource.DASHBOARD)
        set_escalation_enabled(db, c, False, source=EditSource.SLACK)
        db.flush()
        assert c.escalation_enabled is False
        assert c.last_writer == "slack"
        edits = db.query(CommitmentEdit).filter_by(commitment_id=c.id,
                                                   field="escalation_enabled").all()
        assert len(edits) == 1


# ---------------------------------------------------------------------------
# B4 — overdue cadence
# ---------------------------------------------------------------------------

class TestOverdueCadence:
    def test_overdue_uses_floor(self, seeded):
        from app.models import CaptureSource
        from app.services.commitments import create_commitment
        from app.services.pings import compute_next_ping_at
        db, pri = seeded["db"], seeded["pri"]
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        c = create_commitment(
            db, owner=seeded["user"], text="x",
            source=CaptureSource.DASHBOARD,
            deadline=now - timedelta(days=10),  # 10 days overdue
        )
        nxt = compute_next_ping_at(c, pri, last_ping_at=None, now=now, db=db)
        assert nxt == now + timedelta(minutes=pri.max_ping_frequency_minutes)

    def test_deeply_overdue_does_not_overflow(self, seeded):
        """No matter how overdue, we return a sensible interval."""
        from app.models import CaptureSource
        from app.services.commitments import create_commitment
        from app.services.pings import compute_next_ping_at
        db, pri = seeded["db"], seeded["pri"]
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        c = create_commitment(
            db, owner=seeded["user"], text="x",
            source=CaptureSource.DASHBOARD,
            deadline=now - timedelta(days=3650),
        )
        nxt = compute_next_ping_at(c, pri, last_ping_at=None, now=now, db=db)
        assert nxt is not None
        assert (nxt - now).total_seconds() > 0


# ---------------------------------------------------------------------------
# Priority resolution: dead priority_ids fall back to default
# ---------------------------------------------------------------------------

class TestPriorityResolution:
    def test_unknown_priority_id_falls_back_to_default(self, seeded):
        from app.models import CaptureSource
        from app.services.commitments import create_commitment
        db, default = seeded["db"], seeded["pri"]
        c = create_commitment(
            db, owner=seeded["user"], text="x",
            source=CaptureSource.DASHBOARD,
            priority_level_id="not-a-real-id",
        )
        assert c.priority_level_id == default.id

    def test_soft_deleted_priority_id_falls_back_to_default(self, seeded):
        from app.models import CaptureSource
        from app.services.commitments import (
            create_commitment, create_priority_level, soft_delete_priority_level,
        )
        db, user, default = seeded["db"], seeded["user"], seeded["pri"]
        urgent = create_priority_level(db, user=user, name="Urgent")
        soft_delete_priority_level(db, urgent)
        c = create_commitment(
            db, owner=user, text="x",
            source=CaptureSource.DASHBOARD,
            priority_level_id=urgent.id,
        )
        assert c.priority_level_id == default.id
