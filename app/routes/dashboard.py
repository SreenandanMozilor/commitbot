"""
Dashboard HTTP routes.

Server-rendered Jinja templates with a thin HTMX layer for inline state
changes. No JS build step needed.

Auth: signed-cookie session set by `/auth/slack/callback` (Sign in with Slack).
Every route below pulls the current user from the session via
`required_user`. Logged-out requests raise `LoginRequired`, which `app.main`
turns into a redirect to `/auth/slack/login` (or `HX-Redirect` for HTMX).

Routes are deliberately small — all logic lives in `services/commitments.py`.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse, JSONResponse, RedirectResponse, Response,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import committing_db, get_db
from app.models import (
    CaptureSource,
    Commitment,
    CommitmentOutcome,
    CommitmentState,
    EditSource,
    Notation,
    PriorityLevel,
    Reassignment,
    ReassignmentStatus,
    User,
    Workspace,
)
from app.services import commitments as commit_svc
from app.services import pings as ping_svc
from app.services import reassignments as reassign_svc
from app.slack_app import invalidate_notation_cache, list_workspace_members
from app.tz import (
    COMMON_TIMEZONES,
    local_input_to_utc,
    safe_zone,
    to_local,
    validate_zone,
)

log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

VALID_THEMES = {"auto", "light", "dark"}


# ---------------------------------------------------------------------------
# Auth — session-backed
# ---------------------------------------------------------------------------

class LoginRequired(Exception):
    """Raised when an authenticated route is hit without a session.

    `app.main` converts this into a redirect to /auth/slack/login (or an
    HX-Redirect header for HTMX requests), preserving `next` so the user
    lands back on the page they tried to reach.
    """
    def __init__(self, next_url: str = "/"):
        self.next_url = next_url


def _next_for(request: Request) -> str:
    qs = request.url.query
    return request.url.path + (f"?{qs}" if qs else "")


def _lookup_session_user(request: Request, db: Session) -> User:
    """Read the session, return the corresponding User, or raise LoginRequired.

    Slack user_id is workspace-scoped — same id can appear in two workspaces —
    so we match on `(team, user)` whenever the session has both.
    """
    sid = request.session.get("slack_user_id")
    tid = request.session.get("slack_team_id")
    if not sid:
        raise LoginRequired(_next_for(request))
    q = select(User).where(User.slack_user_id == sid)
    if tid:
        q = q.join(Workspace).where(Workspace.slack_team_id == tid)
    user = db.execute(q).scalars().first()
    if user is None:
        # Session points at a row that no longer exists. Drop and re-login.
        request.session.clear()
        raise LoginRequired(_next_for(request))
    return user


def required_user(
    request: Request, db: Session = Depends(get_db),
) -> User:
    """Read-only routes."""
    return _lookup_session_user(request, db)


def required_user_committing(
    request: Request, db: Session = Depends(committing_db),
) -> User:
    """Mutation routes — bound to the committing session."""
    return _lookup_session_user(request, db)


def _get_owned_commitment(db: Session, *, cid: str, user: User) -> Commitment:
    c = db.get(Commitment, cid)
    if c is None or c.user_id != user.id:
        raise HTTPException(404, "Commitment not found.")
    return c


def _parse_local_datetime(value: Optional[str], tz_name: Optional[str]) -> Optional[datetime]:
    """Parse an HTML datetime-local value entered in the user's timezone."""
    if not value:
        return None
    try:
        return local_input_to_utc(value, tz_name)
    except ValueError as e:
        raise HTTPException(400, f"Bad datetime: {e}") from e


def _parse_time(value: Optional[str], default: time) -> time:
    if not value:
        return default
    try:
        return time.fromisoformat(value)
    except ValueError as e:
        raise HTTPException(400, f"Bad time: {e}") from e


def _checkbox(value: Optional[str]) -> bool:
    return value in {"on", "true", "1", "yes"}


def _parse_recipients(value: Optional[str]) -> list[str]:
    """Parse a free-text 'to' field into a list of recipient tokens."""
    if not value:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in value.replace(";", ",").split(","):
        p = raw.strip()
        if not p:
            continue
        if p.startswith("<@") and p.endswith(">"):
            p = p[2:-1].split("|", 1)[0]
        elif p.startswith("@"):
            p = p[1:]
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _theme_from_request(request: Request) -> str:
    theme = request.cookies.get("theme", "auto")
    return theme if theme in VALID_THEMES else "auto"


def _state_counts(db: Session, user: User) -> dict[str, int]:
    visible = [s.value for s in CommitmentState]
    counts = {v: 0 for v in visible}
    grouped = db.execute(
        select(Commitment.state, func.count(Commitment.id))
        .where(Commitment.user_id == user.id)
        .group_by(Commitment.state)
    ).all()
    for st, n in grouped:
        key = st.value if hasattr(st, "value") else st
        if key in counts:
            counts[key] = n

    # Re-interpret two tabs to match user-facing semantics:
    #
    #   "active"     = commitments I'm currently doing.
    #                  Includes both ACTIVE (originated by me) and REASSIGNED
    #                  (handed to me, I accepted). A REASSIGNED commitment is
    #                  functionally my work — the label is just a breadcrumb.
    #
    #   "reassigned" = commitments I HANDED OFF (the perspective the user
    #                  asked for). Found via ACCEPTED Reassignment rows
    #                  where I'm the from_user — works for arbitrarily long
    #                  chains because each hop writes its own row.
    counts["active"] = counts.get("active", 0) + counts.pop("reassigned", 0)
    counts["reassigned"] = db.execute(
        select(func.count(func.distinct(Commitment.id)))
        .join(Reassignment, Reassignment.commitment_id == Commitment.id)
        .where(
            Reassignment.from_user_id == user.id,
            Reassignment.status == ReassignmentStatus.ACCEPTED,
            Commitment.user_id != user.id,
        )
    ).scalar() or 0
    return counts


def _flash_redirect(url: str, message: str, level: str = "info") -> RedirectResponse:
    """Redirect with a one-shot 'flash' cookie that the page JS turns into a toast."""
    resp = RedirectResponse(url, status_code=303)
    payload = quote(json.dumps({"message": message, "level": level}))
    resp.set_cookie(
        "flash",
        payload,
        max_age=15,
        httponly=False,
        path="/",
        samesite="lax",
    )
    return resp


def _htmx_toast_headers(message: str, level: str = "info") -> dict[str, str]:
    """HX-Trigger header so HTMX fires a `showToast` event after the response."""
    return {"HX-Trigger": json.dumps({"showToast": {"message": message, "level": level}})}


def _count_oob_html(db: Session, user: User) -> str:
    """HTMX out-of-band fragment that refreshes every tab badge in place."""
    counts = _state_counts(db, user)
    return "".join(
        f'<span class="count" id="count-{state}" hx-swap-oob="true">{n}</span>'
        for state, n in counts.items()
    )


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    """Public landing page. If already signed in, drop straight onto /."""
    if request.session.get("slack_user_id"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {"theme": _theme_from_request(request)},
    )


@router.get("/", response_class=HTMLResponse)
def dashboard_home(
    request: Request,
    state: str = "active",
    user: User = Depends(required_user),
    db: Session = Depends(get_db),
):
    # `state` may refer to a real CommitmentState OR a cross-cutting outcome
    # filter ('success' / 'failed'). Both feed the same template via the
    # `active_state` field.
    outcome_filter: Optional[CommitmentOutcome] = None
    state_enum: Optional[CommitmentState] = None
    if state in ("success", "failed"):
        outcome_filter = CommitmentOutcome(state)
    else:
        try:
            state_enum = CommitmentState(state)
        except ValueError:
            state_enum = CommitmentState.ACTIVE

    # Special "handed-off" view: commitments the user reassigned away and
    # someone accepted. Found via the Reassignment audit table — works for
    # chained reassignments (each hop is a separate row) and survives
    # state changes on the commitment (Bob might have already completed it).
    is_handed_off_view = (state == "reassigned")

    base_q = select(Commitment).where(Commitment.user_id == user.id)
    if outcome_filter is not None:
        rows_q = base_q.where(Commitment.outcome == outcome_filter)
        rows = db.execute(
            rows_q.order_by(Commitment.deadline.is_(None), Commitment.deadline.asc())
        ).scalars().all()
    elif is_handed_off_view:
        # Include DELETED here — if the new owner trashed your handed-off
        # commitment, you should at least see it sitting in the 48h bin so
        # you can react. After the bin purges the row, the join no longer
        # finds anything and it drops out naturally.
        rows = db.execute(
            select(Commitment)
            .join(Reassignment, Reassignment.commitment_id == Commitment.id)
            .where(
                Reassignment.from_user_id == user.id,
                Reassignment.status == ReassignmentStatus.ACCEPTED,
                Commitment.user_id != user.id,
            )
            .distinct()
            .order_by(Commitment.deadline.is_(None), Commitment.deadline.asc())
        ).scalars().all()
    elif state_enum == CommitmentState.ACTIVE:
        # "Active" tab includes REASSIGNED commitments I own — those are
        # things handed TO me that I'm working on. Treating them as live.
        rows = db.execute(
            base_q.where(
                Commitment.state.in_(
                    [CommitmentState.ACTIVE, CommitmentState.REASSIGNED]
                )
            ).order_by(Commitment.deadline.is_(None), Commitment.deadline.asc())
        ).scalars().all()
    else:
        rows = db.execute(
            base_q.where(Commitment.state == state_enum)
            .order_by(Commitment.deadline.is_(None), Commitment.deadline.asc())
        ).scalars().all()

    priorities = db.execute(
        select(PriorityLevel)
        .where(PriorityLevel.user_id == user.id, PriorityLevel.deleted_at.is_(None))
        .order_by(PriorityLevel.name.asc())
    ).scalars().all()

    # Tabs: every real state, plus the two outcome filters as cross-cutting
    # views over terminal commitments.
    visible_states = [s.value for s in CommitmentState] + ["success", "failed"]
    counts = _state_counts(db, user)
    # Outcome counts so the Success / Failed tabs show meaningful badges.
    outcome_rows = db.execute(
        select(Commitment.outcome, func.count(Commitment.id))
        .where(Commitment.user_id == user.id,
               Commitment.outcome.is_not(None))
        .group_by(Commitment.outcome)
    ).all()
    for o, n in outcome_rows:
        key = o.value if hasattr(o, "value") else o
        counts[key] = n

    # Reassignment data — incoming requests show as a banner everywhere.
    incoming = reassign_svc.list_incoming_pending(
        db, workspace_id=user.workspace_id, slack_user_id=user.slack_user_id,
    )
    incoming_view = []
    for r in incoming:
        c_incoming = db.get(Commitment, r.commitment_id)
        if c_incoming is None:
            continue
        sender = db.get(User, r.from_user_id) if r.from_user_id else None
        incoming_view.append({
            "id": r.id,
            "text": c_incoming.text,
            "from_label": sender.display_name or sender.slack_user_id if sender else "Someone",
            "from_slack_user_id": sender.slack_user_id if sender else None,
            "deadline_local": (
                to_local(c_incoming.deadline, user.tz).strftime("%a %b %d, %H:%M %Z")
                if c_incoming.deadline else None
            ),
            "expires_local": to_local(r.expires_at, user.tz).strftime("%a %b %d, %H:%M %Z"),
            "note": r.note,
        })

    # Per-row outgoing reassignment context — under the revised model, a
    # commitment with a PENDING reassignment is ON_HOLD (limbo). Surface the
    # outgoing-pending pill on those rows in the ON_HOLD tab.
    outgoing_by_commitment: dict[str, dict] = {}
    if state_enum == CommitmentState.ON_HOLD:
        for c in rows:
            r = reassign_svc.pending_for_commitment(db, c.id)
            if r is None:
                continue
            target_user = reassign_svc.find_target_in_workspace(
                db, workspace_id=user.workspace_id,
                slack_user_id=r.to_slack_user_id,
            )
            outgoing_by_commitment[c.id] = {
                "id": r.id,
                "target_label": (
                    target_user.display_name or r.to_slack_user_id
                    if target_user else r.to_slack_user_id
                ),
                "expires_local": to_local(r.expires_at, user.tz).strftime("%a %b %d, %H:%M %Z"),
                "note": r.note,
            }

    # Reassign-target dropdown: list EVERY active human in the workspace
    # via Slack's users.list (cached). Marking which ones have actually
    # signed in to CommitBot — un-onboarded members appear in the dropdown
    # but with a hint, since the service layer will reject reassignment to
    # them anyway. This gives the user a Slack-style person picker rather
    # than only people they've already collaborated with via CommitBot.
    teammates = []
    if state_enum in (CommitmentState.ACTIVE, CommitmentState.REASSIGNED):
        from app.slack_app import bolt_app
        members = list_workspace_members(
            bolt_app.client, user.workspace.slack_team_id,
        )
        # Which of them have a User row with signed_in_at set?
        onboarded_ids = {
            r[0] for r in db.execute(
                select(User.slack_user_id).where(
                    User.workspace_id == user.workspace_id,
                    User.signed_in_at.is_not(None),
                )
            ).all()
        }
        # Fallback: if users.list returns nothing (missing scope, network
        # blip), fall back to the onboarded set so the dropdown is still
        # populated with the people we *know* can be reassignment targets.
        if not members:
            onboarded_users = db.execute(
                select(User).where(
                    User.workspace_id == user.workspace_id,
                    User.signed_in_at.is_not(None),
                    User.id != user.id,
                ).order_by(User.display_name.asc())
            ).scalars().all()
            teammates = [
                {
                    "slack_user_id": u.slack_user_id,
                    "label": u.display_name or u.slack_user_id,
                    "onboarded": True,
                }
                for u in onboarded_users
            ]
        else:
            for m in members:
                if m["id"] == user.slack_user_id:
                    continue  # don't suggest the user to themselves
                teammates.append({
                    "slack_user_id": m["id"],
                    "label": m["name"],
                    "onboarded": m["id"] in onboarded_ids,
                })

    # Resolve recipient display names. The DB stores recipients as either
    # raw Slack user IDs (e.g. "U0B4J0LSMRS") or free-text names (e.g.
    # "priya"). In Slack's Home tab a `<@U…>` auto-expands to the name, but
    # the dashboard would otherwise show the ugly ID. We look up matching
    # User rows in this workspace and pre-build a {slack_id → name} map.
    recipient_ids: set[str] = set()
    for c in rows:
        for r in c.recipients:
            if r.is_current and r.recipient_slack_user_id:
                recipient_ids.add(r.recipient_slack_user_id)
    recipient_names: dict[str, str] = {}
    if recipient_ids:
        for u in db.execute(
            select(User).where(
                User.workspace_id == user.workspace_id,
                User.slack_user_id.in_(recipient_ids),
            )
        ).scalars().all():
            if u.display_name:
                recipient_names[u.slack_user_id] = u.display_name

    # For the handed-off view (Reassigned tab from sender perspective),
    # resolve who currently owns each row so we can render "→ now with @bob"
    # instead of "→ now with U0B…".
    current_owner_names: dict[str, str] = {}
    if is_handed_off_view and rows:
        owner_user_ids = {c.user_id for c in rows}
        for u in db.execute(
            select(User).where(User.id.in_(owner_user_ids))
        ).scalars().all():
            current_owner_names[u.id] = u.display_name or u.slack_user_id

    # State pill in the handed-off view. The actual DB state column is
    # owner-perspective ("REASSIGNED" = "I was handed this"), which reads
    # confusingly from the sender side once a chain forms — Alice → Rhea
    # → Amal would show 'reassigned' even though Amal is actively
    # working on it. Translate to sender-perspective labels: ACTIVE and
    # REASSIGNED both collapse to "Active" because they're functionally
    # identical from someone watching from the outside.
    handed_off_state_label: dict[str, str] = {}
    if is_handed_off_view and rows:
        _LABELS = {
            CommitmentState.ACTIVE.value: "Active",
            CommitmentState.REASSIGNED.value: "Active",
            CommitmentState.ON_HOLD.value: "On hold",
            CommitmentState.COMPLETE.value: "Completed",
            CommitmentState.ARCHIVED.value: "Archived",
            CommitmentState.DELETED.value: "Deleted",
        }
        for c in rows:
            handed_off_state_label[c.id] = _LABELS.get(
                c.state.value, c.state.value.replace("_", " ").title(),
            )

    now = datetime.now(timezone.utc)
    user_zone = safe_zone(user.tz)
    urgency = {}
    deadlines_local = {}
    deadline_inputs = {}
    cadences: dict[str, str] = {}
    show_urgency = state_enum in (CommitmentState.ACTIVE, CommitmentState.REASSIGNED)
    levels_by_id = {p.id: p for p in priorities}
    for c in rows:
        local_dl = to_local(c.deadline, user.tz)
        if local_dl is not None:
            deadlines_local[c.id] = local_dl.strftime("%a %b %d, %H:%M %Z")
            deadline_inputs[c.id] = local_dl.strftime("%Y-%m-%dT%H:%M")
        # Cadence only makes sense for live commitments I OWN — for handed-off
        # rows the new owner has their own cadence (we don't know it from
        # here, and showing 'no pings' would mislead the user).
        if (
            c.user_id == user.id
            and c.state in (CommitmentState.ACTIVE, CommitmentState.REASSIGNED)
        ):
            level = levels_by_id.get(c.priority_level_id) if c.priority_level_id else None
            if user.global_pause:
                cadences[c.id] = "paused"
            else:
                interval = ping_svc.current_ping_interval_minutes(
                    c, level, db=db, now=now,
                )
                cadences[c.id] = ping_svc.format_interval(interval) if interval else "no pings"
        if not c.deadline or not show_urgency:
            urgency[c.id] = ("none", "")
            continue
        dl = c.deadline if c.deadline.tzinfo else c.deadline.replace(tzinfo=timezone.utc)
        delta_hours = (dl - now).total_seconds() / 3600
        if delta_hours < 0:
            urgency[c.id] = ("overdue", "Overdue · ")
        elif delta_hours < 24:
            urgency[c.id] = ("due-soon", "Due soon · ")
        else:
            urgency[c.id] = ("none", "")

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "commitments": rows,
            "priorities": priorities,
            "states": visible_states,
            "active_state": state,  # may be a state name or 'success'/'failed'
            "counts": counts,
            "theme": _theme_from_request(request),
            "urgency": urgency,
            "deadlines_local": deadlines_local,
            "deadline_inputs": deadline_inputs,
            "cadences": cadences,
            "user_tz_label": str(user_zone),
            "incoming_reassignments": incoming_view,
            "outgoing_by_commitment": outgoing_by_commitment,
            "teammates": teammates,
            "recipient_names": recipient_names,
            "is_handed_off_view": is_handed_off_view,
            "current_owner_names": current_owner_names,
            "handed_off_state_label": handed_off_state_label,
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    user: User = Depends(required_user),
    db: Session = Depends(get_db),
):
    priorities = db.execute(
        select(PriorityLevel)
        .where(PriorityLevel.user_id == user.id, PriorityLevel.deleted_at.is_(None))
        .order_by(PriorityLevel.is_system_default.desc(), PriorityLevel.name.asc())
    ).scalars().all()
    notations = db.execute(
        select(Notation).where(Notation.user_id == user.id).order_by(Notation.created_at.asc())
    ).scalars().all()

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "user": user,
            "priorities": priorities,
            "notations": notations,
            "max_notations": commit_svc.MAX_NOTATIONS_PER_USER,
            "theme": _theme_from_request(request),
            "tz_options": COMMON_TIMEZONES,
        },
    )


# ---------------------------------------------------------------------------
# Theme toggle (cookie-based — no auth needed)
# ---------------------------------------------------------------------------

@router.post("/theme")
def set_theme(theme: str = Form(...), redirect: str = Form("/")):
    if theme not in VALID_THEMES:
        theme = "auto"
    safe_redirect = redirect if redirect.startswith("/") else "/"
    resp = RedirectResponse(safe_redirect, status_code=303)
    resp.set_cookie(
        "theme", theme,
        max_age=60 * 60 * 24 * 365,
        path="/", samesite="lax",
    )
    return resp


# ---------------------------------------------------------------------------
# Commitment actions
# ---------------------------------------------------------------------------

@router.post("/commitments/new")
def action_new_commitment(
    text: str = Form(...),
    deadline: Optional[str] = Form(None),
    priority_level_id: Optional[str] = Form(None),
    recipients: Optional[str] = Form(None),
    user: User = Depends(required_user_committing),
    db: Session = Depends(committing_db),
):
    try:
        c = commit_svc.create_commitment(
            db,
            owner=user,
            text=text,
            source=CaptureSource.DASHBOARD,
            deadline=_parse_local_datetime(deadline, user.tz),
            priority_level_id=priority_level_id or None,
            recipient_slack_user_ids=_parse_recipients(recipients),
        )
    except ValueError as e:
        return _flash_redirect("/", str(e), "error")
    level = db.get(PriorityLevel, c.priority_level_id) if c.priority_level_id else None
    ping_svc.schedule_initial_ping(db, c, level)
    return _flash_redirect("/", "Commitment logged.", "success")


@router.post("/commitments/{cid}/done")
def action_done(
    cid: str,
    user: User = Depends(required_user_committing),
    db: Session = Depends(committing_db),
):
    c = _get_owned_commitment(db, cid=cid, user=user)
    commit_svc.mark_done(db, c, source=EditSource.DASHBOARD)
    db.flush()
    return HTMLResponse(_count_oob_html(db, user))


@router.post("/commitments/{cid}/reopen")
def action_reopen(
    cid: str,
    user: User = Depends(required_user_committing),
    db: Session = Depends(committing_db),
):
    c = _get_owned_commitment(db, cid=cid, user=user)
    commit_svc.reopen(db, c, source=EditSource.DASHBOARD)
    if c.state == CommitmentState.ACTIVE:
        level = db.get(PriorityLevel, c.priority_level_id) if c.priority_level_id else None
        ping_svc.ensure_pending_ping(db, c, level)
    db.flush()
    return HTMLResponse(_count_oob_html(db, user))


@router.post("/commitments/{cid}/on_hold")
def action_on_hold(
    cid: str,
    resume_at: Optional[str] = Form(None),
    user: User = Depends(required_user_committing),
    db: Session = Depends(committing_db),
):
    c = _get_owned_commitment(db, cid=cid, user=user)
    commit_svc.put_on_hold(
        db, c,
        resume_at=_parse_local_datetime(resume_at, user.tz),
        source=EditSource.DASHBOARD,
    )
    db.flush()
    return HTMLResponse(_count_oob_html(db, user))


@router.post("/commitments/{cid}/resume")
def action_resume(
    cid: str,
    user: User = Depends(required_user_committing),
    db: Session = Depends(committing_db),
):
    c = _get_owned_commitment(db, cid=cid, user=user)
    commit_svc.resume(db, c, source=EditSource.DASHBOARD, manual=True)
    if c.state == CommitmentState.ACTIVE:
        level = db.get(PriorityLevel, c.priority_level_id) if c.priority_level_id else None
        ping_svc.ensure_pending_ping(db, c, level)
    db.flush()
    return HTMLResponse(_count_oob_html(db, user))


@router.post("/commitments/{cid}/delete")
def action_delete(
    cid: str,
    user: User = Depends(required_user_committing),
    db: Session = Depends(committing_db),
):
    c = _get_owned_commitment(db, cid=cid, user=user)
    commit_svc.soft_delete(db, c, source=EditSource.DASHBOARD)
    db.flush()
    return HTMLResponse(_count_oob_html(db, user))


@router.post("/commitments/{cid}/archive")
def action_archive(
    cid: str,
    user: User = Depends(required_user_committing),
    db: Session = Depends(committing_db),
):
    c = _get_owned_commitment(db, cid=cid, user=user)
    try:
        commit_svc.archive(db, c, source=EditSource.DASHBOARD)
    except ValueError as e:
        headers = {**_htmx_toast_headers(str(e), "error"), "HX-Reswap": "none"}
        return HTMLResponse("", headers=headers)
    db.flush()
    return HTMLResponse(_count_oob_html(db, user))


@router.post("/commitments/{cid}/restore")
def action_restore(
    cid: str,
    user: User = Depends(required_user_committing),
    db: Session = Depends(committing_db),
):
    c = _get_owned_commitment(db, cid=cid, user=user)
    commit_svc.restore_from_bin(db, c, source=EditSource.DASHBOARD)
    db.flush()
    return HTMLResponse(_count_oob_html(db, user))


@router.post("/commitments/{cid}/purge")
def action_purge(
    cid: str,
    user: User = Depends(required_user_committing),
    db: Session = Depends(committing_db),
):
    """Hard-delete a commitment immediately, bypassing the 48h bin wait.

    Only valid from the DELETED state — there's no path to "purge an
    active commitment" without going through soft-delete first, since
    we want users to opt into the 'are you sure?' moment that the bin
    represents.
    """
    c = _get_owned_commitment(db, cid=cid, user=user)
    if c.state != CommitmentState.DELETED:
        raise HTTPException(
            400, "Only commitments in the bin can be purged."
        )
    log.info(
        "Manual purge of commitment %s by user %s", c.id, user.slack_user_id,
    )
    commit_svc.hard_delete(db, c)
    db.flush()
    return HTMLResponse(_count_oob_html(db, user))


@router.post("/commitments/{cid}/edit")
def action_edit(
    cid: str,
    text: Optional[str] = Form(None),
    deadline: Optional[str] = Form(None),
    priority_level_id: Optional[str] = Form(None),
    recipients: Optional[str] = Form(None),
    user: User = Depends(required_user_committing),
    db: Session = Depends(committing_db),
):
    c = _get_owned_commitment(db, cid=cid, user=user)

    cadence_dirty = False
    try:
        if text is not None and text.strip():
            commit_svc.edit_text(db, c, text, source=EditSource.DASHBOARD)
        if deadline is not None:
            new_deadline = _parse_local_datetime(deadline, user.tz) if deadline.strip() else None
            old_deadline = c.deadline
            commit_svc.set_deadline(db, c, new_deadline, source=EditSource.DASHBOARD)
            if c.deadline != old_deadline:
                cadence_dirty = True
        if priority_level_id is not None and priority_level_id != "":
            old_pid = c.priority_level_id
            commit_svc.set_priority(db, c, priority_level_id, source=EditSource.DASHBOARD)
            if c.priority_level_id != old_pid:
                cadence_dirty = True
        if recipients is not None:
            commit_svc.set_recipients(
                db, c, _parse_recipients(recipients), source=EditSource.DASHBOARD,
            )
    except ValueError as e:
        return _flash_redirect("/", str(e), "error")

    if cadence_dirty:
        level = db.get(PriorityLevel, c.priority_level_id) if c.priority_level_id else None
        ping_svc.reschedule_next_ping(db, c, level)

    return _flash_redirect("/", "Changes saved.", "success")


# ---------------------------------------------------------------------------
# Reassignment
# ---------------------------------------------------------------------------

def _slack_client_or_none():
    """Best-effort Slack client for outbound DMs from dashboard actions.

    Returns None when running without real credentials (the bolt app is still
    constructed with the placeholder token in dev). The reassignment service
    layer never depends on Slack reachability — DMs are a UX bonus, not part
    of the contract.
    """
    try:
        from app.slack_app import bolt_app
        return bolt_app.client
    except Exception:
        return None


def _send_reassignment_dm(reassignment_id: str) -> None:
    """Open a fresh session and DM the target with the request + buttons.

    Done in a separate session so the original request commits cleanly before
    we make any Slack calls. Best-effort: any Slack failure is logged.
    """
    from app.db import session_scope
    from app.slack_app import (
        _reassignment_request_blocks,
        _home_format_recipients,
    )
    from app.tz import format_deadline as fmt_dl
    client = _slack_client_or_none()
    if client is None:
        return
    try:
        with session_scope() as db:
            r = db.get(Reassignment, reassignment_id)
            if r is None or r.status != ReassignmentStatus.PENDING:
                return
            c = db.get(Commitment, r.commitment_id)
            if c is None or c.user is None:
                return
            payload = {
                "commit_text": c.text,
                "deadline_local": fmt_dl(c.deadline, c.user.tz),
                "recipients_str": _home_format_recipients(c),
                "expires_local": fmt_dl(r.expires_at, c.user.tz),
                "note": r.note,
                "requester_label": f"<@{c.user.slack_user_id}>",
                "target": r.to_slack_user_id,
            }
        resp = client.chat_postMessage(
            channel=payload["target"],
            text=(
                f":incoming_envelope: {payload['requester_label']} wants to "
                f"hand off a commitment: {payload['commit_text']}"
            ),
            blocks=_reassignment_request_blocks(
                reassignment_id=reassignment_id,
                requester_label=payload["requester_label"],
                commit_text=payload["commit_text"],
                deadline_local=payload["deadline_local"],
                recipients_str=payload["recipients_str"],
                expires_local=payload["expires_local"],
                note=payload["note"],
            ),
        )
        # Stash the DM coordinates for chat.update on outcome.
        with session_scope() as db:
            r2 = db.get(Reassignment, reassignment_id)
            if r2 is not None:
                r2.notice_channel_id = resp.get("channel")
                r2.notice_message_ts = resp.get("ts")
    except Exception:
        log.exception("Sending reassignment DM failed for %s", reassignment_id)


@router.post("/commitments/{cid}/reassign")
def action_reassign(
    cid: str,
    to_slack_user_id: str = Form(...),
    note: Optional[str] = Form(None),
    user: User = Depends(required_user_committing),
    db: Session = Depends(committing_db),
):
    c = _get_owned_commitment(db, cid=cid, user=user)
    try:
        r = reassign_svc.request_reassignment(
            db,
            commitment=c,
            target_slack_user_id=to_slack_user_id,
            source=EditSource.DASHBOARD,
            note=note,
        )
    except ValueError as e:
        return _flash_redirect("/", str(e), "error")
    reassignment_id = r.id
    db.flush()
    # Fire the DM after the route commits. The route's committing_db dep flushes
    # at function-return, so we schedule the DM call after we return.
    # Simplest: do it inline now (small extra latency, but reliable).
    _send_reassignment_dm(reassignment_id)
    return _flash_redirect(
        "/?state=reassigned",
        "Reassignment request sent — they have 24 hours to respond.",
        "success",
    )


@router.post("/reassignments/{rid}/cancel")
def action_cancel_reassignment(
    rid: str,
    user: User = Depends(required_user_committing),
    db: Session = Depends(committing_db),
):
    r = db.get(Reassignment, rid)
    if r is None:
        raise HTTPException(404)
    try:
        reassign_svc.cancel_reassignment(
            db, reassignment=r, actor=user, source=EditSource.DASHBOARD,
        )
    except ValueError as e:
        return _flash_redirect("/?state=reassigned", str(e), "error")
    # Best-effort: retire the recipient's DM buttons.
    client = _slack_client_or_none()
    if client and r.notice_channel_id and r.notice_message_ts:
        try:
            from app.slack_app import retire_reassignment_dm
            c = db.get(Commitment, r.commitment_id)
            retire_reassignment_dm(
                client,
                channel=r.notice_channel_id,
                ts=r.notice_message_ts,
                final_text=(
                    f":x: This reassignment of *{c.text if c else 'the commitment'}* "
                    "was cancelled by the sender."
                ),
            )
        except Exception:
            log.exception("retire DM failed on cancel")
    return _flash_redirect("/", "Reassignment cancelled.", "success")


@router.post("/reassignments/{rid}/accept")
def action_accept_reassignment(
    rid: str,
    user: User = Depends(required_user_committing),
    db: Session = Depends(committing_db),
):
    r = db.get(Reassignment, rid)
    if r is None:
        raise HTTPException(404)
    try:
        reassign_svc.accept_reassignment(
            db, reassignment=r, actor=user, source=EditSource.DASHBOARD,
        )
    except ValueError as e:
        return _flash_redirect("/", str(e), "error")
    # Best-effort outcome notification + DM cleanup.
    client = _slack_client_or_none()
    if client:
        try:
            c = db.get(Commitment, r.commitment_id)
            owner = db.get(User, r.from_user_id) if r.from_user_id else None
            if r.notice_channel_id and r.notice_message_ts:
                from app.slack_app import retire_reassignment_dm
                retire_reassignment_dm(
                    client,
                    channel=r.notice_channel_id,
                    ts=r.notice_message_ts,
                    final_text=(
                        f":white_check_mark: You accepted *"
                        f"{c.text if c else 'the commitment'}*."
                    ),
                )
            if owner:
                client.chat_postMessage(
                    channel=owner.slack_user_id,
                    text=(
                        f":white_check_mark: <@{user.slack_user_id}> accepted "
                        f"your reassignment of *{c.text if c else 'the commitment'}*. "
                        "It's now their commitment."
                    ),
                )
        except Exception:
            log.exception("accept-side notifications failed")
    return _flash_redirect(
        "/", "Reassignment accepted — it's yours now.", "success",
    )


@router.post("/reassignments/{rid}/decline")
def action_decline_reassignment(
    rid: str,
    user: User = Depends(required_user_committing),
    db: Session = Depends(committing_db),
):
    r = db.get(Reassignment, rid)
    if r is None:
        raise HTTPException(404)
    try:
        reassign_svc.decline_reassignment(
            db, reassignment=r, actor=user, source=EditSource.DASHBOARD,
        )
    except ValueError as e:
        return _flash_redirect("/", str(e), "error")
    client = _slack_client_or_none()
    if client:
        try:
            c = db.get(Commitment, r.commitment_id)
            owner = db.get(User, r.from_user_id) if r.from_user_id else None
            if r.notice_channel_id and r.notice_message_ts:
                from app.slack_app import retire_reassignment_dm
                retire_reassignment_dm(
                    client,
                    channel=r.notice_channel_id,
                    ts=r.notice_message_ts,
                    final_text=(
                        f":no_entry_sign: You declined *"
                        f"{c.text if c else 'the commitment'}*."
                    ),
                )
            if owner:
                client.chat_postMessage(
                    channel=owner.slack_user_id,
                    text=(
                        f":no_entry_sign: <@{user.slack_user_id}> declined your "
                        f"reassignment of *{c.text if c else 'the commitment'}*. "
                        "It's back on your plate."
                    ),
                )
        except Exception:
            log.exception("decline-side notifications failed")
    return _flash_redirect("/", "Reassignment declined.", "success")


# ---------------------------------------------------------------------------
# User preferences
# ---------------------------------------------------------------------------

@router.post("/settings/preferences")
def update_preferences(
    global_pause: Optional[str] = Form(None),
    reaction_signal_enabled: Optional[str] = Form(None),
    threaded_confirm_enabled: Optional[str] = Form(None),
    auto_delete_completed_after_days: int = Form(...),
    auto_resume_hours_before_deadline: int = Form(24),
    start_of_day: Optional[str] = Form(None),
    tz: Optional[str] = Form(None),
    user: User = Depends(required_user_committing),
    db: Session = Depends(committing_db),
):
    if auto_delete_completed_after_days < 0 or auto_delete_completed_after_days > 3650:
        return _flash_redirect(
            "/settings",
            "Auto-delete must be 0–3650 days. Use 0 to archive instead of delete.",
            "error",
        )
    if auto_resume_hours_before_deadline < 0 or auto_resume_hours_before_deadline > 720:
        return _flash_redirect(
            "/settings",
            "Auto-resume must be 0–720 hours (0 disables it).",
            "error",
        )
    if tz is not None:
        try:
            user.tz = validate_zone(tz)
        except ValueError as e:
            return _flash_redirect("/settings", str(e), "error")
    user.global_pause = _checkbox(global_pause)
    user.reaction_signal_enabled = _checkbox(reaction_signal_enabled)
    user.threaded_confirm_enabled = _checkbox(threaded_confirm_enabled)
    user.auto_delete_completed_after_days = auto_delete_completed_after_days
    user.auto_resume_hours_before_deadline = auto_resume_hours_before_deadline
    user.start_of_day = _parse_time(start_of_day, user.start_of_day)
    return _flash_redirect("/settings", "Preferences saved.", "success")


# ---------------------------------------------------------------------------
# Priority-level CRUD
# ---------------------------------------------------------------------------

@router.post("/settings/priority/new")
def new_priority(
    name: str = Form(...),
    color: str = Form("#888888"),
    base_ping_interval_minutes: int = Form(240),
    escalation_trigger_hours_before_deadline: int = Form(24),
    max_ping_frequency_minutes: int = Form(30),
    escalation_rate: float = Form(2.0),
    user: User = Depends(required_user_committing),
    db: Session = Depends(committing_db),
):
    try:
        pl = commit_svc.create_priority_level(
            db, user=user,
            name=name, color=color,
            base_ping_interval_minutes=base_ping_interval_minutes,
            escalation_trigger_hours_before_deadline=escalation_trigger_hours_before_deadline,
            max_ping_frequency_minutes=max_ping_frequency_minutes,
            escalation_rate=escalation_rate,
        )
    except ValueError as e:
        return _flash_redirect("/settings", str(e), "error")
    return _flash_redirect("/settings", f"Priority level \"{pl.name}\" created.", "success")


@router.post("/settings/priority/{pid}/delete")
def delete_priority(
    pid: str,
    user: User = Depends(required_user_committing),
    db: Session = Depends(committing_db),
):
    pl = db.get(PriorityLevel, pid)
    if pl is None or pl.user_id != user.id:
        raise HTTPException(404)
    name = pl.name
    try:
        commit_svc.soft_delete_priority_level(db, pl)
    except ValueError as e:
        return _flash_redirect("/settings", str(e), "error")
    return _flash_redirect("/settings", f"Deleted priority \"{name}\".", "success")


# ---------------------------------------------------------------------------
# Notations
# ---------------------------------------------------------------------------

@router.post("/notations")
def create_notation(
    pattern: str = Form(...),
    user: User = Depends(required_user_committing),
    db: Session = Depends(committing_db),
):
    existing = db.execute(
        select(Notation).where(Notation.user_id == user.id)
    ).scalars().all()
    if len(existing) >= commit_svc.MAX_NOTATIONS_PER_USER:
        return _flash_redirect(
            "/settings",
            f"Maximum {commit_svc.MAX_NOTATIONS_PER_USER} notations reached. Delete one to add another.",
            "error",
        )
    try:
        commit_svc.validate_notation_pattern(pattern)
    except ValueError as e:
        return _flash_redirect("/settings", str(e), "error")
    db.add(Notation(user_id=user.id, pattern=pattern, enabled=True))
    invalidate_notation_cache(user.id)
    return _flash_redirect("/settings", "Notation added.", "success")


@router.post("/notations/{nid}/delete")
def delete_notation(
    nid: str,
    user: User = Depends(required_user_committing),
    db: Session = Depends(committing_db),
):
    n = db.get(Notation, nid)
    if n is None or n.user_id != user.id:
        raise HTTPException(404)
    db.delete(n)
    invalidate_notation_cache(user.id)
    return _flash_redirect("/settings", "Notation removed.", "success")


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

def _priority_dict(pl: Optional[PriorityLevel]) -> Optional[dict]:
    if pl is None:
        return None
    return {
        "id": pl.id,
        "name": pl.name,
        "color": pl.color,
        "base_ping_interval_minutes": pl.base_ping_interval_minutes,
        "deleted": pl.deleted_at is not None,
    }


@router.get("/export/json")
def export_json(
    user: User = Depends(required_user),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        select(Commitment).where(Commitment.user_id == user.id)
    ).scalars().all()
    out = []
    for c in rows:
        pl = db.get(PriorityLevel, c.priority_level_id) if c.priority_level_id else None
        out.append({
            "id": c.id,
            "text": c.text,
            "state": c.state.value,
            "source": c.source.value,
            "deadline": c.deadline.isoformat() if c.deadline else None,
            "priority": _priority_dict(pl),
            "escalation_enabled": c.escalation_enabled,
            "recipients": [r.recipient_slack_user_id for r in c.recipients if r.is_current],
            "version": c.version,
            "last_writer": c.last_writer,
            "created_at": c.created_at.isoformat(),
            "completed_at": c.completed_at.isoformat() if c.completed_at else None,
        })
    return JSONResponse(out)


@router.get("/export/csv")
def export_csv(
    user: User = Depends(required_user),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        select(Commitment).where(Commitment.user_id == user.id)
    ).scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "text", "state", "source", "deadline",
        "priority_name", "priority_id",
        "recipients", "version", "last_writer", "created_at", "completed_at",
    ])
    for c in rows:
        pl = db.get(PriorityLevel, c.priority_level_id) if c.priority_level_id else None
        writer.writerow([
            c.id, c.text, c.state.value, c.source.value,
            c.deadline.isoformat() if c.deadline else "",
            pl.name if pl else "",
            c.priority_level_id or "",
            "|".join(r.recipient_slack_user_id for r in c.recipients if r.is_current and r.recipient_slack_user_id),
            c.version, c.last_writer or "",
            c.created_at.isoformat(),
            c.completed_at.isoformat() if c.completed_at else "",
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="commitbot-{user.slack_user_id}.csv"'},
    )
