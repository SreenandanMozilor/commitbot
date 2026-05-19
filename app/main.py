"""
FastAPI entrypoint. Three responsibilities:

  1. Mount the Slack events/interactivity webhook at /slack/events.
  2. Mount the dashboard router at /.
  3. Manage the background scheduler with the FastAPI lifespan.

We also `init_database()` on startup so a fresh checkout can `uvicorn ...`
without first running `python -m app.init_db`. The latter is now reserved
for demo-data seeding only.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import init_database
from app.routes.auth import router as auth_router
from app.routes.dashboard import LoginRequired, router as dashboard_router
from app.scheduler import shutdown_scheduler, start_scheduler
from app.slack_app import bolt_app, slack_request_handler

settings = get_settings()
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("commitbot")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-init schema (idempotent). Cheap, safe, and removes a manual step.
    init_database()
    # Pass the bot's Slack web client into the scheduler so real pings can be
    # delivered when DRY_RUN_PINGS=false. In dev with placeholder credentials
    # the client object is still constructed; deliver_ping falls back to
    # dry-run logging.
    start_scheduler(slack_client=bolt_app.client)
    log.info("CommitBot up. dry_run_pings=%s", settings.dry_run_pings)
    try:
        yield
    finally:
        shutdown_scheduler()


app = FastAPI(title="CommitBot", version="0.3.0", lifespan=lifespan)

# Signed-cookie session — used by Sign-in-with-Slack to remember the logged-in
# user across requests. `secure_cookies` should be True whenever the dashboard
# is served over HTTPS (ngrok, production).
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="commitbot_session",
    https_only=settings.secure_cookies,
    same_site="lax",
    max_age=60 * 60 * 24 * 30,  # 30 days
)


@app.post("/slack/events")
async def slack_events(req: Request):
    """Single endpoint for Slack events, slash commands, shortcuts, interactivity."""
    return await slack_request_handler.handle(req)


# Auth (Sign in with Slack)
app.include_router(auth_router)

# Dashboard at /
app.include_router(dashboard_router)


@app.exception_handler(LoginRequired)
async def _login_required_handler(request: Request, exc: LoginRequired):
    """Send the user through Sign in with Slack, preserving their target URL."""
    next_url = quote(exc.next_url) if exc.next_url else "/"
    login_url = f"/auth/slack/login?next={next_url}"
    # HTMX needs HX-Redirect to do a real navigation instead of swapping the
    # login HTML into the page fragment.
    if request.headers.get("HX-Request"):
        return Response(status_code=401, headers={"HX-Redirect": login_url})
    return RedirectResponse(login_url, status_code=303)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "version": app.version}
