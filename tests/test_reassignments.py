"""
Reassignment service tests.

Covers the state machine, validation, and side-effects (ping rescheduling,
edit-log entries). No Slack — these run directly against the service module
which is the source of truth for what reassignment *means*.

Covered cases:
  - Happy path: request → accept → owner changes, priority remapped, pings
    re-armed.
  - Decline: rollback to ACTIVE under original owner.
  - Cancel: same rollback, owner-initiated.
  - Expire: scheduler-style sweep flips PENDING past expires_at to EXPIRED
    and restores commitment.
  - Self-reassign rejected.
  - Double pending rejected.
  - Reassign to un-onboarded user rejected.
  - Accept/decline by wrong user rejected.
  - Reassign blocks edits while REASSIGNED.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
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
def two_users(db_session):
    """Workspace with Alice (owner) and Bob (target). Both onboarded."""
    from app.models import PriorityLevel, User, Workspace
    ws = Workspace(slack_team_id="T_TEST", bot_token="xoxb")
    db_session.add(ws); db_session.flush()

    now = datetime.now(timezone.utc)
    alice = User(
        workspace_id=ws.id, slack_user_id="U_ALICE",
        display_name="Alice", signed_in_at=now,
    )
    bob = User(
        workspace_id=ws.id, slack_user_id="U_BOB",
        display_name="Bob", signed_in_at=now,
    )
    db_session.add_all([alice, bob]); db_session.flush()

    alice_pri = PriorityLevel(
        user_id=alice.id, name="Normal",
        base_ping_interval_minutes=60,
        escalation_trigger_hours_before_deadline=24,
        max_ping_frequency_minutes=10,
        escalation_rate=2.0,
        is_system_default=True,
    )
    bob_pri = PriorityLevel(
        user_id=bob.id, name="Normal",
        base_ping_interval_minutes=120,   # different from Alice's so we can
        escalation_trigger_hours_before_deadline=24,  # confirm remap happened
        max_ping_frequency_minutes=15,
        escalation_rate=2.0,
        is_system_default=True,
    )
    db_session.add_all([alice_pri, bob_pri]); db_session.flush()
    db_session.commit()
    return {
        "db": db_session, "ws": ws,
        "alice": alice, "bob": bob,
        "alice_pri": alice_pri, "bob_pri": bob_pri,
    }


@pytest.fixture
def commitment(two_users):
    """An ACTIVE commitment owned by Alice with one queued ping."""
    from app.models import CaptureSource, Commitment, CommitmentState
    from app.services import pings as ping_svc
    db, alice, alice_pri = two_users["db"], two_users["alice"], two_users["alice_pri"]
    c = Commitment(
        user_id=alice.id, workspace_id=alice.workspace_id,
        text="Send the spec to Priya",
        source=CaptureSource.SLASH_COMMAND,
        priority_level_id=alice_pri.id,
        state=CommitmentState.ACTIVE,
    )
    db.add(c); db.flush()
    ping_svc.schedule_initial_ping(db, c, alice_pri)
    db.commit()
    return c


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

class TestHappyPaths:
    def test_request_moves_to_on_hold_limbo_and_pings_deleted(self, two_users, commitment):
        from app.models import CommitmentState, EditSource, Ping
        from app.services import reassignments as svc
        db = two_users["db"]

        # Sanity: commitment starts with one unsent ping.
        unsent_before = db.query(Ping).filter_by(
            commitment_id=commitment.id, sent_at=None,
        ).count()
        assert unsent_before == 1

        r = svc.request_reassignment(
            db, commitment=commitment,
            target_slack_user_id="U_BOB",
            source=EditSource.SLACK,
            note="slammed this week",
        )
        db.commit()
        db.refresh(commitment)

        # The limbo state is ON_HOLD (no auto-resume — the 24h timer lives on
        # the Reassignment row's expires_at instead).
        assert commitment.state == CommitmentState.ON_HOLD
        assert commitment.on_hold_resume_at is None
        assert r.note == "slammed this week"
        unsent_after = db.query(Ping).filter_by(
            commitment_id=commitment.id, sent_at=None,
        ).count()
        assert unsent_after == 0

    def test_accept_moves_to_REASSIGNED_state_under_new_owner(self, two_users, commitment):
        from app.models import CommitmentState, EditSource, Ping
        from app.services import reassignments as svc
        db, alice, bob = two_users["db"], two_users["alice"], two_users["bob"]
        bob_pri = two_users["bob_pri"]

        r = svc.request_reassignment(
            db, commitment=commitment,
            target_slack_user_id="U_BOB", source=EditSource.SLACK,
        )
        db.commit()

        svc.accept_reassignment(
            db, reassignment=r, actor=bob, source=EditSource.SLACK,
        )
        db.commit()
        db.refresh(commitment)

        assert commitment.user_id == bob.id
        # Post-acceptance state is REASSIGNED — Bob's work-in-progress.
        assert commitment.state == CommitmentState.REASSIGNED
        # Priority should be remapped to Bob's default, not Alice's.
        assert commitment.priority_level_id == bob_pri.id
        # A fresh ping should be queued under Bob's cadence.
        pings = db.query(Ping).filter_by(commitment_id=commitment.id, sent_at=None).all()
        assert len(pings) == 1

    def test_decline_rolls_back_to_original_owner(self, two_users, commitment):
        from app.models import CommitmentState, EditSource, ReassignmentStatus
        from app.services import reassignments as svc
        db, alice, bob = two_users["db"], two_users["alice"], two_users["bob"]

        r = svc.request_reassignment(
            db, commitment=commitment, target_slack_user_id="U_BOB",
            source=EditSource.SLACK,
        )
        db.commit()
        svc.decline_reassignment(
            db, reassignment=r, actor=bob, source=EditSource.SLACK,
        )
        db.commit()
        db.refresh(commitment); db.refresh(r)

        assert r.status == ReassignmentStatus.DECLINED
        assert commitment.user_id == alice.id
        assert commitment.state == CommitmentState.ACTIVE

    def test_cancel_rolls_back_and_records_status(self, two_users, commitment):
        from app.models import CommitmentState, EditSource, ReassignmentStatus
        from app.services import reassignments as svc
        db, alice = two_users["db"], two_users["alice"]

        r = svc.request_reassignment(
            db, commitment=commitment, target_slack_user_id="U_BOB",
            source=EditSource.SLACK,
        )
        db.commit()
        svc.cancel_reassignment(
            db, reassignment=r, actor=alice, source=EditSource.SLACK,
        )
        db.commit()
        db.refresh(commitment); db.refresh(r)

        assert r.status == ReassignmentStatus.CANCELLED
        assert commitment.state == CommitmentState.ACTIVE
        assert commitment.user_id == alice.id

    def test_expire_due_flips_overdue_pending(self, two_users, commitment):
        from app.models import CommitmentState, EditSource, ReassignmentStatus
        from app.services import reassignments as svc
        db = two_users["db"]

        r = svc.request_reassignment(
            db, commitment=commitment, target_slack_user_id="U_BOB",
            source=EditSource.SLACK,
        )
        # Backdate expires_at so the sweep grabs it.
        r.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()

        expired = svc.expire_due(db)
        db.commit()
        db.refresh(commitment); db.refresh(r)

        assert len(expired) == 1
        assert r.status == ReassignmentStatus.EXPIRED
        assert commitment.state == CommitmentState.ACTIVE


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_self_reassign_rejected(self, two_users, commitment):
        from app.models import EditSource
        from app.services import reassignments as svc
        db = two_users["db"]
        with pytest.raises(ValueError, match="yourself"):
            svc.request_reassignment(
                db, commitment=commitment, target_slack_user_id="U_ALICE",
                source=EditSource.SLACK,
            )

    def test_double_pending_rejected(self, two_users, commitment):
        from app.models import EditSource, User
        from app.services import reassignments as svc
        db = two_users["db"]
        # Need a second onboarded user to attempt a second pending.
        carol = User(
            workspace_id=two_users["ws"].id, slack_user_id="U_CAROL",
            signed_in_at=datetime.now(timezone.utc),
        )
        db.add(carol); db.commit()

        svc.request_reassignment(
            db, commitment=commitment, target_slack_user_id="U_BOB",
            source=EditSource.SLACK,
        )
        db.commit()
        with pytest.raises(ValueError, match="already a pending"):
            svc.request_reassignment(
                db, commitment=commitment, target_slack_user_id="U_CAROL",
                source=EditSource.SLACK,
            )

    def test_target_must_be_onboarded(self, two_users, commitment):
        from app.models import EditSource, User
        from app.services import reassignments as svc
        db = two_users["db"]
        # Lurker has a User row but no signed_in_at (auto-provisioned).
        lurker = User(
            workspace_id=two_users["ws"].id, slack_user_id="U_LURK",
        )
        db.add(lurker); db.commit()
        with pytest.raises(ValueError, match="hasn't signed in"):
            svc.request_reassignment(
                db, commitment=commitment, target_slack_user_id="U_LURK",
                source=EditSource.SLACK,
            )

    def test_target_outside_workspace_rejected(self, two_users, commitment):
        from app.models import EditSource, PriorityLevel, User, Workspace
        from app.services import reassignments as svc
        db = two_users["db"]
        other_ws = Workspace(slack_team_id="T_OTHER", bot_token="xoxb")
        db.add(other_ws); db.flush()
        dave = User(
            workspace_id=other_ws.id, slack_user_id="U_DAVE",
            signed_in_at=datetime.now(timezone.utc),
        )
        db.add(dave); db.commit()
        with pytest.raises(ValueError, match="hasn't signed in"):
            # Same error message — we look for the target *in the same workspace*,
            # so a cross-workspace user simply doesn't exist as far as this lookup
            # is concerned.
            svc.request_reassignment(
                db, commitment=commitment, target_slack_user_id="U_DAVE",
                source=EditSource.SLACK,
            )

    def test_only_active_commitments_can_be_reassigned(self, two_users, commitment):
        from app.models import CommitmentState, EditSource
        from app.services import reassignments as svc
        from app.services import commitments as commit_svc
        db = two_users["db"]
        commit_svc.put_on_hold(
            db, commitment, resume_at=None, source=EditSource.DASHBOARD,
        )
        db.commit()
        with pytest.raises(ValueError, match="active"):
            svc.request_reassignment(
                db, commitment=commitment, target_slack_user_id="U_BOB",
                source=EditSource.SLACK,
            )

    def test_accept_by_wrong_user_rejected(self, two_users, commitment):
        from app.models import EditSource, User
        from app.services import reassignments as svc
        db = two_users["db"]
        intruder = User(
            workspace_id=two_users["ws"].id, slack_user_id="U_NOPE",
            signed_in_at=datetime.now(timezone.utc),
        )
        db.add(intruder); db.commit()
        r = svc.request_reassignment(
            db, commitment=commitment, target_slack_user_id="U_BOB",
            source=EditSource.SLACK,
        )
        db.commit()
        with pytest.raises(ValueError, match="named recipient"):
            svc.accept_reassignment(
                db, reassignment=r, actor=intruder, source=EditSource.SLACK,
            )

    def test_cancel_by_non_owner_rejected(self, two_users, commitment):
        from app.models import EditSource
        from app.services import reassignments as svc
        db, bob = two_users["db"], two_users["bob"]
        r = svc.request_reassignment(
            db, commitment=commitment, target_slack_user_id="U_BOB",
            source=EditSource.SLACK,
        )
        db.commit()
        with pytest.raises(ValueError, match="current owner"):
            svc.cancel_reassignment(
                db, reassignment=r, actor=bob, source=EditSource.SLACK,
            )


# ---------------------------------------------------------------------------
# Side effects: edit log + idempotency
# ---------------------------------------------------------------------------

class TestSideEffects:
    def test_each_transition_writes_edit_log(self, two_users, commitment):
        from app.models import CommitmentEdit, EditSource
        from app.services import reassignments as svc
        db, bob = two_users["db"], two_users["bob"]

        r = svc.request_reassignment(
            db, commitment=commitment, target_slack_user_id="U_BOB",
            source=EditSource.SLACK,
        )
        db.commit()
        svc.accept_reassignment(
            db, reassignment=r, actor=bob, source=EditSource.SLACK,
        )
        db.commit()

        fields = {
            e.field for e in db.query(CommitmentEdit)
            .filter_by(commitment_id=commitment.id).all()
        }
        # The request emits a state transition + a 'reassignment_requested'
        # marker; the accept emits an 'owner' change + a 'priority_level_id'
        # remap (since Alice's and Bob's defaults differ).
        assert "state" in fields
        assert "reassignment_requested" in fields
        assert "owner" in fields

    def test_accept_is_idempotent(self, two_users, commitment):
        from app.models import EditSource, ReassignmentStatus
        from app.services import reassignments as svc
        db, bob = two_users["db"], two_users["bob"]
        r = svc.request_reassignment(
            db, commitment=commitment, target_slack_user_id="U_BOB",
            source=EditSource.SLACK,
        )
        db.commit()
        svc.accept_reassignment(
            db, reassignment=r, actor=bob, source=EditSource.SLACK,
        )
        db.commit()
        # A second accept on the already-accepted row is a no-op, not an error.
        svc.accept_reassignment(
            db, reassignment=r, actor=bob, source=EditSource.SLACK,
        )
        assert r.status == ReassignmentStatus.ACCEPTED

    def test_pending_limbo_blocks_field_edits(self, two_users, commitment):
        """Even though the commitment is in ON_HOLD (normally editable), the
        pending reassignment locks it — Bob is deciding based on its current
        state, so Alice can't change the text mid-flight."""
        from app.models import EditSource
        from app.services import reassignments as svc
        from app.services import commitments as commit_svc
        db = two_users["db"]
        svc.request_reassignment(
            db, commitment=commitment, target_slack_user_id="U_BOB",
            source=EditSource.SLACK,
        )
        db.commit()
        with pytest.raises(ValueError, match="awaiting a reassignment"):
            commit_svc.edit_text(
                db, commitment, "new text", source=EditSource.DASHBOARD,
            )

    def test_decline_restores_to_prior_state_not_just_active(self, two_users, commitment):
        """If Bob already owned a REASSIGNED commitment and re-reassigned to
        Carol, then Carol declined — the commitment should land back under
        Bob in REASSIGNED, not lose the 'reassigned' label by going ACTIVE.
        """
        from app.models import CommitmentState, EditSource, User
        from app.services import reassignments as svc
        db, bob = two_users["db"], two_users["bob"]
        # Carol — a third onboarded user.
        carol = User(
            workspace_id=two_users["ws"].id, slack_user_id="U_CAROL",
            signed_in_at=datetime.now(timezone.utc),
        )
        db.add(carol); db.commit()

        # Alice → Bob (accept). Commitment now REASSIGNED under Bob.
        r1 = svc.request_reassignment(
            db, commitment=commitment, target_slack_user_id="U_BOB",
            source=EditSource.SLACK,
        )
        db.commit()
        svc.accept_reassignment(
            db, reassignment=r1, actor=bob, source=EditSource.SLACK,
        )
        db.commit()
        db.refresh(commitment)
        assert commitment.state == CommitmentState.REASSIGNED

        # Bob → Carol. Now ON_HOLD with prior_state = REASSIGNED.
        r2 = svc.request_reassignment(
            db, commitment=commitment, target_slack_user_id="U_CAROL",
            source=EditSource.SLACK,
        )
        db.commit()
        db.refresh(commitment)
        assert commitment.state == CommitmentState.ON_HOLD
        assert commitment.prior_state == CommitmentState.REASSIGNED

        # Carol declines → commitment goes back to REASSIGNED, not ACTIVE.
        svc.decline_reassignment(
            db, reassignment=r2, actor=carol, source=EditSource.SLACK,
        )
        db.commit()
        db.refresh(commitment)
        assert commitment.state == CommitmentState.REASSIGNED
        assert commitment.user_id == bob.id

    def test_reassigned_commitments_are_re_reassignable(self, two_users, commitment):
        """After Bob accepts, he should be able to hand the same commitment
        off to Carol — REASSIGNED is a live state like ACTIVE for this
        purpose."""
        from app.models import CommitmentState, EditSource, User
        from app.services import reassignments as svc
        db, bob = two_users["db"], two_users["bob"]

        carol = User(
            workspace_id=two_users["ws"].id, slack_user_id="U_CAROL",
            signed_in_at=datetime.now(timezone.utc),
        )
        db.add(carol); db.commit()

        r1 = svc.request_reassignment(
            db, commitment=commitment, target_slack_user_id="U_BOB",
            source=EditSource.SLACK,
        )
        db.commit()
        svc.accept_reassignment(
            db, reassignment=r1, actor=bob, source=EditSource.SLACK,
        )
        db.commit()
        db.refresh(commitment)
        assert commitment.state == CommitmentState.REASSIGNED

        # Bob hands it on to Carol.
        r2 = svc.request_reassignment(
            db, commitment=commitment, target_slack_user_id="U_CAROL",
            source=EditSource.SLACK,
        )
        db.commit()
        db.refresh(commitment)
        # Limbo again — back to ON_HOLD, now from Bob's side.
        assert commitment.state == CommitmentState.ON_HOLD
