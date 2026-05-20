"""
End-to-end tests for the agentic commitment capture pipeline.

All tests run against the deterministic StubProvider — no network, no API
key, no cost. The aim is to exercise the orchestration (buffer → classify
→ persist → undo) plus the per-user gates (agent_enabled, confidence
floor) and the safety rails (dry-run, dedup, undo window).

Anthropic-specific behavior (JSON repair, message_id hallucination filter)
is covered by direct calls into AnthropicProvider with a mocked client —
but the fixture below is enough to exercise the contract.
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
    # Force the stub provider — these tests don't talk to a real LLM.
    os.environ["AGENT_PROVIDER"] = "stub"
    os.environ["AGENT_DRY_RUN"] = "false"
    os.environ["AGENT_CONFIDENCE_FLOOR"] = "0.75"
    os.environ["AGENT_UNDO_WINDOW_MINUTES"] = "60"
    os.environ["AGENT_BUFFER_RETENTION_DAYS"] = "7"

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
    """An onboarded user with the agent turned ON."""
    from app.models import PriorityLevel, User, Workspace
    ws = Workspace(slack_team_id="T", bot_token="xoxb")
    db_session.add(ws); db_session.flush()
    u = User(
        workspace_id=ws.id, slack_user_id="U_ALICE",
        signed_in_at=datetime.now(timezone.utc),
        agent_enabled=True,
    )
    db_session.add(u); db_session.flush()
    p = PriorityLevel(
        user_id=u.id, name="Normal", color="#888",
        base_ping_interval_minutes=240,
        escalation_trigger_hours_before_deadline=24,
        max_ping_frequency_minutes=30, escalation_rate=2.0,
        is_system_default=True,
    )
    db_session.add(p); db_session.flush()
    return u


# ---------------------------------------------------------------------------
# StubProvider
# ---------------------------------------------------------------------------

def test_stub_classifies_clear_commitment():
    from app.services.llm import HarvestedMessage, StubProvider
    out = StubProvider().classify([
        HarvestedMessage(id="m1", text="I'll send the spec by Friday"),
    ])
    assert len(out) == 1
    v = out[0]
    assert v.is_commitment is True
    # Confidence gets the deadline-token boost ("by Friday").
    assert v.confidence >= 0.85
    assert "first-person future" in v.rationale.lower()


def test_stub_rejects_vague_intent():
    from app.services.llm import HarvestedMessage, StubProvider
    out = StubProvider().classify([
        HarvestedMessage(id="m1", text="I'll think about it"),
    ])
    assert out[0].is_commitment is False
    assert out[0].confidence <= 0.5


def test_stub_rejects_hypothetical():
    from app.services.llm import HarvestedMessage, StubProvider
    out = StubProvider().classify([
        HarvestedMessage(id="m1", text="If we ship today, I'll buy lunch"),
    ])
    assert out[0].is_commitment is False


def test_stub_rejects_third_party_action():
    from app.services.llm import HarvestedMessage, StubProvider
    out = StubProvider().classify([
        HarvestedMessage(id="m1", text="John should fix that bug"),
    ])
    assert out[0].is_commitment is False


# ---------------------------------------------------------------------------
# Buffer + scan
# ---------------------------------------------------------------------------

def test_buffer_message_is_idempotent(db_session, alice):
    """Re-buffering the same (user, channel, ts) returns None on the dup."""
    from app.services import agent as agent_svc

    first = agent_svc.buffer_message(
        db_session, user=alice, channel_id="C1",
        message_ts="1700000000.000100", text="I'll send the spec by Friday",
    )
    assert first is not None
    second = agent_svc.buffer_message(
        db_session, user=alice, channel_id="C1",
        message_ts="1700000000.000100", text="I'll send the spec by Friday",
    )
    assert second is None  # dup squashed
    assert agent_svc.pending_buffer_count(db_session, alice) == 1


def test_buffer_refuses_when_agent_disabled(db_session, alice):
    """An off-by-default user never accumulates buffer rows."""
    from app.services import agent as agent_svc
    alice.agent_enabled = False
    out = agent_svc.buffer_message(
        db_session, user=alice, channel_id="C1",
        message_ts="1700000000.000100", text="I'll do it",
    )
    assert out is None
    assert agent_svc.pending_buffer_count(db_session, alice) == 0


def test_scan_persists_high_confidence_capture(db_session, alice):
    """A clear commitment becomes a real Commitment row via the service layer."""
    from app.services import agent as agent_svc
    from app.services.llm import StubProvider
    from app.models import CaptureSource, Commitment, CommitmentState

    agent_svc.buffer_message(
        db_session, user=alice, channel_id="C1",
        message_ts="1700000000.000100",
        text="I'll send the design doc by Friday",
    )
    created = agent_svc.scan_user(db_session, alice, provider=StubProvider())
    assert len(created) == 1
    c = created[0]
    assert c.source == CaptureSource.AGENT
    assert c.state == CommitmentState.ACTIVE
    assert c.agent_confidence is not None and c.agent_confidence >= 0.75
    assert c.agent_rationale  # non-empty
    # Slack provenance carries through so the existing dedup constraint
    # would block a redundant capture from the notation path later.
    assert c.slack_channel_id == "C1"
    assert c.slack_message_ts == "1700000000.000100"


def test_scan_drops_below_floor(db_session, alice):
    """Low-confidence verdicts don't get persisted."""
    from app.services import agent as agent_svc
    from app.services.llm import StubProvider
    from app.models import AgentMessageBuffer, Commitment

    # "I'll think about it" is in the stub's anti-pattern set → confidence 0.2.
    agent_svc.buffer_message(
        db_session, user=alice, channel_id="C1",
        message_ts="1700000000.000200", text="I'll think about it",
    )
    created = agent_svc.scan_user(db_session, alice, provider=StubProvider())
    assert created == []
    # But the row is still marked processed so the next sweep skips it.
    row = db_session.query(AgentMessageBuffer).first()
    assert row.processed_at is not None
    # And no commitment was created.
    assert db_session.query(Commitment).count() == 0


def test_scan_is_idempotent(db_session, alice):
    """Re-running the scan over the same buffer doesn't double-create."""
    from app.services import agent as agent_svc
    from app.services.llm import StubProvider

    agent_svc.buffer_message(
        db_session, user=alice, channel_id="C1",
        message_ts="1700000000.000300",
        text="I'll send the doc tonight",
    )
    first = agent_svc.scan_user(db_session, alice, provider=StubProvider())
    second = agent_svc.scan_user(db_session, alice, provider=StubProvider())
    assert len(first) == 1
    assert second == []  # buffer is now drained


def test_dry_run_does_not_persist(db_session, alice, monkeypatch):
    """AGENT_DRY_RUN suppresses writes — useful for prompt-tuning in dev."""
    from app.services import agent as agent_svc
    from app.services.llm import StubProvider
    from app.models import AgentMessageBuffer, Commitment
    from app import config as app_config

    monkeypatch.setenv("AGENT_DRY_RUN", "true")
    app_config.get_settings.cache_clear()

    agent_svc.buffer_message(
        db_session, user=alice, channel_id="C1",
        message_ts="1700000000.000400",
        text="I'll send the doc tonight",
    )
    created = agent_svc.scan_user(db_session, alice, provider=StubProvider())
    assert created == []
    assert db_session.query(Commitment).count() == 0
    # Buffer row should NOT be marked processed in dry-run so re-runs are
    # deterministic and observable.
    row = db_session.query(AgentMessageBuffer).first()
    assert row.processed_at is None


def test_per_user_floor_override(db_session, alice):
    """A user can raise the floor above the system default."""
    from app.services import agent as agent_svc
    from app.services.llm import StubProvider
    from app.models import Commitment

    # Stub assigns 0.88 to "I'll send …" without a deadline token.
    # Set floor at 95 → that capture should be dropped.
    alice.agent_confidence_floor_pct = 95
    agent_svc.buffer_message(
        db_session, user=alice, channel_id="C1",
        message_ts="1700000000.000500",
        text="I'll review the PR",
    )
    created = agent_svc.scan_user(db_session, alice, provider=StubProvider())
    assert created == []
    assert db_session.query(Commitment).count() == 0


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------

def test_undo_hard_deletes_fresh_capture(db_session, alice):
    from app.services import agent as agent_svc
    from app.services.llm import StubProvider
    from app.models import Commitment

    agent_svc.buffer_message(
        db_session, user=alice, channel_id="C1",
        message_ts="1700000000.000600",
        text="I'll send the doc tonight",
    )
    (c,) = agent_svc.scan_user(db_session, alice, provider=StubProvider())
    ok = agent_svc.undo_agent_capture(db_session, c)
    assert ok is True
    # Flush so the pending DELETE is visible to the count query (the
    # session is configured with autoflush=False; production callers
    # rely on session_scope() committing).
    db_session.flush()
    # Row is gone — NOT in the bin. A false positive shouldn't pollute
    # the user's failed-commitments history.
    assert db_session.query(Commitment).count() == 0


def test_undo_refuses_non_agent_capture(db_session, alice):
    """Slash-command + notation captures are off-limits for Undo."""
    from app.services import agent as agent_svc
    from app.services import commitments as commit_svc
    from app.models import CaptureSource, Commitment

    c = commit_svc.create_commitment(
        db_session, owner=alice,
        text="manual commitment", source=CaptureSource.SLASH_COMMAND,
    )
    assert agent_svc.undo_agent_capture(db_session, c) is False
    assert db_session.query(Commitment).count() == 1


def test_undo_refuses_past_window(db_session, alice):
    """After the undo window the inline affordance retires."""
    from app.services import agent as agent_svc
    from app.services import commitments as commit_svc
    from app.models import CaptureSource, Commitment

    c = commit_svc.create_commitment(
        db_session, owner=alice,
        text="auto-captured ages ago", source=CaptureSource.AGENT,
    )
    # Stamp it as 90 minutes old; the default window is 60 minutes.
    c.created_at = datetime.now(timezone.utc) - timedelta(minutes=90)
    db_session.flush()

    assert agent_svc.is_within_undo_window(c) is False
    assert agent_svc.undo_agent_capture(db_session, c) is False
    assert db_session.query(Commitment).count() == 1


# ---------------------------------------------------------------------------
# Buffer prune
# ---------------------------------------------------------------------------

def test_prune_removes_old_buffer_rows(db_session, alice):
    from app.services import agent as agent_svc
    from app.models import AgentMessageBuffer

    agent_svc.buffer_message(
        db_session, user=alice, channel_id="C1",
        message_ts="1700000000.000700", text="something",
    )
    # Backdate it past the retention window.
    row = db_session.query(AgentMessageBuffer).first()
    row.created_at = datetime.now(timezone.utc) - timedelta(days=8)
    db_session.flush()

    removed = agent_svc.prune_buffer(db_session)
    assert removed == 1
    assert db_session.query(AgentMessageBuffer).count() == 0


def test_scan_all_skips_disabled_users(db_session, alice):
    """Users without agent_enabled are not even queried for buffer rows."""
    from app.services import agent as agent_svc
    from app.services.llm import StubProvider

    alice.agent_enabled = False
    db_session.flush()
    out = agent_svc.scan_all(db_session, provider=StubProvider())
    assert out == {}
