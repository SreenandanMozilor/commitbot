"""
Tests for the success/failed outcome model.

`outcome` is set on every terminal-state transition (COMPLETE, ARCHIVED,
DELETED). The rule is the same everywhere:
  - SUCCESS  iff completed_at is set AND on-time (or there's no deadline)
  - FAILED   otherwise (never completed, or completed late)

reopen() and restore_from_bin() back to ACTIVE clear the outcome — the
commitment is in flight again.
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
def alice(db_session):
    from app.models import PriorityLevel, User, Workspace
    ws = Workspace(slack_team_id="T", bot_token="xoxb")
    db_session.add(ws); db_session.flush()
    u = User(
        workspace_id=ws.id, slack_user_id="U_ALICE",
        signed_in_at=datetime.now(timezone.utc),
    )
    db_session.add(u); db_session.flush()
    p = PriorityLevel(
        user_id=u.id, name="Normal",
        base_ping_interval_minutes=60,
        escalation_trigger_hours_before_deadline=24,
        max_ping_frequency_minutes=10,
        escalation_rate=2.0,
        is_system_default=True,
    )
    db_session.add(p); db_session.commit()
    return u


def _make_commitment(db, owner, *, deadline=None):
    from app.models import CaptureSource, Commitment, CommitmentState
    c = Commitment(
        user_id=owner.id, workspace_id=owner.workspace_id,
        text="something",
        source=CaptureSource.SLASH_COMMAND,
        deadline=deadline,
        state=CommitmentState.ACTIVE,
    )
    db.add(c); db.flush()
    db.commit()
    return c


# ---------------------------------------------------------------------------
# mark_done — success / failed based on timing
# ---------------------------------------------------------------------------

class TestMarkDone:
    def test_on_time_completion_is_success(self, db_session, alice):
        from app.models import CommitmentOutcome, EditSource
        from app.services import commitments as svc
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        c = _make_commitment(db_session, alice, deadline=future)
        svc.mark_done(db_session, c, source=EditSource.DASHBOARD)
        db_session.commit()
        assert c.outcome == CommitmentOutcome.SUCCESS

    def test_completion_with_no_deadline_is_success(self, db_session, alice):
        from app.models import CommitmentOutcome, EditSource
        from app.services import commitments as svc
        c = _make_commitment(db_session, alice, deadline=None)
        svc.mark_done(db_session, c, source=EditSource.DASHBOARD)
        db_session.commit()
        assert c.outcome == CommitmentOutcome.SUCCESS

    def test_late_completion_is_failed(self, db_session, alice):
        from app.models import CommitmentOutcome, EditSource
        from app.services import commitments as svc
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        c = _make_commitment(db_session, alice, deadline=past)
        svc.mark_done(db_session, c, source=EditSource.DASHBOARD)
        db_session.commit()
        assert c.outcome == CommitmentOutcome.FAILED


# ---------------------------------------------------------------------------
# soft_delete — failed unless previously completed
# ---------------------------------------------------------------------------

class TestSoftDelete:
    def test_delete_from_active_is_failed(self, db_session, alice):
        from app.models import CommitmentOutcome, EditSource
        from app.services import commitments as svc
        c = _make_commitment(db_session, alice)
        svc.soft_delete(db_session, c, source=EditSource.DASHBOARD)
        db_session.commit()
        assert c.outcome == CommitmentOutcome.FAILED

    def test_delete_after_on_time_completion_keeps_success(self, db_session, alice):
        from app.models import CommitmentOutcome, EditSource
        from app.services import commitments as svc
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        c = _make_commitment(db_session, alice, deadline=future)
        svc.mark_done(db_session, c, source=EditSource.DASHBOARD)
        db_session.commit()
        assert c.outcome == CommitmentOutcome.SUCCESS
        svc.soft_delete(db_session, c, source=EditSource.DASHBOARD)
        db_session.commit()
        # Carry-forward — once you've succeeded, deleting doesn't downgrade.
        assert c.outcome == CommitmentOutcome.SUCCESS


# ---------------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------------

class TestArchive:
    def test_archive_after_success_keeps_success(self, db_session, alice):
        from app.models import CommitmentOutcome, EditSource
        from app.services import commitments as svc
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        c = _make_commitment(db_session, alice, deadline=future)
        svc.mark_done(db_session, c, source=EditSource.DASHBOARD)
        db_session.commit()
        svc.archive(db_session, c, source=EditSource.DASHBOARD)
        db_session.commit()
        assert c.outcome == CommitmentOutcome.SUCCESS

    def test_archive_after_late_completion_keeps_failed(self, db_session, alice):
        from app.models import CommitmentOutcome, EditSource
        from app.services import commitments as svc
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        c = _make_commitment(db_session, alice, deadline=past)
        svc.mark_done(db_session, c, source=EditSource.DASHBOARD)
        db_session.commit()
        svc.archive(db_session, c, source=EditSource.DASHBOARD)
        db_session.commit()
        assert c.outcome == CommitmentOutcome.FAILED


# ---------------------------------------------------------------------------
# reopen / restore — clear outcome when back to ACTIVE
# ---------------------------------------------------------------------------

class TestImmediateAutoArchive:
    """When the user has auto_delete_completed_after_days == 0, mark_done
    should immediately archive — the user has opted into 'keep my Complete
    tab clean' and shouldn't have to wait up to an hour for the sweep."""

    def test_x0_archives_immediately_on_done(self, db_session, alice):
        from app.models import CommitmentOutcome, CommitmentState, EditSource
        from app.services import commitments as svc
        alice.auto_delete_completed_after_days = 0
        db_session.commit()

        future = datetime.now(timezone.utc) + timedelta(hours=1)
        c = _make_commitment(db_session, alice, deadline=future)
        svc.mark_done(db_session, c, source=EditSource.DASHBOARD)
        db_session.commit()

        # Skipped past COMPLETE → directly in ARCHIVED, with SUCCESS preserved.
        assert c.state == CommitmentState.ARCHIVED
        assert c.outcome == CommitmentOutcome.SUCCESS

    def test_x_positive_stays_in_complete(self, db_session, alice):
        from app.models import CommitmentState, EditSource
        from app.services import commitments as svc
        alice.auto_delete_completed_after_days = 30
        db_session.commit()
        c = _make_commitment(db_session, alice)
        svc.mark_done(db_session, c, source=EditSource.DASHBOARD)
        db_session.commit()
        # Without the X=0 short-circuit, the commitment lives in COMPLETE
        # until the hourly sweep eventually deletes it.
        assert c.state == CommitmentState.COMPLETE


class TestReopenAndRestore:
    def test_reopen_clears_outcome(self, db_session, alice):
        from app.models import CommitmentOutcome, EditSource
        from app.services import commitments as svc
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        c = _make_commitment(db_session, alice, deadline=future)
        svc.mark_done(db_session, c, source=EditSource.DASHBOARD)
        db_session.commit()
        assert c.outcome == CommitmentOutcome.SUCCESS
        svc.reopen(db_session, c, source=EditSource.DASHBOARD)
        db_session.commit()
        assert c.outcome is None

    def test_restore_from_bin_to_active_clears_outcome(self, db_session, alice):
        from app.models import CommitmentOutcome, EditSource
        from app.services import commitments as svc
        c = _make_commitment(db_session, alice)
        svc.soft_delete(db_session, c, source=EditSource.DASHBOARD)
        db_session.commit()
        assert c.outcome == CommitmentOutcome.FAILED
        svc.restore_from_bin(db_session, c, source=EditSource.DASHBOARD)
        db_session.commit()
        assert c.outcome is None

    def test_restore_from_bin_to_complete_keeps_outcome(self, db_session, alice):
        from app.models import CommitmentOutcome, EditSource
        from app.services import commitments as svc
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        c = _make_commitment(db_session, alice, deadline=future)
        svc.mark_done(db_session, c, source=EditSource.DASHBOARD)
        db_session.commit()
        svc.soft_delete(db_session, c, source=EditSource.DASHBOARD)
        db_session.commit()
        assert c.outcome == CommitmentOutcome.SUCCESS
        # Coming up from bin while completed → COMPLETE (with outcome
        # preserved).
        svc.restore_from_bin(db_session, c, source=EditSource.DASHBOARD)
        db_session.commit()
        assert c.outcome == CommitmentOutcome.SUCCESS
