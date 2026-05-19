# CommitBot

> *"I said I'd do that and forgot."*

A Slack bot + web dashboard for tracking the small promises you make in
chat ("I'll send the report by Friday", "I'll review your PR tomorrow")
and reminding you about them before they slip.

---

## Table of contents

- [What it does in one paragraph](#what-it-does-in-one-paragraph)
- [Why it exists](#why-it-exists)
- [How a commitment moves through the system](#how-a-commitment-moves-through-the-system)
- [Features, explained one at a time](#features-explained-one-at-a-time)
  - [1. Three ways to capture a commitment](#1-three-ways-to-capture-a-commitment)
  - [2. Per-user priority levels with escalating pings](#2-per-user-priority-levels-with-escalating-pings)
  - [3. Reassignment workflow](#3-reassignment-workflow)
  - [4. Success / Failed outcome on every terminal commitment](#4-success--failed-outcome-on-every-terminal-commitment)
  - [5. Timezones — everywhere you'd expect](#5-timezones--everywhere-youd-expect)
  - [6. Sign in with Slack (real OAuth)](#6-sign-in-with-slack-real-oauth)
  - [7. App Home — your commitments inside Slack](#7-app-home--your-commitments-inside-slack)
  - [8. Web dashboard](#8-web-dashboard)
  - [9. Auto-retention (delete or archive completed items)](#9-auto-retention-delete-or-archive-completed-items)
  - [10. Background sweeps](#10-background-sweeps)
- [Architecture in plain English](#architecture-in-plain-english)
- [Running it locally](#running-it-locally)
- [The five state-transition rules](#the-five-state-transition-rules)
- [Tests](#tests)
- [What's deliberately out of scope](#whats-deliberately-out-of-scope)

---

## What it does in one paragraph

You type `/commit I'll send the spec by Friday` in any Slack channel.
CommitBot logs it, posts a public confirmation, and starts pinging you on
a cadence that *accelerates* as the deadline approaches. You can reply
"Done" right from the ping DM, snooze it, or hand it off to a teammate
(who has to agree before they own it). Everything's also visible in a
web dashboard you sign into with your Slack account.

---

## Why it exists

This started as a research project on how people handle the
small-but-numerous promises that get made in chat. Three interviews
(an APM, a UX designer, a support hire) all converged on the same
finding: **workarounds exist, but they don't scale.** Sticky notes work
for one person. Notes apps don't ping the next person in a dependency
chain. Slack stars are invisible to everyone else. CommitBot is the
attempt to make commitment-tracking ambient — captured where you are,
visible to the right people, and reminded without you having to babysit
the reminder system.

---

## How a commitment moves through the system

```
                       ┌── put_on_hold ──► ON_HOLD ─ resume ─┐
                       │                                     │
   /commit  →  ACTIVE                                        ▼
                       │                                  ACTIVE
                       │  request_reassignment              │
                       ├──► ON_HOLD (limbo) ─ accept ──► REASSIGNED
                       │      │                             │
                       │      └─ decline/cancel/expire ─►   │
                       │                                    │
                       │  mark_done  ─────────────────►  COMPLETE
                       │                                    │
                       └─ soft_delete ─► DELETED ─ 48h ─► (gone)
                                              │ archive ─► ARCHIVED

   Every terminal transition stamps an OUTCOME:
     completed_at on time?  →  SUCCESS
     otherwise              →  FAILED
```

States in plain language:

| State | What it means | Do you get pinged? |
|---|---|---|
| **ACTIVE** | You're working on it. | Yes |
| **ON_HOLD** | Paused. Either you snoozed it, or it's waiting for someone to accept a reassignment. | No |
| **REASSIGNED** | Someone handed this off to you and you accepted. Functionally like ACTIVE but tagged so you can see your hand-offs separately. | Yes |
| **COMPLETE** | You finished it. | No |
| **ARCHIVED** | Completed and filed away. Stays forever. | No |
| **DELETED** | In the bin. Gone for good 48 hours later. | No |

---

## Features, explained one at a time

### 1. Three ways to capture a commitment

You pick whichever feels most natural in the moment.

**Method A — `/commit` slash command** (in `app/slack_app.py` at the
function `handle_commit_slash`).

Type `/commit I'll send the report by Friday` in any Slack channel where
CommitBot is a member. The bot posts a public message ("@you committed:
…") so your team sees what you said, and simultaneously logs the
commitment in the database.

```
You:  /commit I'll send the Q2 retrospective by Friday
Bot:  @you committed: I'll send the Q2 retrospective by Friday
       └─ thread: ✓ Logged. Open Home to set a deadline.
```

**Method B — Custom regex notations** (`handle_message_for_notation`).

Set up to 5 regex patterns in *Settings → Custom notations*. When a
message you send matches any of them, it gets logged automatically. The
default suggestion is `\[\[commit.*\]\]`, so you can write:

```
You:  [[commit @priya I'll send the spec tomorrow]]
```

…and the bot quietly logs it. Quiet by design — your teammates don't see
a "Logged" reply unless you ask for one (Settings → Preferences →
*Reply in thread when notations match*).

**Method C — Right-click "Mark as commitment"** (`handle_message_shortcut`).

Any Slack message (yours or someone else's) → right-click → **Mark as
commitment**. If it's your own message, you owe it. If it's someone
else's, *they* owe you (so it lands on your dashboard's "owed to me"
view).

**Behind the scenes, all three paths share:**

- **Onboarding gate** — a user who hasn't signed in to the dashboard
  yet gets a friendly nudge instead of having a commitment silently
  logged where they can't see it. (`_is_onboarded` in `slack_app.py`.)
- **Deduplication** — capturing the same Slack message twice creates
  one row, not two (`services/commitments.py:find_existing_by_slack_message`).
- **Recipient extraction** — `@user` mentions in the message are
  parsed as recipients. Plain `@priya` and the auto-substituted
  `<@U12345>` both work. Free-text names (Priya) display as `@Priya`;
  real Slack users display as a clickable pill in Slack and their
  display name on the dashboard. (`_extract_mentions`.)

### 2. Per-user priority levels with escalating pings

Each user can define their own priority levels (in *Settings → Priority
levels*). A priority is just four numbers:

| Knob | What it controls |
|---|---|
| **Base interval** | Minutes between pings before escalation kicks in. |
| **Escalation window** | Hours before the deadline at which pinging starts accelerating. |
| **Escalation rate** | How aggressively each ping speeds up the next one. `2.0` means each ping fires twice as fast as the previous. |
| **Floor** | Fastest the cadence can ever get. Hard cap. |

The math, in plain words (`app/services/pings.py:compute_next_ping_at`):

- **Outside the escalation window:** ping every *base* minutes.
- **Inside the window:** the interval shrinks each time a ping fires:
  `interval = base / (rate ^ stages_so_far)`, never going below *floor*.
- **Past the deadline:** ping at *floor* indefinitely.
- **You hit "Stop escalation":** ping at *base*, forever. Even if
  overdue.

Each ping DM has buttons: **Mark done**, **Snooze 2h**, **Tomorrow**, plus
context-aware **Stop / Resume escalation**. The "Stop escalation" button
hides automatically when the cadence is already at the floor — there's
nothing left to slow down.

There's also a **system-wide floor** (`SYSTEM_MIN_PING_INTERVAL_MINUTES =
1` in `pings.py`) — even a misconfigured priority can't ping more often
than once a minute. Defense-in-depth.

### 3. Reassignment workflow

The full transfer-ownership story. Lives in `app/services/reassignments.py`.

**How it flows:**

```
 Alice (owner)                                Bob (target)
 ─────────────                                ─────────────
 click "Reassign"
   → modal: pick Bob, optional note
 commitment → ON_HOLD (limbo)
 24-hour timer starts
                              ──► DM to Bob ─►  "Alice wants to hand off
                                                 'send the report' to you"
                                                 [Accept] [Decline]

                                                 Bob clicks Accept
                                                         │
                                                         ▼
 commitment → REASSIGNED                     commitment → REASSIGNED
 (owner = Bob, priority remaps               (appears in Bob's tab,
  to Bob's default, ping is                   he gets the pings now)
  re-queued with new cadence)

 ── or ──
                                                 Bob clicks Decline,
                                                 OR 24h expires,
                                                 OR Alice cancels:

 commitment → ACTIVE (back to Alice)
 Alice DM'd with the outcome
 Bob's DM rewritten to retire the buttons
```

**The careful bits:**

- The 24-hour limbo state is `ON_HOLD` (with `prior_state` stashed so
  you know what to return to on decline). The auto-resume sweep is
  explicitly told to ignore commitments in reassignment limbo.
- Permission checks: only the owner can request/cancel; only the named
  recipient can accept/decline. Anyone else clicking gets an ephemeral
  *"only @owner can do that"* message.
- Bob must have signed in to the dashboard at least once before Alice
  can reassign to him. Otherwise her modal shows an inline error.
- While the request is pending, the commitment can't be edited
  (otherwise Bob agrees to one thing and inherits something else).
- A chained reassignment (Alice → Bob → Carol, Carol declines) correctly
  goes back to **Bob**, not all the way to Alice. The `prior_state`
  column makes this work.

### 4. Success / Failed outcome on every terminal commitment

The simple bit, but the one users notice. Every commitment that ends up
in a terminal state (COMPLETE / ARCHIVED / DELETED) gets stamped with an
outcome:

- **SUCCESS** — you completed it AND it was on time (or there was no
  deadline).
- **FAILED** — anything else: you finished it late, or you gave up and
  deleted it.

The rule lives in **one place**: `compute_outcome` in
`services/commitments.py`. Every transition function (`mark_done`,
`soft_delete`, `archive`) calls into it. `reopen` and `restore_from_bin`
clear the outcome (the commitment is back in flight).

You see this two ways in the UI:

1. A small green/red chip in the bottom-right of every terminal
   commitment card on the dashboard.
2. Cross-cutting **Success** and **Failed** tabs that filter all
   terminal commitments by outcome, regardless of state.

### 5. Timezones — everywhere you'd expect

Every user has a `tz` field (an IANA name like `Asia/Kolkata` or
`America/New_York`). The database stores **everything in UTC** — but
the moment the data is shown to the user, or accepted from them, it's
converted. All this lives in **one file** (`app/tz.py`):

- Dashboard deadline pills: rendered in your zone.
- Dashboard deadline inputs (datetime-local): pre-filled in your zone
  and parsed back in your zone on submit.
- Slack App Home and ping DMs: same.
- Slack's deadline-set modal: the **label** changes ("Time
  (Asia/Kolkata)"), not just the value.
- "Snooze to tomorrow at 9am" means 9am in *your* zone.

You set your timezone in *Settings → Preferences*. There's an autocomplete
datalist with common zones, but any valid IANA name is accepted.

### 6. Sign in with Slack (real OAuth)

The dashboard is gated by **Sign in with Slack** (OpenID Connect — not
the bot-install OAuth). All of this lives in `app/routes/auth.py`.

**The flow:**

1. You visit `/`. No session → redirected to `/auth/slack/login`.
2. Login page → Sign in with Slack button → Slack's authorize URL.
3. You approve → Slack redirects to `/auth/slack/callback` with a
   short-lived `code`.
4. We exchange the code for an access token, call Slack's
   `openid.connect.userInfo`, and get back your `user_id`, `team_id`,
   email, and display name.
5. We create or update your User row, stamp `signed_in_at`, sign a
   session cookie, and drop you on the dashboard.

**Security:**

- CSRF: a `state` param generated with `secrets.token_urlsafe(32)`,
  stashed in the session, verified with `secrets.compare_digest` on
  callback.
- Open-redirect guard: the post-login `next` URL is only honored if
  it's a relative path (`_safe_next`).
- Session cookies are signed with `itsdangerous`, `SameSite=Lax`, and
  `Secure` when serving over HTTPS.

**Onboarding gate:** the Slack capture paths (`/commit`, message
shortcut) explicitly refuse users who haven't completed this flow at
least once. Otherwise we'd accumulate commitment rows for people who
can't access them. `User.signed_in_at` is the proof of onboarding.

### 7. App Home — your commitments inside Slack

Click **CommitBot** in your Slack sidebar → Home tab. Built by
`_build_home_view` in `slack_app.py`. Three sections, in priority order:

1. **Awaiting your response** — incoming reassignment requests, with
   Accept / Decline buttons inline.
2. **Awaiting their response** — your outgoing pending reassignments,
   with a Cancel button.
3. **Your active commitments** — every ACTIVE and REASSIGNED commitment
   you own, with buttons: *Edit deadline*, *Stop / Resume escalation*
   (only if relevant), *Reassign*, *Mark done*.

Each commitment shows its current cadence (*"🔔 every 30m"*) next to
the deadline, so you can see at a glance how aggressively the system
is pinging you.

The home tab refreshes automatically after every action, so the view
you see in Slack always reflects the latest state.

### 8. Web dashboard

The full-fidelity surface. `app/routes/dashboard.py`. Eight tabs at the
top filter commitments by state or outcome:

| Tab | Shows |
|---|---|
| Active | Things you're working on. |
| On hold | Snoozed manually OR awaiting a reassignment response. |
| Reassigned | Handed to you by a teammate (post-accept). |
| Complete | Done, not yet archived/deleted. |
| Archived | Done and filed. |
| Deleted | In the 48h bin. |
| Success | Cross-cutting filter: every terminal commitment with
            outcome=SUCCESS. |
| Failed | Same, FAILED. |

**Per-row, you get:**

- A "Reassign to a teammate" collapsible form (active commitments
  only).
- An "Edit details" panel to change text/deadline/priority/recipients.
- An "Unsaved changes" badge next to the summary, so if you collapse
  the panel mid-edit you don't lose track.
- Quick-action buttons appropriate to the state (Done / Hold / Delete
  for active, Resume / Delete for on-hold, Reopen / Archive / Delete
  for complete, etc.).

**Plus:**

- **Theme toggle**: light / dark / auto (follows system). Cookie-backed.
- **JSON / CSV export** of all your commitments.
- **"Awaiting your response" banner** at the top of the page whenever
  you have incoming reassignment requests.

### 9. Auto-retention (delete or archive completed items)

In *Settings → Preferences*, there's *Auto-delete completed after [X]
days*. How it works:

- **X > 0**: completed commitments older than X days get **hard-deleted**
  by the hourly sweep — the row is removed from the database entirely,
  no trip through the bin.
- **X = 0**: completed commitments get **archived** instead — moved to
  the Archived tab and kept forever.

The X = 0 case also kicks in **immediately on completion** — `mark_done`
checks the user's setting and calls `archive` inline if they're at 0.
So users who hate seeing finished items in their Complete tab get an
empty Complete tab.

### 10. Background sweeps

Five recurring jobs run in-process (`app/scheduler.py`):

| Job | Cadence | What it does |
|---|---|---|
| `process_due_pings` | 60s | Deliver any pings whose `scheduled_for` is now or earlier. Schedule the next one for each commitment using the cadence calculator. |
| `purge_bin` | hourly | Hard-delete commitments that have been in the DELETED state for more than 48 hours. |
| `auto_resume_on_hold` | 5min | Snoozed commitments wake back up at their `on_hold_resume_at`. Skips reassignment limbo. |
| `expire_reassignments` | 5min | Pending reassignments past their 24h window flip to EXPIRED. Commitment rolls back to its prior state, both parties are DM'd. |
| `auto_delete_old_completed` | hourly | Enforces the per-user retention rule (see §9). |

All five run via APScheduler in the same Python process as the web
server. For production scale you'd swap to Celery or RQ; the
service-layer code doesn't care.

---

## Architecture in plain English

The project follows a **three-layer convention**, strictly:

1. **Models** (`app/models.py`) describe **what the data looks like**.
   Pure SQLAlchemy declarations, no logic.
2. **Services** (`app/services/`) describe **what you can do with that
   data**. Every state transition, every validation rule, every
   recipient resolution — all here. No HTTP, no Slack.
3. **Entry points** (`app/routes/`, `app/slack_app.py`) are thin
   adapters. They take in an HTTP request or a Slack action, parse it,
   call into a service, render a response. They never reach into
   commitment fields directly.

This means a single rule like *"completing a commitment computes its
outcome and bumps the version"* lives in **one place**
(`services/commitments.py:mark_done`). Both the dashboard and the Slack
"Done" button call into it. Tests test the service layer directly,
without needing a fake HTTP request or a fake Slack payload.

```
app/
├── main.py            FastAPI wiring (~60 lines)
├── config.py          Pydantic settings (reads .env)
├── db.py              SQLite engine, sessions, idempotent migrations
├── models.py          Every table (User, Workspace, Commitment, …)
├── tz.py              UTC ↔ local helpers (one file)
├── scheduler.py       Five recurring jobs
├── slack_app.py       Every Slack interaction (largest file)
├── services/
│   ├── commitments.py State transitions, outcome rule, edit log
│   ├── pings.py       Cadence math, escalation curve
│   └── reassignments.py  Hand-off flow
├── routes/
│   ├── auth.py        Sign in with Slack
│   └── dashboard.py   Web UI HTTP routes
└── templates/         Jinja HTML (base, dashboard, settings, login)
```

**Tech choices, briefly:**

- **FastAPI** for the web framework — async-ready, plays well with both
  Slack webhooks and the dashboard's HTML routes.
- **slack-bolt** for the Slack SDK — handles signature verification,
  retries, the awkward 3-second-ack rule.
- **SQLAlchemy 2.0 + SQLite** — single file DB, zero setup. Trivial
  swap to Postgres later (change `DATABASE_URL`).
- **APScheduler** for background jobs — runs in-process; one less
  service to deploy.
- **Jinja2 + HTMX** for the dashboard UI — no JS build step, no SPA.
  Buttons that mutate state use HTMX `hx-post` to swap a single row in
  place.

---

## Running it locally

You need Python 3.13 (3.14 has issues with `pydantic-core`'s Rust
bindings). On a Mac with Homebrew:

```bash
brew install python@3.13
git clone <this repo>
cd commitbot

python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Copy the example env and fill in your Slack credentials when ready.
# For local dev with DRY_RUN_PINGS=true, the placeholders are fine.
cp .env.example .env

# Start the server.
.venv/bin/uvicorn app.main:app --reload --port 8000
```

Dashboard at <http://localhost:8000>. Slack webhook at
`http://your-tunnel/slack/events` once you expose with ngrok.

**Slack app config** (one-time, in <https://api.slack.com/apps>):

- **OAuth & Permissions → Redirect URLs**: add
  `https://<your-ngrok-host>/auth/slack/callback`.
- **OAuth & Permissions → User Token Scopes**: add `openid`, `profile`,
  `email` (for Sign in with Slack).
- **OAuth & Permissions → Bot Token Scopes**: `chat:write`, `commands`,
  `im:write`, `app_mentions:read`, `message.channels`, `message.im`,
  `message.groups`, `users:read`.
- **Slash Commands**: add `/commit` pointing at `<host>/slack/events`.
- **Interactivity & Shortcuts**: enable; request URL
  `<host>/slack/events`; add a message shortcut callback
  `mark_as_commitment`.
- **Event Subscriptions**: enable; request URL `<host>/slack/events`;
  subscribe to `app_home_opened`, `message.channels`, `message.im`,
  `message.groups`, `message.mpim`.
- **App Home**: toggle **Home Tab** on.

---

## The five state-transition rules

If you remember nothing else from this README:

1. **All field edits go through service-layer functions.** Routes and
   Slack handlers never reach into `commitment.text =`. They call
   `commit_svc.edit_text(...)`, which writes an edit-log entry and
   bumps the version atomically.

2. **Every terminal transition computes an outcome.** `mark_done`,
   `soft_delete`, and `archive` all call `compute_outcome` exactly the
   same way. There's no code path that leaves a terminal row
   unclassified.

3. **`ON_HOLD` and `REASSIGNED` aren't the same thing.** `ON_HOLD` is
   limbo — manual snooze or reassignment-pending. `REASSIGNED` is a
   live state under a new owner who accepted a hand-off. They look
   different in the UI but neither one is "done."

4. **Ping cadence respects user intent first, then deadline.** If the
   user pressed *Stop escalation*, the cadence is `base` — regardless
   of whether the commitment is overdue or inside the escalation
   window. The "fast pinging because deadline is near" logic only
   fires when escalation is enabled.

5. **Onboarding is required for Slack-side authorship.** `/commit` and
   the message shortcut refuse users who haven't signed in. Otherwise
   we'd be silently collecting data for people with no way to see it.

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

~100 tests, runs in ~3 seconds. Coverage:

- **test_services.py** — notation validation, message deduplication,
  versioning, on-hold precedence, bin, field validation, ping cadence.
- **test_escalation.py** — every branch of the cadence calculator
  (no-deadline, before window, inside window, overdue, paused,
  REASSIGNED, escalation_enabled toggle, floor, system min, max-stages
  clamp, defensive rate clamp, is_at_floor accuracy,
  current_interval/compute_next parity, reschedule, format_interval,
  process_due_pings end-to-end).
- **test_reassignments.py** — happy path (accept/decline/cancel/expire),
  validation (self-reassign rejected, double-pending rejected,
  un-onboarded target rejected, cross-workspace rejected, only-active
  rule), side effects (edit log, idempotency, edit-during-limbo block,
  prior_state restoration on decline).
- **test_outcomes.py** — every rule for SUCCESS/FAILED across
  mark_done, soft_delete, archive, reopen, restore_from_bin, plus
  immediate auto-archive at X=0.
- **test_smoke.py** — end-to-end: app boots, demo data seeds, every
  dashboard tab renders, mark-done round-trips, cross-user mutation is
  rejected.

---

## What's deliberately out of scope

- **Meeting AI / voice capture** — the spec called for it; the MVP
  doesn't include it. Architectural hooks are there.
- **Jira / Zendesk integration** — same.
- **`blocked_by` dependency links** — schema-only Phase 2 hook. The
  column exists; no UI or service code reads it.
- **Daily digest** — `PriorityLevel.daily_digest_enabled` field is
  there; no scheduler job assembles digests yet.
- **Native mobile app** — the dashboard is responsive but not
  mobile-optimised.

---

## Acknowledgements

This started as a capstone project. Three interviews shaped it: an APM
who'd already patched the problem with sticky notes, a UX designer who
wanted voice capture, and a support hire who pointed out that
context-rich ticket systems still lose nuance because *every message
looks the same*. Those constraints became the design brief.
