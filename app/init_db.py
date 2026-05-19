"""
Initialise the SQLite database.

Usage:
    python -m app.init_db                       # just create tables
    python -m app.init_db --with-demo-user      # also seed demo data
    python -m app.init_db --reset-demo          # wipe and reseed demo user
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, time, timedelta, timezone

from app.db import Base, SessionLocal, engine
from app.models import (
    CaptureSource,
    Commitment,
    CommitmentRecipient,
    CommitmentState,
    Notation,
    PriorityLevel,
    User,
    Workspace,
)

log = logging.getLogger(__name__)


def create_tables() -> None:
    from app import models  # noqa: F401
    Base.metadata.create_all(engine)
    print("Tables created.")


def _seed_demo_data(db) -> None:
    ws = Workspace(slack_team_id="T_DEMO", name="Demo Workspace", bot_token="xoxb-demo")
    db.add(ws)
    db.flush()

    user = User(
        workspace_id=ws.id,
        slack_user_id="U_DEMO",
        email="demo@example.com",
        display_name="Demo User",
    )
    db.add(user)
    db.flush()

    normal = PriorityLevel(
        user_id=user.id, name="Normal", color="#2454ff",
        base_ping_interval_minutes=240, escalation_trigger_hours_before_deadline=24,
        max_ping_frequency_minutes=30, escalation_rate=2.0,
        is_system_default=True,
    )
    urgent = PriorityLevel(
        user_id=user.id, name="Urgent", color="#b8331f",
        base_ping_interval_minutes=60, escalation_trigger_hours_before_deadline=48,
        max_ping_frequency_minutes=15, escalation_rate=2.5,
    )
    db.add_all([normal, urgent])
    db.flush()

    db.add(Notation(user_id=user.id, pattern=r"\[\[commit.*\]\]", enabled=True))

    now = datetime.now(timezone.utc)
    samples = [
        Commitment(
            user_id=user.id, workspace_id=ws.id,
            text="Send the Q2 retrospective to leadership",
            source=CaptureSource.SLASH_COMMAND,
            slack_channel_id="C_DEMO", slack_message_ts="1700000001.000100",
            deadline=now + timedelta(days=2), priority_level_id=urgent.id,
            state=CommitmentState.ACTIVE,
        ),
        Commitment(
            user_id=user.id, workspace_id=ws.id,
            text="Review @priya's PR on the auth refactor",
            source=CaptureSource.MESSAGE_SHORTCUT,
            slack_channel_id="C_DEMO", slack_message_ts="1700000002.000100",
            deadline=now + timedelta(hours=18), priority_level_id=normal.id,
            state=CommitmentState.ACTIVE,
        ),
        Commitment(
            user_id=user.id, workspace_id=ws.id,
            text="Reply to the customer escalation in #support",
            source=CaptureSource.NOTATION,
            slack_channel_id="C_DEMO", slack_message_ts="1700000003.000100",
            deadline=None, priority_level_id=normal.id,
            state=CommitmentState.ACTIVE,
        ),
    ]
    db.add_all(samples)
    db.flush()
    db.add(CommitmentRecipient(commitment_id=samples[1].id, recipient_slack_user_id="U_PRIYA", is_current=True))


def seed_demo(reset: bool = False) -> None:
    db = SessionLocal()
    try:
        existing = db.query(Workspace).filter_by(slack_team_id="T_DEMO").first()
        if existing is not None:
            if not reset:
                print("Demo workspace already exists; skipping. Use --reset-demo to wipe and reseed.")
                return
            db.delete(existing)
            db.commit()
            print("Existing demo data deleted.")

        _seed_demo_data(db)
        db.commit()
        # Note: the dashboard now requires real Sign-in-with-Slack OAuth, so
        # the seeded U_DEMO user isn't directly accessible via browser.
        # seed_demo is primarily a fixture for the test suite — see
        # tests/test_smoke.py for the session-cookie injection pattern.
        print("Demo data seeded (test fixture; not reachable via OAuth login).")
    finally:
        db.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--with-demo-user", action="store_true",
                   help="Seed a demo workspace + user + sample data (skip if exists)")
    p.add_argument("--reset-demo", action="store_true",
                   help="Wipe any existing demo data and reseed")
    args = p.parse_args()
    create_tables()
    if args.reset_demo:
        seed_demo(reset=True)
    elif args.with_demo_user:
        seed_demo(reset=False)


if __name__ == "__main__":
    main()
