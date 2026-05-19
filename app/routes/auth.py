"""
Sign in with Slack — OAuth flow for the dashboard.

Uses Slack's OpenID Connect endpoints (distinct from the bot "Add to Slack"
flow): the user authorises CommitBot to read their identity, and we stash the
resulting `slack_user_id` + `slack_team_id` in a signed session cookie.

Routes:
  GET  /auth/slack/login     → redirect to Slack's authorize endpoint
  GET  /auth/slack/callback  → exchange code, fetch userInfo, set session
  POST /auth/logout          → clear session

Slack app config needed (one-time, in api.slack.com/apps):
  - OAuth & Permissions → Redirect URLs → add `{app_base_url}{redirect_path}`
  - Add User Token Scopes (OpenID): `openid`, `profile`, `email`
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import committing_db
from app.models import PriorityLevel, User, Workspace

log = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()

SLACK_AUTHORIZE_URL = "https://slack.com/openid/connect/authorize"
SLACK_TOKEN_URL = "https://slack.com/api/openid.connect.token"
SLACK_USERINFO_URL = "https://slack.com/api/openid.connect.userInfo"


def _redirect_uri() -> str:
    return settings.app_base_url.rstrip("/") + settings.slack_oauth_redirect_path


def _safe_next(value: Optional[str]) -> str:
    """Only honour relative paths — defends against open-redirect."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


@router.get("/auth/slack/login")
def slack_login(request: Request, next: str = Query("/")) -> RedirectResponse:
    """Kick off the OAuth flow — stash a state token + the post-login URL."""
    if not settings.slack_client_id:
        raise HTTPException(
            500,
            "Sign in with Slack isn't configured — set SLACK_CLIENT_ID / "
            "SLACK_CLIENT_SECRET and register the redirect URL with Slack.",
        )

    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    request.session["oauth_next"] = _safe_next(next)

    params = {
        "client_id": settings.slack_client_id,
        "scope": "openid profile email",
        "response_type": "code",
        "redirect_uri": _redirect_uri(),
        "state": state,
    }
    return RedirectResponse(f"{SLACK_AUTHORIZE_URL}?{urlencode(params)}", status_code=302)


@router.get("/auth/slack/callback")
def slack_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    db: Session = Depends(committing_db),
) -> RedirectResponse:
    """Exchange the code, fetch the user, and start a session."""
    if error:
        log.warning("Slack OAuth error: %s", error)
        raise HTTPException(400, f"Slack returned an error: {error}")
    if not code or not state:
        raise HTTPException(400, "Missing code or state from Slack.")

    expected_state = request.session.pop("oauth_state", None)
    next_url = _safe_next(request.session.pop("oauth_next", None))
    if not expected_state or not secrets.compare_digest(state, expected_state):
        raise HTTPException(400, "OAuth state mismatch — try signing in again.")

    # Token exchange. Slack accepts client credentials either as Basic auth or
    # as form params; the form-param style is friendlier to log when debugging.
    with httpx.Client(timeout=10.0) as http:
        tok_resp = http.post(
            SLACK_TOKEN_URL,
            data={
                "client_id": settings.slack_client_id,
                "client_secret": settings.slack_client_secret,
                "code": code,
                "redirect_uri": _redirect_uri(),
                "grant_type": "authorization_code",
            },
        )
        tok = tok_resp.json()
        if not tok.get("ok"):
            log.warning("Slack token exchange failed: %s", tok)
            raise HTTPException(400, f"Slack token exchange failed: {tok.get('error', 'unknown')}")

        access_token = tok.get("access_token")
        if not access_token:
            raise HTTPException(400, "Slack didn't return an access token.")

        info_resp = http.get(
            SLACK_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        info = info_resp.json()
        if not info.get("ok"):
            log.warning("Slack userInfo failed: %s", info)
            raise HTTPException(400, "Slack userInfo call failed.")

    slack_user_id = info.get("https://slack.com/user_id") or info.get("sub")
    slack_team_id = info.get("https://slack.com/team_id")
    if not slack_user_id or not slack_team_id:
        raise HTTPException(400, "Slack didn't return a user_id/team_id.")
    log.info(
        "OAuth sign-in: team=%s user=%s sub=%s url_user=%s email=%s",
        slack_team_id, slack_user_id, info.get("sub"),
        info.get("https://slack.com/user_id"), info.get("email"),
    )

    email = info.get("email")
    display_name = info.get("name")

    # Find-or-create the User row. Mirrors `_get_or_provision_user` in slack_app
    # but lives here because the auth flow may run before the user ever
    # touched the bot.
    ws = db.execute(
        select(Workspace).where(Workspace.slack_team_id == slack_team_id)
    ).scalar_one_or_none()
    if ws is None:
        ws = Workspace(slack_team_id=slack_team_id, bot_token=settings.slack_bot_token)
        db.add(ws)
        db.flush()

    user = db.execute(
        select(User).where(
            User.workspace_id == ws.id, User.slack_user_id == slack_user_id,
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if user is None:
        user = User(
            workspace_id=ws.id,
            slack_user_id=slack_user_id,
            email=email,
            display_name=display_name,
            signed_in_at=now,
        )
        db.add(user)
        db.flush()
        db.add(PriorityLevel(
            user_id=user.id, name="Normal", color="#4a90e2",
            base_ping_interval_minutes=240,
            escalation_trigger_hours_before_deadline=24,
            max_ping_frequency_minutes=30, escalation_rate=2.0,
            is_system_default=True,
        ))
        db.flush()
    else:
        # Backfill identity fields if the User row was bot-provisioned earlier
        # and we now have richer info from OpenID. Always stamp the sign-in
        # timestamp — this is what the Slack capture paths look at to decide
        # whether the user is onboarded.
        if email and user.email != email:
            user.email = email
        if display_name and user.display_name != display_name:
            user.display_name = display_name
        user.signed_in_at = now
        db.flush()

    request.session["slack_user_id"] = slack_user_id
    request.session["slack_team_id"] = slack_team_id
    request.session["display_name"] = display_name

    # Push the user's App Home from the sign-in CTA to the real commitments
    # view immediately, so they get instant feedback in Slack without waiting
    # to re-open the Home tab.
    try:
        from app.slack_app import _refresh_home, bolt_app
        _refresh_home(
            bolt_app.client,
            team_id=slack_team_id,
            slack_user_id=slack_user_id,
        )
    except Exception:
        log.exception("Couldn't refresh Slack Home after sign-in for %s", slack_user_id)

    return RedirectResponse(next_url, status_code=303)


@router.post("/auth/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/", status_code=303)
