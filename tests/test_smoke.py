"""
Smoke test: app imports, schema builds, demo data seeds, dashboard renders,
and one mutation round-trips through the HTMX endpoint.

This covers the boot path end-to-end so a deploy that breaks the wiring
between FastAPI, SQLAlchemy, and the scheduler fails loud and fast in CI.

For finer-grained tests of state transitions / cadence / notation validation,
see test_services.py.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx
import itsdangerous


def _bootstrap_env() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp.name}"
    os.environ["DRY_RUN_PINGS"] = "true"
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test"
    os.environ["SLACK_SIGNING_SECRET"] = "test-secret"
    os.environ["SESSION_SECRET"] = "test-session-secret"
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    # Reset the cached settings + the SQLAlchemy engine if a previous test
    # already imported these modules in this process.
    import importlib
    from app import config
    config.get_settings.cache_clear()
    return tmp.name


def _login(client, *, slack_user_id: str, slack_team_id: str = "T_DEMO") -> None:
    """Sign the user into the TestClient by minting a SessionMiddleware cookie.

    SessionMiddleware encodes the session dict as base64(json), then signs it
    with a TimestampSigner over `session_secret`. Reproducing that here means
    tests can bypass the real OAuth flow without adding a back-door endpoint.

    We swap the whole cookie jar each time to avoid duplicate-name conflicts —
    every response from SessionMiddleware re-sets the cookie, so otherwise the
    jar accumulates two `commitbot_session` entries on subsequent calls.
    """
    payload = {"slack_user_id": slack_user_id, "slack_team_id": slack_team_id}
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
    signer = itsdangerous.TimestampSigner(os.environ["SESSION_SECRET"])
    cookie_val = signer.sign(encoded).decode("utf-8")
    client.cookies = httpx.Cookies()
    client.cookies.set("commitbot_session", cookie_val)


def test_smoke():
    _bootstrap_env()

    # Clean module cache so the fresh DATABASE_URL is picked up.
    for mod_name in list(sys.modules):
        if mod_name == "app" or mod_name.startswith("app."):
            del sys.modules[mod_name]

    from app.db import Base, engine
    from app import models  # noqa: F401
    Base.metadata.create_all(engine)

    from app.main import app
    assert app is not None

    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert "version" in body

        # Logged-out dashboard redirects to /auth/slack/login.
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303
        assert "/auth/slack/login" in r.headers.get("location", "")

        # Login page renders.
        r = client.get("/login")
        assert r.status_code == 200
        assert "sign in with slack" in r.text.lower()

    print("smoke ok")


def test_demo_seed_round_trip():
    _bootstrap_env()

    for mod_name in list(sys.modules):
        if mod_name == "app" or mod_name.startswith("app."):
            del sys.modules[mod_name]

    from app.db import Base, engine, SessionLocal
    from app import models  # noqa: F401
    Base.metadata.create_all(engine)

    from app.init_db import seed_demo
    seed_demo()

    from app.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        _login(client, slack_user_id="U_DEMO", slack_team_id="T_DEMO")

        # Dashboard renders 3 commitments.
        r = client.get("/")
        assert r.status_code == 200, r.text
        for needle in ("Q2 retrospective", "PR on the auth refactor", "customer escalation"):
            assert needle in r.text, f"missing {needle!r} in dashboard"

        # Settings renders.
        r = client.get("/settings")
        assert r.status_code == 200, r.text
        assert "Custom notations" in r.text
        assert "Priority levels" in r.text

        # JSON export.
        r = client.get("/export/json")
        assert r.status_code == 200
        payload = r.json()
        assert len(payload) == 3
        assert all("text" in c and "state" in c for c in payload)

        # CSV export.
        r = client.get("/export/csv")
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        lines = r.text.strip().splitlines()
        assert lines[0].startswith("id,text,state,source")
        assert "priority_name" in lines[0]  # v0.3.0 — nested priority info
        assert len(lines) == 4  # 1 header + 3 rows

        # Round-trip a "mark done" via HTMX endpoint.
        commit_id = payload[0]["id"]
        r = client.post(f"/commitments/{commit_id}/done")
        assert r.status_code == 200, r.text

        # And it's now in /?state=complete.
        r = client.get("/?state=complete")
        assert r.status_code == 200
        assert payload[0]["text"] in r.text

    print("seed round-trip ok")


def test_authorization_blocks_cross_user_mutation():
    """A second user can't mark the first user's commitment done."""
    _bootstrap_env()
    for mod_name in list(sys.modules):
        if mod_name == "app" or mod_name.startswith("app."):
            del sys.modules[mod_name]

    from app.db import Base, engine, SessionLocal
    from app import models  # noqa: F401
    Base.metadata.create_all(engine)
    from app.init_db import seed_demo
    seed_demo()

    # Create a second user in the same workspace.
    from app.models import User, Workspace
    db = SessionLocal()
    try:
        ws = db.query(Workspace).first()
        intruder = User(workspace_id=ws.id, slack_user_id="U_INTRUDER", email="x@x")
        db.add(intruder)
        db.commit()
    finally:
        db.close()

    from app.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        # As U_DEMO, fetch a commitment id.
        _login(client, slack_user_id="U_DEMO", slack_team_id="T_DEMO")
        r = client.get("/export/json")
        assert r.status_code == 200
        target_id = r.json()[0]["id"]

        # Switch session to U_INTRUDER and try to mutate — must 404.
        _login(client, slack_user_id="U_INTRUDER", slack_team_id="T_DEMO")
        r = client.post(f"/commitments/{target_id}/done")
        assert r.status_code == 404, r.status_code

    print("authz ok")


if __name__ == "__main__":
    test_smoke()
    test_demo_seed_round_trip()
    test_authorization_blocks_cross_user_mutation()
