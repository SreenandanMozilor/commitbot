"""
Slack Bolt application.

Wires up the three corrected capture methods from the revised spec:

  A. Slash command `/commit <text>`           → CaptureSource.SLASH_COMMAND
  B. Custom notation in message text          → CaptureSource.NOTATION
  C. Message shortcut "Mark as commitment"    → CaptureSource.MESSAGE_SHORTCUT

Plus:
  - App Home tab (replaces the "personal commitments channel" idea — F4)
  - Inline ping actions (done / snooze 2h / snooze tomorrow / stop-escalation)

The HTTP entrypoint for Slack is exposed via Bolt's SlackRequestHandler;
mounted into FastAPI in `app.main`.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from time import time as _time
from typing import Any, Optional

from slack_bolt import App
from slack_bolt.adapter.fastapi import SlackRequestHandler
from slack_sdk.errors import SlackApiError
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.models import (
    CaptureSource,
    Commitment,
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
from app.tz import format_deadline, safe_zone, to_local

log = logging.getLogger(__name__)
settings = get_settings()

# Guards the instant-trigger thread so two rapid messages from the same user
# don't spawn parallel scans of the same buffer rows. A scan in progress is
# enough — when it finishes it drains the whole pending buffer anyway.
_instant_scan_inflight: set[str] = set()
_instant_scan_lock = threading.Lock()

# In dev/test we don't have real Slack credentials. We still build the app so
# the routes are wired; Bolt's signature verification will reject anything real
# until credentials are provided. `token_verification_enabled=False` avoids
# the `auth.test` call at boot.
bolt_app = App(
    token=settings.slack_bot_token,
    signing_secret=settings.slack_signing_secret,
    token_verification_enabled=False,
    process_before_response=True,
)
slack_request_handler = SlackRequestHandler(bolt_app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Slack mention parsing: `<@U12345>` and `<@U12345|displayname>` — what Slack
# auto-substitutes when a real user is tab-completed.
MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]+)?>")

# Plain text mention like `@sree` or `[@sree]`. Skips emails by requiring the
# `@` to be preceded by a non-word character (or start-of-string), and the name
# itself must start with a letter to avoid `@1`/`@-` noise.
PLAIN_MENTION_RE = re.compile(r"(?<![\w<])@([A-Za-z][\w\-.]{0,63})")


def _extract_mentions(text: str) -> list[str]:
    """Return recipient tokens found in `text`, deduped, in order of appearance.

    Picks up two forms:
      - Slack's `<@U12345>` auto-substituted mentions → returns the user ID.
      - Plain `@name` tokens the user typed without tab-completion → returns
        the name. These are stored as free-text recipients (matching the
        dashboard's behavior) and rendered as `@name` in the Home view.
    """
    text = text or ""
    seen: set[str] = set()
    out: list[str] = []
    for m in MENTION_RE.finditer(text):
        v = m.group(1)
        if v not in seen:
            seen.add(v)
            out.append(v)
    # Strip Slack-mention spans so their inner contents (e.g. a `|display`
    # alias) don't get re-matched as plain `@name` tokens.
    cleaned = MENTION_RE.sub(" ", text)
    for m in PLAIN_MENTION_RE.finditer(cleaned):
        v = m.group(1)
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _get_or_provision_user(db, *, slack_team_id: str, slack_user_id: str) -> User:
    """Find the User for this (workspace, slack_user_id), creating both if needed."""
    ws = db.execute(select(Workspace).where(Workspace.slack_team_id == slack_team_id)).scalar_one_or_none()
    if ws is None:
        ws = Workspace(slack_team_id=slack_team_id, bot_token=settings.slack_bot_token)
        db.add(ws)
        db.flush()

    u = db.execute(
        select(User).where(User.workspace_id == ws.id, User.slack_user_id == slack_user_id)
    ).scalar_one_or_none()
    if u is None:
        u = User(workspace_id=ws.id, slack_user_id=slack_user_id)
        db.add(u)
        db.flush()
        # Seed a default priority level so new users have something to assign to.
        db.add(PriorityLevel(
            user_id=u.id, name="Normal", color="#4a90e2",
            base_ping_interval_minutes=240, escalation_trigger_hours_before_deadline=24,
            max_ping_frequency_minutes=30, escalation_rate=2.0,
            is_system_default=True,
        ))
        db.flush()
    return u


def _find_user(db, *, slack_team_id: str, slack_user_id: str) -> Optional[User]:
    """Read-only lookup — does NOT provision. Used in hot paths to avoid write churn."""
    return db.execute(
        select(User).join(Workspace).where(
            Workspace.slack_team_id == slack_team_id,
            User.slack_user_id == slack_user_id,
        )
    ).scalar_one_or_none()


def _is_onboarded(user: Optional[User]) -> bool:
    """True iff the user has completed Sign in with Slack at least once.

    The Slack capture paths refuse to log commitments for un-onboarded users —
    otherwise we'd accumulate data they can't access (they need a session to
    see the dashboard, and they can only get one by signing in).
    """
    return user is not None and user.signed_in_at is not None


def _onboarding_nudge_text() -> str:
    return (
        ":wave: Welcome to CommitBot!\n"
        "Before logging commitments, sign in once at "
        f"<{settings.app_base_url}/auth/slack/login|the dashboard>. "
        "That's what links Slack captures to *your* Home tab and dashboard — "
        "without it, anything you `/commit` lives in a row you can't open."
    )


# `chat.postEphemeral` requires the bot to be a member of the channel. In private
# channels (and any channel the bot quietly lost access to) Slack returns
# `channel_not_found` and a naive call raises SlackApiError, propagating to a
# 500 from the webhook listener. DM the invoking user as a fallback — DMs always
# work because they've interacted with the app.
def _safe_ephemeral(
    client: Any, *, channel: str, user: str, text: str, blocks: Optional[list] = None
) -> None:
    kwargs: dict[str, Any] = {"channel": channel, "user": user, "text": text}
    if blocks is not None:
        kwargs["blocks"] = blocks
    try:
        client.chat_postEphemeral(**kwargs)
        return
    except SlackApiError as e:
        err = (e.response or {}).get("error")
        if err not in {"channel_not_found", "not_in_channel", "is_archived"}:
            log.warning("chat_postEphemeral failed (%s); falling back to DM", err)
    except Exception:
        log.exception("chat_postEphemeral failed unexpectedly; falling back to DM")

    dm_kwargs: dict[str, Any] = {"channel": user, "text": text}
    if blocks is not None:
        dm_kwargs["blocks"] = blocks
    try:
        client.chat_postMessage(**dm_kwargs)
    except Exception:
        log.exception("DM fallback also failed for user=%s", user)


# --- Notation regex cache --------------------------------------------------
# Compiling regexes on every message event is wasteful and bad patterns would
# log noise per message. We cache a list of (compiled_pattern, original_string)
# per user-id, invalidated on a 60-second clock so dashboard edits show up
# without an app restart.

_NOTATION_CACHE: dict[str, tuple[float, list[tuple[re.Pattern, str]]]] = {}
_NOTATION_TTL_SEC = 60.0


def _get_compiled_notations(db, user_id: str) -> list[tuple[re.Pattern, str]]:
    now = _time()
    cached = _NOTATION_CACHE.get(user_id)
    if cached is not None and now - cached[0] < _NOTATION_TTL_SEC:
        return cached[1]

    rows = db.execute(
        select(Notation).where(Notation.user_id == user_id, Notation.enabled.is_(True))
    ).scalars().all()
    compiled: list[tuple[re.Pattern, str]] = []
    for n in rows:
        try:
            compiled.append((re.compile(n.pattern), n.pattern))
        except re.error as e:
            log.warning("Invalid notation regex for user=%s pattern=%r: %s", user_id, n.pattern, e)
    _NOTATION_CACHE[user_id] = (now, compiled)
    return compiled


def invalidate_notation_cache(user_id: str) -> None:
    """Called by the dashboard after adding/removing a notation."""
    _NOTATION_CACHE.pop(user_id, None)


# --- Workspace member list ------------------------------------------------
# A 5-minute in-memory cache of `users.list` results so the reassign-target
# dropdown can show every workspace member (not just ones who've signed in
# to CommitBot). Bot needs the `users:read` scope, which the standard
# install already grants.

_MEMBERS_CACHE: dict[str, tuple[float, list[dict]]] = {}
_MEMBERS_TTL_SEC = 300.0


def list_workspace_members(client: Any, team_id: str) -> list[dict]:
    """Return [{id, name}, …] for every active human in the workspace.

    Filters out: deleted accounts, bots/app users, Slackbot. Caches per
    team for 5 min so opening the dashboard isn't gated on a Slack API
    round-trip. On any API failure (missing scope, network) returns an
    empty list — the caller should fall back to whatever list it has.
    """
    cached = _MEMBERS_CACHE.get(team_id)
    now = _time()
    if cached is not None and now - cached[0] < _MEMBERS_TTL_SEC:
        return cached[1]

    members: list[dict] = []
    try:
        cursor: Optional[str] = None
        while True:
            resp = client.users_list(cursor=cursor, limit=200)
            for u in resp.get("members", []):
                if u.get("deleted") or u.get("is_bot") or u.get("is_app_user"):
                    continue
                if u.get("id") == "USLACKBOT":
                    continue
                profile = u.get("profile", {}) or {}
                label = (
                    profile.get("display_name_normalized")
                    or profile.get("display_name")
                    or profile.get("real_name_normalized")
                    or profile.get("real_name")
                    or u.get("real_name")
                    or u["id"]
                )
                members.append({"id": u["id"], "name": label})
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
    except Exception:
        log.exception("users.list failed for team %s", team_id)
        return []

    members.sort(key=lambda m: m["name"].lower())
    _MEMBERS_CACHE[team_id] = (now, members)
    return members


def invalidate_member_cache(team_id: Optional[str] = None) -> None:
    """Force a re-fetch on the next call. Useful from tests / admin endpoints."""
    if team_id is None:
        _MEMBERS_CACHE.clear()
    else:
        _MEMBERS_CACHE.pop(team_id, None)


# ---------------------------------------------------------------------------
# Method A: slash command  /commit <text>
# ---------------------------------------------------------------------------

@bolt_app.command("/commit")
def handle_commit_slash(ack, body, client, logger):
    """Slash command — posts the message AND logs the commitment in one step."""
    ack()  # 3-second deadline — acknowledge immediately

    team_id = body.get("team_id")
    user_id = body.get("user_id")
    channel_id = body.get("channel_id")
    raw_text = (body.get("text") or "").strip()

    # Help
    if not raw_text or raw_text.lower() in {"help", "?", "-h", "--help"}:
        _safe_ephemeral(
            client, channel=channel_id, user=user_id,
            text=(
                ":wave: *CommitBot — slash command*\n"
                "`/commit <text>` — log a commitment and broadcast it in this channel.\n"
                "Examples:\n"
                "  • `/commit I'll send the spec by Friday`\n"
                "  • `/commit Reply to @priya by EOD`\n"
                "Set deadlines + priorities from the app's *Home* tab or the web dashboard."
            ),
        )
        return

    # Onboarding gate. Refuse to log commitments for users who haven't signed
    # in to the dashboard — their data would be invisible to them. Check
    # BEFORE posting the public message so we don't broadcast a "commit" that
    # we won't actually record.
    with session_scope() as db:
        existing = _find_user(db, slack_team_id=team_id, slack_user_id=user_id)
        is_onboarded = _is_onboarded(existing)
        existing_signed = existing.signed_in_at if existing else None
    log.info(
        "/commit gate: team=%s user=%s found=%s signed_in_at=%s",
        team_id, user_id, existing is not None, existing_signed,
    )
    if not is_onboarded:
        _safe_ephemeral(
            client, channel=channel_id, user=user_id,
            text=_onboarding_nudge_text(),
        )
        return

    recipients = _extract_mentions(raw_text)

    # Post the message as the bot citing the user. (Posting as the user
    # requires a user-token scope we don't ask for; this is the standard
    # one-step pattern.)
    posted = client.chat_postMessage(
        channel=channel_id,
        text=f"<@{user_id}> committed: {raw_text}",
    )

    posted_ts = posted.get("ts")
    with session_scope() as db:
        owner = _get_or_provision_user(db, slack_team_id=team_id, slack_user_id=user_id)
        try:
            c = commit_svc.create_commitment(
                db,
                owner=owner,
                text=raw_text,
                source=CaptureSource.SLASH_COMMAND,
                slack_channel_id=channel_id,
                slack_message_ts=posted_ts,
                recipient_slack_user_ids=recipients,
            )
        except ValueError as e:
            # We've already publicly posted the "Alice committed: …" message
            # but the DB refused (e.g. text too long). Delete the channel
            # post so we don't leave an orphan claim no commitment row
            # backs. The bot owns the message it just posted, so chat.delete
            # is allowed.
            try:
                client.chat_delete(channel=channel_id, ts=posted_ts)
            except Exception:
                log.warning(
                    "Couldn't delete orphan /commit post channel=%s ts=%s",
                    channel_id, posted_ts,
                )
            _safe_ephemeral(client, channel=channel_id, user=user_id, text=f":warning: {e}")
            return
        level = db.get(PriorityLevel, c.priority_level_id) if c.priority_level_id else None
        ping_svc.schedule_initial_ping(db, c, level)
        commit_id = c.id
        owner_team_id = owner.workspace.slack_team_id
        owner_slack_user_id = owner.slack_user_id

    # Threaded confirmation. The slash command is explicit user intent, so we
    # always confirm here regardless of the per-user threaded-confirm setting
    # (which governs the *passive* notation pathway).
    try:
        client.chat_postMessage(
            channel=channel_id, thread_ts=posted_ts,
            text=":white_check_mark: Logged. Open *Home* to set a deadline.",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn",
                    "text": ":white_check_mark: *Logged as a commitment.*"}},
                {"type": "actions", "elements": [
                    {"type": "button", "action_id": f"done::{commit_id}",
                     "text": {"type": "plain_text", "text": "Done ✓"}, "style": "primary"},
                ]},
            ],
        )
    except Exception:
        log.warning("Could not post threaded confirmation in %s", channel_id)

    _refresh_home(client, team_id=owner_team_id, slack_user_id=owner_slack_user_id)


# ---------------------------------------------------------------------------
# Method B: custom notation
# ---------------------------------------------------------------------------

@bolt_app.event("message")
def handle_message_for_notation(event, client, logger):
    """
    Listen to messages and check for user-defined notations. The bot must have
    the relevant message scopes (message.channels, message.im, message.groups,
    message.mpim) and be a member of the channel.

    Provisioning rule: we DO NOT auto-create a User row just because we saw
    their message. We only create one when the message actually matches a
    notation belonging to an existing user (or, for the slash-command path,
    when the user is invoking us deliberately).
    """
    # Ignore edits, deletions, and the bot's own messages.
    if event.get("subtype") in {"message_changed", "message_deleted", "bot_message"}:
        return
    if event.get("bot_id"):
        return

    team_id = event.get("team")
    user_slack_id = event.get("user")
    channel_id = event.get("channel")
    ts = event.get("ts")
    text = event.get("text") or ""

    if not (team_id and user_slack_id and ts and text):
        return

    with session_scope() as db:
        owner = _find_user(db, slack_team_id=team_id, slack_user_id=user_slack_id)
        if not _is_onboarded(owner):
            # No User row (bystander) OR row exists but they never signed in.
            # Stay silent — notations are passive and shouldn't surface a
            # nudge for every message in every channel the bot is in.
            return

        # Agent buffer comes BEFORE the notation match. We want every
        # message from an opted-in user available for batched
        # classification, regardless of whether it also matched a notation.
        # If the same message later gets captured as both NOTATION and
        # AGENT, the (workspace, channel, ts) dedup constraint inside
        # `create_commitment` ensures only one row exists.
        trigger_instant_scan = False
        if owner.agent_enabled:
            from app.services import agent as agent_svc
            agent_svc.buffer_message(
                db, user=owner, channel_id=channel_id,
                message_ts=ts, text=text,
            )
            # Cheap regex pre-filter: if this message *might* be a
            # commitment, drain the user's buffer through the real LLM
            # right now instead of waiting for the next scheduled tick.
            # False positives here are fine — the LLM is the final
            # arbiter; the stub just decides whether to spend an API call.
            if agent_svc.is_likely_candidate(text):
                trigger_instant_scan = True

        compiled = _get_compiled_notations(db, owner.id)
        if not compiled:
            return

        # Find the first notation that matches and remember its raw pattern —
        # recipient extraction is OPT-IN per notation: only patterns that
        # contain `@` opt their captures into @-mention parsing.
        matched_pattern: Optional[str] = None
        for p, original in compiled:
            if p.search(text):
                matched_pattern = original
                break
        if matched_pattern is None:
            return

        recipients = _extract_mentions(text) if "@" in matched_pattern else []
        try:
            c = commit_svc.create_commitment(
                db,
                owner=owner,
                text=text,
                source=CaptureSource.NOTATION,
                slack_channel_id=channel_id,
                slack_message_ts=ts,
                recipient_slack_user_ids=recipients,
            )
        except ValueError as e:
            log.warning("Notation capture rejected: %s", e)
            return

        level = db.get(PriorityLevel, c.priority_level_id) if c.priority_level_id else None
        ping_svc.schedule_initial_ping(db, c, level)
        confirm = owner.threaded_confirm_enabled
        react = owner.reaction_signal_enabled

    # Side-effecting Slack calls outside the DB transaction.
    if confirm:
        try:
            client.chat_postMessage(
                channel=channel_id, thread_ts=ts,
                text=":white_check_mark: Logged as a commitment.",
            )
        except Exception:
            log.warning("Could not post threaded confirmation in %s", channel_id)

    if react:
        try:
            client.reactions_add(channel=channel_id, timestamp=ts, name="bookmark_tabs")
        except Exception:
            pass  # already reacted, or no permission

    _refresh_home(client, team_id=team_id, slack_user_id=user_slack_id)

    if trigger_instant_scan:
        _spawn_instant_scan(client, team_id=team_id, slack_user_id=user_slack_id)


def _spawn_instant_scan(client, *, team_id: str, slack_user_id: str) -> None:
    """Fire-and-forget: drain the user's agent buffer through the LLM now.

    Deduped per user — a second message arriving while a scan is already
    running just returns, because the in-flight scan will pick the new
    buffer row up on its single drain.
    """
    key = f"{team_id}:{slack_user_id}"
    with _instant_scan_lock:
        if key in _instant_scan_inflight:
            return
        _instant_scan_inflight.add(key)

    def _do_scan():
        try:
            from app.services import agent as agent_svc
            from app.services.llm import get_provider

            provider = get_provider()
            with session_scope() as db:
                owner = _find_user(db, slack_team_id=team_id, slack_user_id=slack_user_id)
                if not (_is_onboarded(owner) and owner.agent_enabled):
                    return
                created = agent_svc.scan_user(db, owner, provider=provider)
            if created:
                _refresh_home(client, team_id=team_id, slack_user_id=slack_user_id)
        except Exception:
            log.exception("instant agent scan failed for %s/%s", team_id, slack_user_id)
        finally:
            with _instant_scan_lock:
                _instant_scan_inflight.discard(key)

    threading.Thread(target=_do_scan, daemon=True).start()


# ---------------------------------------------------------------------------
# Method C: message shortcut (right-click → "Mark as commitment")
# ---------------------------------------------------------------------------

@bolt_app.shortcut("mark_as_commitment")
def handle_message_shortcut(ack, shortcut, client):
    ack()
    team_id = shortcut["team"]["id"]
    invoking_user = shortcut["user"]["id"]
    message = shortcut["message"]
    channel_id = shortcut["channel"]["id"]
    ts = message["ts"]
    text = message.get("text", "")

    # Same onboarding gate as `/commit` — block before any DB write.
    with session_scope() as db:
        existing = _find_user(db, slack_team_id=team_id, slack_user_id=invoking_user)
        is_onboarded = _is_onboarded(existing)
    if not is_onboarded:
        _safe_ephemeral(
            client, channel=channel_id, user=invoking_user,
            text=_onboarding_nudge_text(),
        )
        return

    with session_scope() as db:
        owner = _get_or_provision_user(db, slack_team_id=team_id, slack_user_id=invoking_user)

        # Recipients are always extracted from @-mentions in the message
        # text — same rule whether the message was yours or someone else's.
        # (An earlier design implicitly added the message author as
        # recipient when right-clicking someone else's message. That was
        # semantically misleading: the commitment was still owned by you,
        # and the recipient pill rendered as "→ @them", which read as if
        # you owed them. To track "they owe me X", capture the commitment
        # first and then reassign it to them via the reassignment flow.)
        recipients = _extract_mentions(text)

        try:
            c = commit_svc.create_commitment(
                db,
                owner=owner,
                text=text,
                source=CaptureSource.MESSAGE_SHORTCUT,
                slack_channel_id=channel_id,
                slack_message_ts=ts,
                recipient_slack_user_ids=recipients,
            )
        except ValueError as e:
            _safe_ephemeral(client, channel=channel_id, user=invoking_user,
                            text=f":warning: {e}")
            return
        level = db.get(PriorityLevel, c.priority_level_id) if c.priority_level_id else None
        ping_svc.schedule_initial_ping(db, c, level)
        owner_team_id = owner.workspace.slack_team_id
        owner_slack_user_id = owner.slack_user_id

    _safe_ephemeral(
        client, channel=channel_id, user=invoking_user,
        text=":white_check_mark: Logged as a commitment. Set a deadline in the app's *Home* tab.",
    )
    _refresh_home(client, team_id=owner_team_id, slack_user_id=owner_slack_user_id)


# ---------------------------------------------------------------------------
# App Home — replaces "personal commitments channel" (F4)
# ---------------------------------------------------------------------------

_HOME_SLACK_ID_RE = re.compile(r"^[UW][A-Z0-9]{6,}$")


def _home_format_recipients(c: Commitment) -> str:
    rec_tokens = [
        r.recipient_slack_user_id for r in c.recipients
        if r.is_current and r.recipient_slack_user_id
    ]
    formatted = [
        f"<@{tok}>" if _HOME_SLACK_ID_RE.match(tok) else f"@{tok}"
        for tok in rec_tokens
    ]
    return ", ".join(formatted)


def _home_active_blocks(db, owner: User, active: list[Commitment]) -> list[dict]:
    """Render the standard active-commitments section (the bulk of the Home)."""
    out: list[dict] = []
    for idx, c in enumerate(active[:20]):
        level = db.get(PriorityLevel, c.priority_level_id) if c.priority_level_id else None
        interval = ping_svc.current_ping_interval_minutes(c, level, db=db)
        at_floor = ping_svc.is_at_escalation_floor(c, level, db=db)
        cadence_str = ping_svc.format_interval(interval) if interval else "no pings"
        recipients_str = _home_format_recipients(c)

        meta_parts = []
        if c.deadline:
            meta_parts.append(f":calendar: {format_deadline(c.deadline, owner.tz)}")
        else:
            meta_parts.append(":calendar: no deadline")
        meta_parts.append(f":bell: {cadence_str}")
        if not c.escalation_enabled:
            meta_parts.append(":pause_button: escalation off")
        if recipients_str:
            meta_parts.append(f":arrow_right: {recipients_str}")

        out.append({"type": "section",
                    "text": {"type": "mrkdwn", "text": f"*{c.text[:200]}*"}})
        out.append({"type": "context",
                    "elements": [{"type": "mrkdwn", "text": "   ".join(meta_parts)}]})

        action_elements: list[dict] = [
            {"type": "button", "action_id": f"setdeadline::{c.id}",
             "text": {"type": "plain_text",
                      "text": "Edit deadline" if c.deadline else "Set deadline",
                      "emoji": True}},
            # Hold — indefinite pause. Replaces the old "Clear" (deadline)
            # button, which was confusingly named (sounded like delete) and
            # the rare case for deadline removal is handled on the dashboard.
            {"type": "button", "action_id": f"hold::{c.id}",
             "text": {"type": "plain_text", "text": "Hold", "emoji": True}},
        ]
        if c.deadline:
            if not c.escalation_enabled:
                action_elements.append({
                    "type": "button", "action_id": f"resumeesc::{c.id}",
                    "text": {"type": "plain_text", "text": "Resume escalation",
                             "emoji": True},
                })
            elif not at_floor:
                action_elements.append({
                    "type": "button", "action_id": f"stopesc::{c.id}",
                    "text": {"type": "plain_text", "text": "Stop escalation",
                             "emoji": True},
                })
        # Reassign — hand the commitment to someone else.
        action_elements.append({
            "type": "button", "action_id": f"reassign::{c.id}",
            "text": {"type": "plain_text", "text": "Reassign", "emoji": True},
        })
        action_elements.append({
            "type": "button", "action_id": f"done::{c.id}",
            "text": {"type": "plain_text", "text": "Mark done", "emoji": True},
            "style": "primary",
        })
        out.append({"type": "actions", "elements": action_elements})

        if idx < min(len(active), 20) - 1:
            out.append({"type": "divider"})

    if len(active) > 20:
        out.append({"type": "divider"})
        out.append({"type": "context", "elements": [
            {"type": "mrkdwn",
             "text": f"…and *{len(active) - 20}* more. Open the dashboard to see them all."}
        ]})
    return out


def _home_incoming_reassignment_blocks(
    db, owner: User, incoming: list[Reassignment],
) -> list[dict]:
    """Pending reassignments addressed to this user. Top-of-Home priority."""
    if not incoming:
        return []
    out: list[dict] = [
        {"type": "header", "text": {"type": "plain_text",
            "text": "Awaiting your response", "emoji": True}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "Someone wants to hand a commitment off to you."}]},
    ]
    for r in incoming[:10]:
        c = db.get(Commitment, r.commitment_id)
        if c is None:
            continue
        sender = db.get(User, r.from_user_id) if r.from_user_id else None
        from_label = f"<@{sender.slack_user_id}>" if sender else "someone"
        deadline_str = (
            f":calendar: {format_deadline(c.deadline, owner.tz)}"
            if c.deadline else ":calendar: no deadline"
        )
        recipients_str = _home_format_recipients(c)
        meta_bits = [f":bust_in_silhouette: from {from_label}", deadline_str]
        if recipients_str:
            meta_bits.append(f":arrow_right: owed to {recipients_str}")
        meta_bits.append(
            f":hourglass: expires {format_deadline(r.expires_at, owner.tz)}"
        )

        out.append({"type": "section",
                    "text": {"type": "mrkdwn", "text": f"*{c.text[:200]}*"}})
        out.append({"type": "context",
                    "elements": [{"type": "mrkdwn", "text": "   ".join(meta_bits)}]})
        if r.note:
            out.append({"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"_“{r.note[:300]}”_"},
            ]})
        out.append({"type": "actions", "elements": [
            {"type": "button", "action_id": f"acceptra::{r.id}",
             "text": {"type": "plain_text", "text": "Accept", "emoji": True},
             "style": "primary"},
            {"type": "button", "action_id": f"declinera::{r.id}",
             "text": {"type": "plain_text", "text": "Decline", "emoji": True}},
        ]})
        out.append({"type": "divider"})
    return out


def _home_outgoing_reassignment_blocks(
    db, owner: User, outgoing: list[Reassignment],
) -> list[dict]:
    """Pending reassignments this user has sent — show with Cancel."""
    if not outgoing:
        return []
    out: list[dict] = [
        {"type": "header", "text": {"type": "plain_text",
            "text": "Awaiting their response", "emoji": True}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "Commitments you've asked someone else to take over."}]},
    ]
    for r in outgoing[:10]:
        c = db.get(Commitment, r.commitment_id)
        if c is None:
            continue
        deadline_str = (
            f":calendar: {format_deadline(c.deadline, owner.tz)}"
            if c.deadline else ":calendar: no deadline"
        )
        out.append({"type": "section",
                    "text": {"type": "mrkdwn", "text": f"*{c.text[:200]}*"}})
        out.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": (
                f":arrow_forward: sent to <@{r.to_slack_user_id}>   "
                f"{deadline_str}   "
                f":hourglass: expires {format_deadline(r.expires_at, owner.tz)}"
            ),
        }]})
        out.append({"type": "actions", "elements": [
            {"type": "button", "action_id": f"cancelra::{r.id}",
             "text": {"type": "plain_text", "text": "Cancel reassignment",
                      "emoji": True}},
        ]})
        out.append({"type": "divider"})
    return out


def _home_agent_blocks(db, owner: User) -> list[dict]:
    """Render the agentic-capture status strip + recent auto-captures.

    Two layers stacked together at the top of Home so users always have a
    clear answer to "what is the agent doing right now?":

      - A one-line status row: ON/OFF chip, model name, pending buffer
        count, and a Scan-now button.
      - A "Recently auto-captured" sub-section when the agent has logged
        commitments inside the configurable Undo window. Each entry gets
        an inline Undo button that hard-deletes the row (so a false
        positive never lands in the user's failed-commitments stats).
    """
    from app.services import agent as agent_svc
    out: list[dict] = []

    pending = agent_svc.pending_buffer_count(db, owner)
    state_chip = ":robot_face: *Agent: ON*" if owner.agent_enabled else ":zzz: *Agent: off*"
    effective_interval = agent_svc.effective_scan_interval_minutes(owner)
    cadence = f"every {effective_interval}m" if owner.agent_enabled else "paused"
    floor_pct = (
        owner.agent_confidence_floor_pct
        if owner.agent_confidence_floor_pct is not None
        else int(round(settings.agent_confidence_floor * 100))
    )
    status_bits = [
        state_chip, f"model: `{settings.agent_model}`",
        f"scan: {cadence}", f"floor: ≥{floor_pct}%",
        f"buffered: {pending}",
    ]
    if settings.agent_dry_run:
        status_bits.append(":construction: dry-run")
    out.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "   ".join(status_bits)}],
    })

    toggle_label = "Turn agent off" if owner.agent_enabled else "Turn agent on"
    action_elements: list[dict] = [
        {"type": "button", "action_id": "agent_toggle",
         "text": {"type": "plain_text", "text": toggle_label, "emoji": True},
         "style": "primary" if not owner.agent_enabled else None},
    ]
    if owner.agent_enabled:
        action_elements.append({
            "type": "button", "action_id": "agent_scan_now",
            "text": {"type": "plain_text",
                     "text": "Scan recent messages", "emoji": True},
        })
        # Interval picker. Likely-commitment messages already trigger an
        # instant scan via the stub pre-filter, so this only controls how
        # often we backstop-sweep the buffer for things the stub missed.
        interval_options = [
            (1, "Every minute"),
            (5, "Every 5 min"),
            (15, "Every 15 min"),
            (30, "Every 30 min"),
            (60, "Every hour"),
        ]
        opts = [
            {"text": {"type": "plain_text", "text": label, "emoji": True},
             "value": str(m)}
            for m, label in interval_options
        ]
        initial = next(
            (o for o in opts if int(o["value"]) == effective_interval),
            opts[3],  # fall back to 30m if the user's value isn't preset
        )
        action_elements.append({
            "type": "static_select",
            "action_id": "agent_set_interval",
            "placeholder": {"type": "plain_text",
                            "text": "Scan interval", "emoji": True},
            "options": opts,
            "initial_option": initial,
        })
    # Block Kit refuses a button with style=None — drop the key if unset.
    action_elements = [
        {k: v for k, v in e.items() if v is not None} for e in action_elements
    ]
    out.append({"type": "actions", "elements": action_elements})

    if not owner.agent_enabled:
        out.append({"type": "context", "elements": [{
            "type": "mrkdwn",
            "text": (
                "_When on, the agent watches your messages in channels CommitBot "
                "is in, and logs anything that looks like a personal commitment. "
                f"Fresh captures show an Undo button here for "
                f"{settings.agent_undo_window_minutes} minutes._"
            ),
        }]})
        return out

    recent = agent_svc.recent_agent_captures(db, owner=owner)
    if not recent:
        return out

    out.append({"type": "divider"})
    out.append({"type": "header", "text": {
        "type": "plain_text",
        "text": f"Recently auto-captured  ({len(recent)})",
        "emoji": True,
    }})
    out.append({"type": "context", "elements": [{
        "type": "mrkdwn",
        "text": (
            f"_The agent flagged these from your messages. "
            f"Undo within {settings.agent_undo_window_minutes} min if it got one wrong._"
        ),
    }]})

    for c in recent[:10]:
        captured = c.created_at
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        age_min = max(0, int((datetime.now(timezone.utc) - captured).total_seconds() // 60))
        age_str = f"{age_min}m ago" if age_min < 60 else f"{age_min // 60}h ago"
        conf_str = (
            f"   :bar_chart: {int(round((c.agent_confidence or 0) * 100))}% confident"
            if c.agent_confidence is not None else ""
        )
        meta = f":hourglass: captured {age_str}{conf_str}"
        out.append({"type": "section", "text": {
            "type": "mrkdwn", "text": f"*{c.text[:200]}*",
        }})
        if c.agent_rationale:
            out.append({"type": "context", "elements": [{
                "type": "mrkdwn", "text": f"_{c.agent_rationale[:240]}_",
            }]})
        out.append({"type": "context",
                    "elements": [{"type": "mrkdwn", "text": meta}]})
        out.append({"type": "actions", "elements": [
            {"type": "button", "action_id": f"agent_undo::{c.id}",
             "text": {"type": "plain_text", "text": "Undo (delete)", "emoji": True},
             "style": "danger",
             "confirm": {
                 "title": {"type": "plain_text", "text": "Delete this auto-capture?"},
                 "text": {"type": "mrkdwn", "text": (
                     "This permanently removes the agent's capture. It won't "
                     "appear in your failed-commitments history."
                 )},
                 "confirm": {"type": "plain_text", "text": "Delete"},
                 "deny": {"type": "plain_text", "text": "Keep"},
             }},
            {"type": "button", "action_id": f"setdeadline::{c.id}",
             "text": {"type": "plain_text", "text": "Set deadline", "emoji": True}},
            {"type": "button", "action_id": f"done::{c.id}",
             "text": {"type": "plain_text", "text": "Mark done", "emoji": True},
             "style": "primary"},
        ]})
        out.append({"type": "divider"})

    if len(recent) > 10:
        out.append({"type": "context", "elements": [{
            "type": "mrkdwn",
            "text": f"…and {len(recent) - 10} more recent auto-captures.",
        }]})
    return out


def _build_home_view(db, owner: User) -> dict:
    """Build the App Home Block Kit view for `owner`.

    Sections, in order:
      1. Header + "open dashboard" context
      2. Agent status strip + Recently auto-captured (if any)
      3. Incoming reassignment requests (Accept/Decline)
      4. Outgoing pending reassignments (Cancel)
      5. Active commitments list
    """
    # The Home tab is the user's "what should I do?" view — show both ACTIVE
    # (their own work) and REASSIGNED (work handed to them by teammates).
    # Both are live, pingable states from the model's perspective.
    active = db.execute(
        select(Commitment).where(
            Commitment.user_id == owner.id,
            Commitment.state.in_(
                [CommitmentState.ACTIVE, CommitmentState.REASSIGNED]
            ),
        ).order_by(Commitment.deadline.is_(None), Commitment.deadline.asc())
    ).scalars().all()

    incoming = reassign_svc.list_incoming_pending(
        db, workspace_id=owner.workspace_id, slack_user_id=owner.slack_user_id,
    )
    outgoing = reassign_svc.list_outgoing_pending(db, owner_id=owner.id)

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text",
            "text": "Your commitments", "emoji": True}},
        {"type": "context", "elements": [
            {"type": "mrkdwn",
             "text": "Type `/commit` anywhere to log one  ·  "
                     f"<{settings.app_base_url}/|Open dashboard> (sign in with Slack)"}
        ]},
        {"type": "divider"},
    ]

    blocks.extend(_home_agent_blocks(db, owner))
    blocks.append({"type": "divider"})

    blocks.extend(_home_incoming_reassignment_blocks(db, owner, incoming))
    blocks.extend(_home_outgoing_reassignment_blocks(db, owner, outgoing))

    if not active and not incoming and not outgoing:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": ":sparkles: Nothing tracked yet. Type `/commit something you'll do` anywhere in Slack."}})
        return {"type": "home", "blocks": blocks}

    if active:
        if incoming or outgoing:
            # Visual separator between reassignment sections and the main list.
            blocks.append({"type": "header",
                           "text": {"type": "plain_text",
                                    "text": "Your active commitments",
                                    "emoji": True}})
        blocks.extend(_home_active_blocks(db, owner, active))

    # "Clear bot DMs" — destructive utility at the very bottom. Slack
    # doesn't let third-party apps put UI into the Messages tab, so the
    # cleanest place for a bulk-delete affordance is the bottom of the
    # Home tab. Style danger so it's visually distinct.
    blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": "_Cleaning up old pings and notifications from your DMs._"}]})
    blocks.append({"type": "actions", "elements": [{
        "type": "button",
        "action_id": "clearmsgs",
        "text": {"type": "plain_text", "text": "Clear all CommitBot DMs",
                 "emoji": True},
        "style": "danger",
    }]})
    return {"type": "home", "blocks": blocks}


def _build_signin_home_view() -> dict:
    """Welcome screen for users who haven't completed Sign in with Slack yet."""
    return {
        "type": "home",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text",
                "text": "Welcome to CommitBot", "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn",
                "text": (
                    ":wave: Before you can log commitments, you'll need to "
                    "sign in once. This links Slack captures to *your* "
                    "dashboard so what you `/commit` actually shows up here."
                )}},
            {"type": "actions", "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "Sign in with Slack",
                         "emoji": True},
                "style": "primary",
                "url": f"{settings.app_base_url}/auth/slack/login",
            }]},
            {"type": "context", "elements": [{
                "type": "mrkdwn",
                "text": "Only your name, email, and Slack user id are read.",
            }]},
        ],
    }


def _refresh_home(client, *, team_id: str, slack_user_id: str) -> None:
    """Reload the home view from DB and publish it for `slack_user_id`.

    Un-onboarded users get a sign-in CTA instead of a commitments list — we
    don't auto-provision User rows here for the same reason the capture paths
    don't: rows you can't reach are worse than no row.
    """
    with session_scope() as db:
        owner = _find_user(db, slack_team_id=team_id, slack_user_id=slack_user_id)
        view = _build_home_view(db, owner) if _is_onboarded(owner) else _build_signin_home_view()
    try:
        client.views_publish(user_id=slack_user_id, view=view)
    except Exception:
        log.exception("views_publish failed for user=%s", slack_user_id)


@bolt_app.event("app_home_opened")
def render_app_home(event, client):
    team_id = event.get("view", {}).get("team_id") or event.get("team")
    slack_user_id = event.get("user")
    if not (team_id and slack_user_id):
        # Without a team_id we'd create a Workspace row with NULL slack_team_id,
        # which then conflicts with itself on the unique index.
        log.warning("app_home_opened missing team/user: team=%r user=%r", team_id, slack_user_id)
        return
    _refresh_home(client, team_id=team_id, slack_user_id=slack_user_id)


# ---------------------------------------------------------------------------
# Inline ping actions
# ---------------------------------------------------------------------------

def _update_message_if_possible(client, body: dict, *, new_text: str) -> None:
    """If the action came from a message (DM), rewrite that message to retire the buttons."""
    channel = (body.get("channel") or {}).get("id")
    message = body.get("message") or {}
    ts = message.get("ts")
    if not (channel and ts):
        return
    try:
        client.chat_update(channel=channel, ts=ts, text=new_text,
                           blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": new_text}}])
    except Exception:
        log.warning("chat_update failed for channel=%s ts=%s", channel, ts)


def _is_commitment_owner(c: Commitment, body: dict) -> bool:
    """True iff the user who triggered this action owns the commitment.

    Slack `user_id`s are workspace-scoped, so we match on both team and user.
    """
    clicker = (body.get("user") or {}).get("id")
    team = (body.get("team") or {}).get("id") or body.get("team_id")
    if not clicker:
        return False
    if c.user is None:
        return False
    if c.user.slack_user_id != clicker:
        return False
    # Defensive: prevent a cross-workspace ID collision from authorising someone
    # in a different install.
    if team and c.user.workspace and c.user.workspace.slack_team_id != team:
        return False
    return True


def _deny_non_owner(client, body: dict, *, owner_slack_user_id: Optional[str]) -> None:
    """Send an ephemeral notice in the channel where the action was taken."""
    clicker = (body.get("user") or {}).get("id")
    channel = ((body.get("channel") or {}).get("id")
               or (body.get("container") or {}).get("channel_id"))
    if not (clicker and channel):
        return
    owner_mention = f"<@{owner_slack_user_id}>" if owner_slack_user_id else "the owner"
    _safe_ephemeral(
        client, channel=channel, user=clicker,
        text=f":lock: Only {owner_mention} can act on this commitment.",
    )


@bolt_app.action("agent_toggle")
def handle_agent_toggle(ack, body, client):
    """Flip the user's `agent_enabled` flag and re-render Home."""
    ack()
    team_id = (body.get("team") or {}).get("id") or body.get("team_id")
    slack_user_id = (body.get("user") or {}).get("id")
    if not (team_id and slack_user_id):
        return
    with session_scope() as db:
        owner = _find_user(db, slack_team_id=team_id, slack_user_id=slack_user_id)
        if not _is_onboarded(owner):
            return
        owner.agent_enabled = not owner.agent_enabled
        new_state = owner.agent_enabled
    log.info(
        "agent_toggle: user=%s now %s",
        slack_user_id, "ON" if new_state else "off",
    )
    _refresh_home(client, team_id=team_id, slack_user_id=slack_user_id)


@bolt_app.action("agent_set_interval")
def handle_agent_set_interval(ack, body, client):
    """Persist the user's selected scan interval and re-render Home."""
    ack()
    team_id = (body.get("team") or {}).get("id") or body.get("team_id")
    slack_user_id = (body.get("user") or {}).get("id")
    if not (team_id and slack_user_id):
        return
    # static_select returns {actions: [{selected_option: {value: "..."}}]}.
    selected: Optional[str] = None
    for a in body.get("actions") or []:
        if a.get("action_id") == "agent_set_interval":
            selected = ((a.get("selected_option") or {}).get("value"))
            break
    try:
        minutes = max(1, min(1440, int(selected) if selected else 30))
    except (TypeError, ValueError):
        return
    with session_scope() as db:
        owner = _find_user(db, slack_team_id=team_id, slack_user_id=slack_user_id)
        if not _is_onboarded(owner):
            return
        owner.agent_scan_interval_minutes = minutes
    log.info("agent_set_interval: user=%s minutes=%d", slack_user_id, minutes)
    _refresh_home(client, team_id=team_id, slack_user_id=slack_user_id)


@bolt_app.action("agent_scan_now")
def handle_agent_scan_now(ack, body, client):
    """Trigger a synchronous scan of the user's buffered messages.

    Runs in a background thread so the Bolt request returns inside Slack's
    3-second window. The Home tab refreshes when the scan finishes.
    """
    ack()
    team_id = (body.get("team") or {}).get("id") or body.get("team_id")
    slack_user_id = (body.get("user") or {}).get("id")
    if not (team_id and slack_user_id):
        return

    channel = ((body.get("channel") or {}).get("id")
               or (body.get("container") or {}).get("channel_id"))

    def _do_scan():
        from app.services import agent as agent_svc
        from app.services.llm import get_provider

        try:
            provider = get_provider()
            with session_scope() as db:
                owner = _find_user(db, slack_team_id=team_id, slack_user_id=slack_user_id)
                if not (_is_onboarded(owner) and owner.agent_enabled):
                    return
                created = agent_svc.scan_user(db, owner, provider=provider)
                count = len(created)
        except Exception:
            log.exception("agent_scan_now failed for user=%s", slack_user_id)
            count = 0

        # Ephemeral receipt where the user clicked (Home is in DMs context
        # already, so the channel id often refers to the user themselves —
        # postEphemeral falls back to DM via `_safe_ephemeral`).
        if channel:
            text = (
                f":robot_face: Agent scan complete. "
                f"{count} new commitment{'s' if count != 1 else ''} captured."
            )
            try:
                _safe_ephemeral(client, channel=channel, user=slack_user_id, text=text)
            except Exception:
                log.debug("agent scan receipt suppressed", exc_info=True)
        _refresh_home(client, team_id=team_id, slack_user_id=slack_user_id)

    threading.Thread(target=_do_scan, daemon=True).start()


@bolt_app.action(re.compile(r"^agent_undo::"))
def handle_agent_undo(ack, action, body, client):
    """Hard-delete an agent-captured commitment within its Undo window."""
    ack()
    commitment_id = action["action_id"].split("::", 1)[1]
    with session_scope() as db:
        c = db.get(Commitment, commitment_id)
        if c is None:
            return
        if not _is_commitment_owner(c, body):
            owner_id = c.user.slack_user_id if c.user else None
            _deny_non_owner(client, body, owner_slack_user_id=owner_id)
            return
        from app.services import agent as agent_svc
        team_id = c.user.workspace.slack_team_id
        slack_user_id = c.user.slack_user_id
        commit_text = c.text
        removed = agent_svc.undo_agent_capture(db, c)

    if not removed:
        # Past the Undo window OR not actually an agent capture. Tell the
        # user and bow out — leave the commitment alone.
        channel = ((body.get("channel") or {}).get("id")
                   or (body.get("container") or {}).get("channel_id"))
        if channel:
            _safe_ephemeral(
                client, channel=channel, user=slack_user_id,
                text=(":warning: Undo window has passed. "
                      "Soft-delete it from the dashboard instead."),
            )
        return

    _update_message_if_possible(
        client, body, new_text=f":wastebasket: *Undone:* {commit_text}",
    )
    _refresh_home(client, team_id=team_id, slack_user_id=slack_user_id)


@bolt_app.action(re.compile(r"^done::"))
def handle_done_action(ack, action, body, client):
    ack()
    commitment_id = action["action_id"].split("::", 1)[1]
    with session_scope() as db:
        c = db.get(Commitment, commitment_id)
        if c is None:
            return
        if not _is_commitment_owner(c, body):
            owner_id = c.user.slack_user_id if c.user else None
            _deny_non_owner(client, body, owner_slack_user_id=owner_id)
            return
        commit_svc.mark_done(db, c, source=EditSource.SLACK)
        team_id = c.user.workspace.slack_team_id
        slack_user_id = c.user.slack_user_id
        commit_text = c.text

    _update_message_if_possible(client, body, new_text=f":white_check_mark: *Done:* {commit_text}")
    _refresh_home(client, team_id=team_id, slack_user_id=slack_user_id)


@bolt_app.action(re.compile(r"^snooze2h::"))
def handle_snooze_2h(ack, action, body, client):
    ack()
    commitment_id = action["action_id"].split("::", 1)[1]
    with session_scope() as db:
        c = db.get(Commitment, commitment_id)
        if c is None:
            return
        if not _is_commitment_owner(c, body):
            owner_id = c.user.slack_user_id if c.user else None
            _deny_non_owner(client, body, owner_slack_user_id=owner_id)
            return
        commit_svc.put_on_hold(
            db, c,
            resume_at=datetime.now(timezone.utc) + timedelta(hours=2),
            source=EditSource.SLACK,
        )
        team_id = c.user.workspace.slack_team_id
        slack_user_id = c.user.slack_user_id
        commit_text = c.text

    _update_message_if_possible(client, body, new_text=f":zzz: *Snoozed 2h:* {commit_text}")
    _refresh_home(client, team_id=team_id, slack_user_id=slack_user_id)


@bolt_app.action(re.compile(r"^snoozetomorrow::"))
def handle_snooze_tomorrow(ack, action, body, client):
    ack()
    commitment_id = action["action_id"].split("::", 1)[1]
    with session_scope() as db:
        c = db.get(Commitment, commitment_id)
        if c is None:
            return
        if not _is_commitment_owner(c, body):
            owner_id = c.user.slack_user_id if c.user else None
            _deny_non_owner(client, body, owner_slack_user_id=owner_id)
            return
        # Tomorrow at the user's start_of_day, interpreted in their timezone.
        zone = safe_zone(c.user.tz)
        tomorrow_local = (datetime.now(zone) + timedelta(days=1)).date()
        sod = c.user.start_of_day
        resume_at = datetime.combine(tomorrow_local, sod, tzinfo=zone).astimezone(timezone.utc)
        commit_svc.put_on_hold(db, c, resume_at=resume_at, source=EditSource.SLACK)
        team_id = c.user.workspace.slack_team_id
        slack_user_id = c.user.slack_user_id
        commit_text = c.text

    _update_message_if_possible(client, body, new_text=f":calendar: *Snoozed to tomorrow:* {commit_text}")
    _refresh_home(client, team_id=team_id, slack_user_id=slack_user_id)


@bolt_app.action(re.compile(r"^hold::"))
def handle_hold(ack, action, body, client):
    """Put a commitment on hold indefinitely. Unlike Snooze 2h / Tomorrow,
    no `on_hold_resume_at` is set — the user must manually resume from the
    dashboard, or the deadline-driven auto-resume sweep picks it up if a
    deadline is approaching."""
    ack()
    commitment_id = action["action_id"].split("::", 1)[1]
    with session_scope() as db:
        c = db.get(Commitment, commitment_id)
        if c is None:
            return
        if not _is_commitment_owner(c, body):
            owner_id = c.user.slack_user_id if c.user else None
            _deny_non_owner(client, body, owner_slack_user_id=owner_id)
            return
        commit_svc.put_on_hold(db, c, resume_at=None, source=EditSource.SLACK)
        team_id = c.user.workspace.slack_team_id
        slack_user_id = c.user.slack_user_id
        commit_text = c.text

    _update_message_if_possible(
        client, body,
        new_text=f":pause_button: *On hold:* {commit_text}",
    )
    _refresh_home(client, team_id=team_id, slack_user_id=slack_user_id)


# --- Deadline editing via App Home ----------------------------------------

def _build_deadline_modal(
    commitment_id: str,
    commit_text: str,
    current_deadline: Optional[datetime],
    *,
    user_tz: str,
) -> dict:
    """Modal with a date + time picker, pre-populated with the existing deadline.

    Both the initial values and the parsed submission are in the user's
    configured timezone. We stash the tz in private_metadata so the
    submission handler doesn't have to look it up again.
    """
    date_element: dict = {
        "type": "datepicker",
        "action_id": "deadline_date",
        "placeholder": {"type": "plain_text", "text": "Pick a date"},
    }
    time_element: dict = {
        "type": "timepicker",
        "action_id": "deadline_time",
        "placeholder": {"type": "plain_text", "text": "Pick a time"},
    }
    if current_deadline is not None:
        local = to_local(current_deadline, user_tz)
        date_element["initial_date"] = local.strftime("%Y-%m-%d")
        time_element["initial_time"] = local.strftime("%H:%M")

    zone_label = str(safe_zone(user_tz))
    return {
        "type": "modal",
        "callback_id": "set_deadline_modal",
        "private_metadata": f"{commitment_id}|{zone_label}",
        "title": {"type": "plain_text", "text": "Set deadline"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn",
                "text": f"*{commit_text[:120]}*"}},
            {"type": "input", "block_id": "date_block",
             "label": {"type": "plain_text", "text": "Date"},
             "element": date_element},
            {"type": "input", "block_id": "time_block",
             "label": {"type": "plain_text", "text": f"Time ({zone_label})"},
             "element": time_element},
            {"type": "context", "elements": [
                {"type": "mrkdwn",
                 "text": f"_Times are interpreted in *{zone_label}* — change your timezone in Settings._"},
            ]},
        ],
    }


@bolt_app.action(re.compile(r"^setdeadline::"))
def handle_set_deadline_action(ack, action, body, client):
    ack()
    commitment_id = action["action_id"].split("::", 1)[1]
    trigger_id = body.get("trigger_id")
    if not trigger_id:
        log.warning("setdeadline action without trigger_id (body=%s)", body.get("type"))
        return

    with session_scope() as db:
        c = db.get(Commitment, commitment_id)
        if c is None:
            return
        if not _is_commitment_owner(c, body):
            owner_id = c.user.slack_user_id if c.user else None
            _deny_non_owner(client, body, owner_slack_user_id=owner_id)
            return
        current_deadline = c.deadline
        commit_text = c.text
        user_tz = c.user.tz if c.user else "UTC"

    modal = _build_deadline_modal(
        commitment_id, commit_text, current_deadline, user_tz=user_tz,
    )
    try:
        client.views_open(trigger_id=trigger_id, view=modal)
    except Exception:
        log.exception("views_open failed for commitment %s", commitment_id)


@bolt_app.view("set_deadline_modal")
def handle_set_deadline_submit(ack, view, body, client):
    state = view.get("state", {}).get("values", {})
    meta = (view.get("private_metadata") or "").split("|", 1)
    commitment_id = meta[0] if meta else None
    modal_tz = meta[1] if len(meta) > 1 else "UTC"
    date_str = state.get("date_block", {}).get("deadline_date", {}).get("selected_date")
    time_str = state.get("time_block", {}).get("deadline_time", {}).get("selected_time")

    errors: dict[str, str] = {}
    if not date_str:
        errors["date_block"] = "Pick a date."
    if not time_str:
        errors["time_block"] = "Pick a time."
    if errors:
        ack(response_action="errors", errors=errors)
        return

    try:
        local_dt = datetime.fromisoformat(f"{date_str}T{time_str}:00")
        deadline = local_dt.replace(tzinfo=safe_zone(modal_tz)).astimezone(timezone.utc)
    except ValueError as e:
        ack(response_action="errors", errors={"date_block": f"Couldn't parse date/time: {e}"})
        return

    with session_scope() as db:
        c = db.get(Commitment, commitment_id) if commitment_id else None
        if c is None:
            ack()
            return
        if not _is_commitment_owner(c, body):
            ack(response_action="errors",
                errors={"date_block": "You can't edit someone else's commitment."})
            return
        ack()  # close the modal
        try:
            commit_svc.set_deadline(db, c, deadline, source=EditSource.SLACK)
        except ValueError as e:
            log.warning("set_deadline rejected for %s: %s", commitment_id, e)
            return
        level = db.get(PriorityLevel, c.priority_level_id) if c.priority_level_id else None
        ping_svc.reschedule_next_ping(db, c, level)
        team_id = c.user.workspace.slack_team_id
        slack_user_id = c.user.slack_user_id

    _refresh_home(client, team_id=team_id, slack_user_id=slack_user_id)


@bolt_app.action(re.compile(r"^cleardeadline::"))
def handle_clear_deadline_action(ack, action, body, client):
    ack()
    commitment_id = action["action_id"].split("::", 1)[1]
    with session_scope() as db:
        c = db.get(Commitment, commitment_id)
        if c is None:
            return
        if not _is_commitment_owner(c, body):
            owner_id = c.user.slack_user_id if c.user else None
            _deny_non_owner(client, body, owner_slack_user_id=owner_id)
            return
        try:
            commit_svc.set_deadline(db, c, None, source=EditSource.SLACK)
        except ValueError as e:
            log.warning("clear deadline rejected for %s: %s", commitment_id, e)
            return
        level = db.get(PriorityLevel, c.priority_level_id) if c.priority_level_id else None
        ping_svc.reschedule_next_ping(db, c, level)
        team_id = c.user.workspace.slack_team_id
        slack_user_id = c.user.slack_user_id

    _refresh_home(client, team_id=team_id, slack_user_id=slack_user_id)


@bolt_app.action(re.compile(r"^stopesc::"))
def handle_stop_escalation(ack, action, body, client):
    ack()
    commitment_id = action["action_id"].split("::", 1)[1]
    with session_scope() as db:
        c = db.get(Commitment, commitment_id)
        if c is None:
            return
        if not _is_commitment_owner(c, body):
            owner_id = c.user.slack_user_id if c.user else None
            _deny_non_owner(client, body, owner_slack_user_id=owner_id)
            return
        # B2: route through the service layer so the edit log captures this.
        commit_svc.set_escalation_enabled(db, c, False, source=EditSource.SLACK)
        level = db.get(PriorityLevel, c.priority_level_id) if c.priority_level_id else None
        # Cadence changed (now at base instead of accelerating) — re-arm so the
        # already-queued ping doesn't keep ticking at the escalated rate.
        ping_svc.reschedule_next_ping(db, c, level)
        team_id = c.user.workspace.slack_team_id
        slack_user_id = c.user.slack_user_id
        commit_text = c.text

    _update_message_if_possible(client, body, new_text=f":no_bell: *Escalation stopped:* {commit_text}")
    _refresh_home(client, team_id=team_id, slack_user_id=slack_user_id)


@bolt_app.action(re.compile(r"^resumeesc::"))
def handle_resume_escalation(ack, action, body, client):
    ack()
    commitment_id = action["action_id"].split("::", 1)[1]
    with session_scope() as db:
        c = db.get(Commitment, commitment_id)
        if c is None:
            return
        if not _is_commitment_owner(c, body):
            owner_id = c.user.slack_user_id if c.user else None
            _deny_non_owner(client, body, owner_slack_user_id=owner_id)
            return
        commit_svc.set_escalation_enabled(db, c, True, source=EditSource.SLACK)
        level = db.get(PriorityLevel, c.priority_level_id) if c.priority_level_id else None
        # Rearm so the next ping reflects the (possibly faster) escalating cadence.
        ping_svc.reschedule_next_ping(db, c, level)
        team_id = c.user.workspace.slack_team_id
        slack_user_id = c.user.slack_user_id
        commit_text = c.text

    _update_message_if_possible(client, body, new_text=f":bell: *Escalation resumed:* {commit_text}")
    _refresh_home(client, team_id=team_id, slack_user_id=slack_user_id)


# ---------------------------------------------------------------------------
# Reassignment (owner hands a commitment off; recipient accepts/declines)
# ---------------------------------------------------------------------------

def _build_reassign_modal(commitment_id: str, commit_text: str) -> dict:
    """Modal: a Slack `users_select` for the new owner + an optional note."""
    return {
        "type": "modal",
        "callback_id": "reassign_modal",
        "private_metadata": commitment_id,
        "title": {"type": "plain_text", "text": "Reassign"},
        "submit": {"type": "plain_text", "text": "Send request"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn",
                "text": f"*{commit_text[:140]}*"}},
            {"type": "input", "block_id": "target_block",
             "label": {"type": "plain_text", "text": "Hand off to"},
             "element": {
                "type": "users_select",
                "action_id": "target_user",
                "placeholder": {"type": "plain_text", "text": "Pick a teammate"},
             }},
            {"type": "input", "block_id": "note_block", "optional": True,
             "label": {"type": "plain_text", "text": "Note (optional)"},
             "element": {
                "type": "plain_text_input",
                "action_id": "note",
                "multiline": True,
                "max_length": 500,
                "placeholder": {"type": "plain_text",
                                "text": "Why are you handing this off?"},
             }},
            {"type": "context", "elements": [{"type": "mrkdwn",
                "text": ("They have *24 hours* to accept. If they don't respond, "
                         "the commitment comes back to you.")}]},
        ],
    }


@bolt_app.action(re.compile(r"^reassign::"))
def handle_reassign_action(ack, action, body, client):
    ack()
    commitment_id = action["action_id"].split("::", 1)[1]
    trigger_id = body.get("trigger_id")
    if not trigger_id:
        return

    with session_scope() as db:
        c = db.get(Commitment, commitment_id)
        if c is None:
            return
        if not _is_commitment_owner(c, body):
            owner_id = c.user.slack_user_id if c.user else None
            _deny_non_owner(client, body, owner_slack_user_id=owner_id)
            return
        if c.state not in (CommitmentState.ACTIVE, CommitmentState.REASSIGNED):
            _safe_ephemeral(
                client,
                channel=(body.get("channel") or {}).get("id")
                        or (body.get("container") or {}).get("channel_id")
                        or c.user.slack_user_id,
                user=body["user"]["id"],
                text=":warning: Only active commitments can be reassigned.",
            )
            return
        commit_text = c.text

    modal = _build_reassign_modal(commitment_id, commit_text)
    try:
        client.views_open(trigger_id=trigger_id, view=modal)
    except Exception:
        log.exception("views_open failed for reassign of %s", commitment_id)


@bolt_app.view("reassign_modal")
def handle_reassign_submit(ack, view, body, client):
    state = view.get("state", {}).get("values", {})
    commitment_id = view.get("private_metadata") or ""
    target_user = (
        state.get("target_block", {})
             .get("target_user", {})
             .get("selected_user")
    )
    note = (
        state.get("note_block", {})
             .get("note", {})
             .get("value")
    )

    errors: dict[str, str] = {}
    if not target_user:
        errors["target_block"] = "Pick someone to hand this off to."
    if errors:
        ack(response_action="errors", errors=errors)
        return

    requester_slack_id = body.get("user", {}).get("id")
    notice_channel_id: Optional[str] = None
    notice_message_ts: Optional[str] = None
    reassignment_id: Optional[str] = None
    error_msg: Optional[str] = None
    commit_text: Optional[str] = None
    deadline = None
    owner_tz = "UTC"
    owner_team_id: Optional[str] = None
    requester_label: Optional[str] = None

    with session_scope() as db:
        c = db.get(Commitment, commitment_id)
        if c is None:
            ack(response_action="errors",
                errors={"target_block": "Commitment no longer exists."})
            return
        if c.user is None or c.user.slack_user_id != requester_slack_id:
            ack(response_action="errors",
                errors={"target_block": "Only the owner can reassign this."})
            return

        try:
            r = reassign_svc.request_reassignment(
                db,
                commitment=c,
                target_slack_user_id=target_user,
                source=EditSource.SLACK,
                note=note,
            )
        except ValueError as e:
            ack(response_action="errors", errors={"target_block": str(e)})
            return

        reassignment_id = r.id
        commit_text = c.text
        deadline = c.deadline
        owner_tz = c.user.tz
        owner_team_id = c.user.workspace.slack_team_id
        requester_label = f"<@{c.user.slack_user_id}>"
        recipients_for_dm = _home_format_recipients(c)
        expires_at = r.expires_at

    ack()  # close the modal — DM + refresh happen below

    # DM the target with the request + Accept/Decline. Capture the message_ts
    # so we can chat.update it later (retire buttons on outcome).
    try:
        resp = client.chat_postMessage(
            channel=target_user,
            text=(
                f":incoming_envelope: {requester_label} wants to hand off "
                f"a commitment: {commit_text}"
            ),
            blocks=_reassignment_request_blocks(
                reassignment_id=reassignment_id,
                requester_label=requester_label,
                commit_text=commit_text,
                deadline_local=format_deadline(deadline, owner_tz),
                recipients_str=recipients_for_dm,
                expires_local=format_deadline(expires_at, owner_tz),
                note=note,
            ),
        )
        notice_channel_id = resp.get("channel")
        notice_message_ts = resp.get("ts")
    except Exception:
        log.exception("Failed to DM reassignment to %s", target_user)

    # Persist the DM coordinates so the outcome handler can chat.update it.
    if notice_message_ts:
        with session_scope() as db:
            r = db.get(Reassignment, reassignment_id)
            if r is not None:
                r.notice_channel_id = notice_channel_id
                r.notice_message_ts = notice_message_ts

    # Refresh both parties' Home tabs.
    if owner_team_id:
        try:
            _refresh_home(client, team_id=owner_team_id, slack_user_id=requester_slack_id)
        except Exception:
            log.exception("home refresh failed for requester")
        try:
            _refresh_home(client, team_id=owner_team_id, slack_user_id=target_user)
        except Exception:
            log.exception("home refresh failed for target")


def _reassignment_request_blocks(
    *, reassignment_id: str, requester_label: str, commit_text: str,
    deadline_local: str, recipients_str: str, expires_local: str,
    note: Optional[str],
) -> list[dict]:
    """The Block Kit body of the DM we send the target."""
    meta = [
        f":bust_in_silhouette: from {requester_label}",
        f":calendar: {deadline_local}",
    ]
    if recipients_str:
        meta.append(f":arrow_right: owed to {recipients_str}")
    meta.append(f":hourglass: expires {expires_local}")

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": (
                f":incoming_envelope: *{requester_label}* wants to hand off "
                f"a commitment to you."
            )}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*{commit_text[:300]}*"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "   ".join(meta)}]},
    ]
    if note:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"_“{note[:300]}”_"},
        ]})
    blocks.append({"type": "actions", "elements": [
        {"type": "button", "action_id": f"acceptra::{reassignment_id}",
         "text": {"type": "plain_text", "text": "Accept", "emoji": True},
         "style": "primary"},
        {"type": "button", "action_id": f"declinera::{reassignment_id}",
         "text": {"type": "plain_text", "text": "Decline", "emoji": True}},
    ]})
    return blocks


# --- Recipient actions (accept / decline) ---------------------------------

def _check_actor_is_target(
    db, body: dict, reassignment: Reassignment,
) -> Optional[User]:
    """Return the actor's User row iff they're the named target. Else None."""
    actor_slack_id = (body.get("user") or {}).get("id")
    actor_team_id = (body.get("team") or {}).get("id") or body.get("team_id")
    if not actor_slack_id or actor_slack_id != reassignment.to_slack_user_id:
        return None
    return _find_user(db, slack_team_id=actor_team_id, slack_user_id=actor_slack_id)


@bolt_app.action(re.compile(r"^acceptra::"))
def handle_accept_reassignment(ack, action, body, client):
    ack()
    rid = action["action_id"].split("::", 1)[1]
    payload: dict[str, Any] = {}
    with session_scope() as db:
        r = db.get(Reassignment, rid)
        if r is None or r.status != ReassignmentStatus.PENDING:
            _update_message_if_possible(
                client, body,
                new_text=":hourglass: This request is no longer pending.",
            )
            return
        actor = _check_actor_is_target(db, body, r)
        if actor is None:
            _safe_ephemeral(
                client,
                channel=(body.get("channel") or {}).get("id") or r.to_slack_user_id,
                user=(body.get("user") or {}).get("id") or "",
                text=":lock: This request isn't addressed to you.",
            )
            return
        try:
            reassign_svc.accept_reassignment(
                db, reassignment=r, actor=actor, source=EditSource.SLACK,
            )
        except ValueError as e:
            _safe_ephemeral(
                client,
                channel=(body.get("channel") or {}).get("id") or actor.slack_user_id,
                user=actor.slack_user_id, text=f":warning: {e}",
            )
            return
        c = db.get(Commitment, r.commitment_id)
        owner = db.get(User, r.from_user_id) if r.from_user_id else None
        payload = {
            "team_id": actor.workspace.slack_team_id,
            "accepter": actor.slack_user_id,
            "owner_slack": owner.slack_user_id if owner else None,
            "commit_text": c.text if c else "(deleted)",
        }

    _update_message_if_possible(
        client, body,
        new_text=f":white_check_mark: *Accepted:* {payload['commit_text']}",
    )
    # Tell the original owner.
    try:
        if payload["owner_slack"]:
            client.chat_postMessage(
                channel=payload["owner_slack"],
                text=(
                    f":white_check_mark: <@{payload['accepter']}> accepted your "
                    f"reassignment of *{payload['commit_text']}*. "
                    "It's now their commitment."
                ),
            )
    except Exception:
        log.exception("Owner notification (accept) failed")
    # Refresh both Home tabs.
    for who in (payload["accepter"], payload["owner_slack"]):
        if who and payload["team_id"]:
            try:
                _refresh_home(client, team_id=payload["team_id"], slack_user_id=who)
            except Exception:
                log.exception("home refresh failed after accept for %s", who)


@bolt_app.action(re.compile(r"^declinera::"))
def handle_decline_reassignment(ack, action, body, client):
    ack()
    rid = action["action_id"].split("::", 1)[1]
    payload: dict[str, Any] = {}
    with session_scope() as db:
        r = db.get(Reassignment, rid)
        if r is None or r.status != ReassignmentStatus.PENDING:
            _update_message_if_possible(
                client, body,
                new_text=":hourglass: This request is no longer pending.",
            )
            return
        actor = _check_actor_is_target(db, body, r)
        if actor is None:
            _safe_ephemeral(
                client,
                channel=(body.get("channel") or {}).get("id") or r.to_slack_user_id,
                user=(body.get("user") or {}).get("id") or "",
                text=":lock: This request isn't addressed to you.",
            )
            return
        try:
            reassign_svc.decline_reassignment(
                db, reassignment=r, actor=actor, source=EditSource.SLACK,
            )
        except ValueError as e:
            _safe_ephemeral(
                client,
                channel=(body.get("channel") or {}).get("id") or actor.slack_user_id,
                user=actor.slack_user_id, text=f":warning: {e}",
            )
            return
        c = db.get(Commitment, r.commitment_id)
        owner = db.get(User, r.from_user_id) if r.from_user_id else None
        payload = {
            "team_id": actor.workspace.slack_team_id,
            "decliner": actor.slack_user_id,
            "owner_slack": owner.slack_user_id if owner else None,
            "commit_text": c.text if c else "(deleted)",
        }

    _update_message_if_possible(
        client, body,
        new_text=f":no_entry_sign: *Declined:* {payload['commit_text']}",
    )
    try:
        if payload["owner_slack"]:
            client.chat_postMessage(
                channel=payload["owner_slack"],
                text=(
                    f":no_entry_sign: <@{payload['decliner']}> declined your "
                    f"reassignment of *{payload['commit_text']}*. "
                    "It's back on your plate."
                ),
            )
    except Exception:
        log.exception("Owner notification (decline) failed")
    for who in (payload["decliner"], payload["owner_slack"]):
        if who and payload["team_id"]:
            try:
                _refresh_home(client, team_id=payload["team_id"], slack_user_id=who)
            except Exception:
                log.exception("home refresh failed after decline for %s", who)


# --- Owner cancel ----------------------------------------------------------

@bolt_app.action(re.compile(r"^cancelra::"))
def handle_cancel_reassignment(ack, action, body, client):
    ack()
    rid = action["action_id"].split("::", 1)[1]
    payload: dict[str, Any] = {}
    with session_scope() as db:
        r = db.get(Reassignment, rid)
        if r is None or r.status != ReassignmentStatus.PENDING:
            return
        c = db.get(Commitment, r.commitment_id)
        if c is None or c.user is None:
            return
        actor_slack_id = (body.get("user") or {}).get("id")
        if c.user.slack_user_id != actor_slack_id:
            _deny_non_owner(
                client, body,
                owner_slack_user_id=c.user.slack_user_id,
            )
            return
        try:
            reassign_svc.cancel_reassignment(
                db, reassignment=r, actor=c.user, source=EditSource.SLACK,
            )
        except ValueError as e:
            _safe_ephemeral(
                client,
                channel=(body.get("channel") or {}).get("id") or c.user.slack_user_id,
                user=c.user.slack_user_id, text=f":warning: {e}",
            )
            return
        payload = {
            "team_id": c.user.workspace.slack_team_id,
            "owner_slack": c.user.slack_user_id,
            "target_slack": r.to_slack_user_id,
            "commit_text": c.text,
            "notice_channel": r.notice_channel_id,
            "notice_ts": r.notice_message_ts,
        }

    # Retire the recipient's DM buttons by rewriting that message.
    if payload.get("notice_channel") and payload.get("notice_ts"):
        try:
            retire_reassignment_dm(
                client,
                channel=payload["notice_channel"],
                ts=payload["notice_ts"],
                final_text=(
                    f":x: This reassignment of *{payload['commit_text']}* "
                    "was cancelled by the sender."
                ),
            )
        except Exception:
            log.exception("retire_reassignment_dm failed on cancel")

    for who in (payload["owner_slack"], payload["target_slack"]):
        if who and payload["team_id"]:
            try:
                _refresh_home(client, team_id=payload["team_id"], slack_user_id=who)
            except Exception:
                log.exception("home refresh failed after cancel for %s", who)


# --- Helpers used by the scheduler when reassignments expire --------------

# --- Clear all bot DMs (utility from App Home) ----------------------------

@bolt_app.action("clearmsgs")
def handle_clearmsgs_request(ack, body, client):
    """User hit the "Clear all CommitBot DMs" button on their Home tab.
    Open a modal asking them to confirm before we wipe their DM history."""
    ack()
    trigger_id = body.get("trigger_id")
    if not trigger_id:
        return
    modal = {
        "type": "modal",
        "callback_id": "clearmsgs_modal",
        "title": {"type": "plain_text", "text": "Clear all messages?"},
        "submit": {"type": "plain_text", "text": "Yes, clear them"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn",
                "text": ("*This permanently deletes every message CommitBot "
                         "has sent you in this DM.* The chat history is gone "
                         "for good — buttons, pings, reassignment requests, "
                         "everything.")}},
            {"type": "context", "elements": [{"type": "mrkdwn",
                "text": ("Your commitments, deadlines, and future pings "
                         "continue normally — only the past chat history is "
                         "removed.")}]},
        ],
    }
    try:
        client.views_open(trigger_id=trigger_id, view=modal)
    except Exception:
        log.exception("clearmsgs modal failed")


@bolt_app.view("clearmsgs_modal")
def handle_clearmsgs_confirm(ack, body, client):
    """The user confirmed — close the modal IMMEDIATELY and do the slow
    bulk-delete in a background thread. Slack requires view submissions
    to ack within 3 seconds; chat.delete is rate-limited to ~50/minute,
    so any non-trivial cleanup would otherwise time out and the user
    would see Slack's "We had some trouble connecting" error.
    """
    ack()
    user_id = (body.get("user") or {}).get("id")
    team_id = (body.get("team") or {}).get("id") or body.get("team_id")
    if not user_id:
        return
    # Fire-and-forget. Daemon=True so the thread doesn't keep the process
    # alive if the server is shutting down.
    threading.Thread(
        target=_clearmsgs_worker,
        args=(client, user_id, team_id),
        daemon=True,
        name=f"clearmsgs-{user_id}",
    ).start()


def _clearmsgs_worker(client: Any, user_id: str, team_id: Optional[str]) -> None:
    """Background worker — opens the IM, paginates the FULL history into a
    list, then deletes every bot-posted message with a throttle + an
    explicit rate-limit retry. Refreshes the Home view at the end.

    Why two passes instead of "fetch a page, delete its bot messages,
    advance the cursor": chat.delete is Slack Tier 3 (~50/min), and a
    single Home tab session can rack up hundreds of pings. The first
    50ish deletes succeed, then Slack starts returning 429 ratelimited;
    if we'd been walking the cursor forward we'd already be past those
    messages and lose track of them. Collect-then-delete decouples
    pagination from mutation, so a rate-limited delete just sleeps and
    retries the same ts.
    """
    import time
    from slack_sdk.errors import SlackApiError

    try:
        im = client.conversations_open(users=user_id)
        channel = (im or {}).get("channel", {}).get("id")
        if not channel:
            log.warning("conversations.open returned no channel for %s", user_id)
            return
    except Exception:
        log.exception("conversations.open failed in clearmsgs")
        return

    # Pass 1: paginate the full DM history and collect bot-message ts's.
    # Doing this first means later deletes don't disturb the cursor.
    to_delete: list[str] = []
    cursor: Optional[str] = None
    while True:
        try:
            hist = client.conversations_history(
                channel=channel, limit=200, cursor=cursor,
            )
        except Exception:
            log.exception("conversations.history failed in clearmsgs")
            break
        for msg in hist.get("messages", []):
            # Only our bot's messages. User-typed messages can't be deleted
            # by us (no permission), and we wouldn't want to anyway.
            is_bot = msg.get("bot_id") or msg.get("subtype") == "bot_message"
            if is_bot and msg.get("ts"):
                to_delete.append(msg["ts"])
        cursor = (hist.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break

    # Pass 2: delete with throttle + retry. Tier 3 lets us do ~50/min;
    # a 1.1s gap keeps us comfortably under that. On a 429 we honour
    # the Retry-After header and try the same ts again instead of
    # marking it failed (which is what made the button look "stuck"
    # after the first ~50 messages and forced repeated clicks).
    deleted = 0
    failed = 0
    for ts in to_delete:
        for attempt in range(5):
            try:
                client.chat_delete(channel=channel, ts=ts)
                deleted += 1
                break
            except SlackApiError as e:
                err = (e.response or {}).get("error")
                if err == "ratelimited":
                    retry_after = 30
                    headers = getattr(e.response, "headers", {}) or {}
                    try:
                        retry_after = int(headers.get("Retry-After", retry_after))
                    except (TypeError, ValueError):
                        pass
                    log.info(
                        "clearmsgs rate-limited; sleeping %ds before retry",
                        retry_after,
                    )
                    time.sleep(max(1, retry_after))
                    continue
                # Permanent failure (message_not_found, already deleted,
                # cant_delete_message). Skip — no point retrying.
                log.debug("chat_delete %s skipped: %s", ts, err)
                failed += 1
                break
            except Exception:
                log.exception("chat_delete %s raised unexpectedly", ts)
                failed += 1
                break
        else:
            failed += 1
        time.sleep(1.1)

    log.info(
        "clearmsgs: user=%s total=%d deleted=%d failed=%d",
        user_id, len(to_delete), deleted, failed,
    )
    if team_id:
        try:
            _refresh_home(client, team_id=team_id, slack_user_id=user_id)
        except Exception:
            log.exception("home refresh after clearmsgs failed")


def retire_reassignment_dm(
    client: Any, *, channel: Optional[str], ts: Optional[str], final_text: str,
) -> None:
    """Rewrite the recipient's DM to remove the buttons + show final outcome."""
    if not (channel and ts):
        return
    try:
        client.chat_update(
            channel=channel, ts=ts, text=final_text,
            blocks=[{"type": "section",
                     "text": {"type": "mrkdwn", "text": final_text}}],
        )
    except Exception:
        log.warning(
            "chat_update failed for reassignment DM channel=%s ts=%s", channel, ts,
        )


def notify_reassignment_expired_owner(
    client: Any, *, owner_slack_user_id: str, commitment_text: str,
    target_slack_user_id: str,
) -> None:
    client.chat_postMessage(
        channel=owner_slack_user_id,
        text=(
            f":hourglass: Your reassignment of *{commitment_text}* to "
            f"<@{target_slack_user_id}> expired without a response. "
            "It's back on your plate."
        ),
    )


# ---------------------------------------------------------------------------
# Ping delivery (called from services.pings.deliver_ping)
# ---------------------------------------------------------------------------

def send_ping_dm(
    client: Any,
    *,
    user_id: str,
    commitment: Commitment,
    db: Optional[Any] = None,
) -> None:
    """Real Slack send. Called by the scheduler when DRY_RUN_PINGS is false."""
    user_tz = commitment.user.tz if commitment.user else "UTC"
    deadline_str = format_deadline(commitment.deadline, user_tz)

    level = (
        db.get(PriorityLevel, commitment.priority_level_id)
        if (db is not None and commitment.priority_level_id) else None
    )
    interval = ping_svc.current_ping_interval_minutes(commitment, level, db=db)
    at_floor = ping_svc.is_at_escalation_floor(commitment, level, db=db)
    cadence_str = ping_svc.format_interval(interval) if interval else "no pings"

    meta_bits = [f":calendar: {deadline_str}", f":bell: {cadence_str}"]
    if not commitment.escalation_enabled:
        meta_bits.append(":pause_button: escalation off")

    # Two action rows so all the buttons render reliably regardless of
    # Slack-client width. With 5 buttons in a single row, narrow DM columns
    # silently truncate or hide some — users see Hold OR Stop, not both.
    primary_row = [
        {"type": "button", "action_id": f"done::{commitment.id}",
         "text": {"type": "plain_text", "text": "Mark done", "emoji": True},
         "style": "primary"},
        {"type": "button", "action_id": f"snooze2h::{commitment.id}",
         "text": {"type": "plain_text", "text": "Snooze 2h", "emoji": True}},
        {"type": "button", "action_id": f"snoozetomorrow::{commitment.id}",
         "text": {"type": "plain_text", "text": "Tomorrow", "emoji": True}},
    ]
    # Second row groups the longer-term "park / cadence-toggle" controls.
    # Hold is indefinite (no auto-resume unless the deadline-driven sweep
    # picks it up). Stop/Resume escalation only show when a deadline exists.
    cadence_row = [
        {"type": "button", "action_id": f"hold::{commitment.id}",
         "text": {"type": "plain_text", "text": "Hold", "emoji": True}},
    ]
    if commitment.deadline:
        if not commitment.escalation_enabled:
            cadence_row.append({
                "type": "button", "action_id": f"resumeesc::{commitment.id}",
                "text": {"type": "plain_text", "text": "Resume escalation",
                         "emoji": True},
            })
        elif not at_floor:
            cadence_row.append({
                "type": "button", "action_id": f"stopesc::{commitment.id}",
                "text": {"type": "plain_text", "text": "Stop escalation",
                         "emoji": True},
            })

    client.chat_postMessage(
        channel=user_id,
        text=f":bell: {commitment.text} — {deadline_str}",
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn",
                "text": f":bell:  *{commitment.text}*"}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": "   ".join(meta_bits)},
            ]},
            {"type": "actions", "elements": primary_row},
            {"type": "actions", "elements": cadence_row},
        ],
    )
