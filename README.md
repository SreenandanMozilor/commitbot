# CommitBot

> *"I said I'd do that and forgot."*

A Slack bot + web dashboard that catches the small promises you make in
chat — "I'll send the report by Friday", "I'll review your PR tomorrow",
"I'll get you that number" — and gently reminds you about them on a
cadence that escalates as the deadline approaches.

---

## Table of contents

1. [What it does in one paragraph](#what-it-does-in-one-paragraph)
2. [Why it exists](#why-it-exists)
3. [Functionalities — what you can actually do](#functionalities--what-you-can-actually-do)
4. [States — the lifecycle of a commitment](#states--the-lifecycle-of-a-commitment)
5. [Flows — annotated user journeys](#flows--annotated-user-journeys)
   - [A. Creating a commitment via `/commit`](#a-creating-a-commitment-via-commit)
   - [B. The reassignment flow (Alice → Bob)](#b-the-reassignment-flow-alice--bob)
   - [C. The ping loop](#c-the-ping-loop)
   - [D. Sign in with Slack](#d-sign-in-with-slack)
6. [Architecture](#architecture)
   - [The three-layer rule](#the-three-layer-rule)
   - [Data model](#data-model)
   - [Background jobs](#background-jobs)
   - [Tech stack and why](#tech-stack-and-why)
7. [Running it locally](#running-it-locally)
8. [Tests](#tests)
9. [Things deliberately out of scope](#things-deliberately-out-of-scope)

---

## What it does in one paragraph

You type `/commit I'll send the spec by Friday` in any Slack channel.
CommitBot logs it, posts a public confirmation, and starts pinging you on
a cadence that **accelerates** as the deadline approaches. You can reply
"Done" from the ping DM, snooze it, put it on hold, or **hand it off to
a teammate** (who has to agree before it's theirs). Everything's also
visible in a web dashboard you sign into with your Slack account.

---

## Why it exists

Three interviews shaped this — an APM, a UX designer, a new support
hire. They all converged on the same finding: **workarounds exist, but
they don't scale.** Sticky notes work for one person. Notes apps don't
ping the next person in a dependency chain. Slack stars are invisible
to teammates. CommitBot makes commitment-tracking *ambient*: captured
where you already are, visible to the right people, reminded without
you having to babysit the reminder system.

---

## Functionalities — what you can actually do

### Capture a commitment three ways

| Method | Trigger | Where the bot logs it | Visibility |
|---|---|---|---|
| **`/commit` slash command** | Type `/commit <text>` in any channel | As yours (outbound) | Public message in the channel + your dashboard |
| **Custom regex notation** | Type a message matching a pattern you defined (e.g. `[[commit @priya draft by Mon]]`) | As yours (outbound) | Silent log (optional threaded reply) + your dashboard |
| **Right-click → Mark as commitment** | Any Slack message → context menu | Yours if it was your message; theirs (owed-to-you) if it wasn't | Your dashboard only |

`@mentions` in any of these are extracted as **recipients** — both
auto-substituted Slack pills (`<@U12345>`) and plain text (`@priya`).
Emails are skipped via a negative lookbehind.

> Behind the scenes: see `app/slack_app.py` →
> `handle_commit_slash` (`:232`), `handle_message_for_notation`
> (`:333`), `handle_message_shortcut` (`:427`). Mention parsing in
> `_extract_mentions` (`:79`).

### Set deadlines + priorities + recipients

Every commitment can carry:

- A **deadline** (optional) — entered in your local timezone, stored as
  UTC, displayed back in your zone.
- A **priority level** that controls how often you get pinged. Levels
  are entirely user-defined: you make as many as you want with whatever
  cadence rules fit your workflow.
- **Recipients** — comma-separated names or Slack mentions, the people
  you're committing to.

### Be reminded, intelligently

Each priority is just four numbers:

| Knob | What it controls |
|---|---|
| Base interval | How often you're pinged outside the escalation window. |
| Escalation window | Hours before the deadline at which the cadence starts speeding up. |
| Escalation rate | Each ping in the window is this much faster than the previous. `2.0` = doubles each time. |
| Floor | The fastest the cadence can ever get. Hard cap. |

There's also a **system-wide minimum** (`SYSTEM_MIN_PING_INTERVAL_MINUTES = 1`)
so even a misconfigured priority can't ping more than once a minute.

Each ping DM has buttons: **Mark done**, **Snooze 2h**, **Tomorrow**,
**Hold**, plus context-aware **Stop / Resume escalation**.

> Source of truth: `app/services/pings.py`. `compute_next_ping_at`
> (`:80`) is the cadence calculator — branches: no deadline → base;
> escalation off → base; overdue → floor; before window → base; inside
> window → `base / rate^stages`, floored.

### Hand a commitment off (reassignment)

Click **Reassign** on any commitment. Pick a teammate (a Slack-style
dropdown shows everyone in the workspace, not just people you've
already worked with through CommitBot). They get a DM with **Accept**
and **Decline** buttons. They have 24 hours to respond.

- **Accept** — they take over. It appears in their *Active* tab; it
  stays visible to you (read-only) in your *Reassigned* tab.
- **Decline** — bounces back to you, *Active* state.
- **Ignore** — 24h expires, also bounces back.
- **You cancel** — withdraw the request before they decide.

Re-reassignment is allowed (Bob accepted, then Bob hands to Carol —
both Alice and Bob keep visibility in their respective Reassigned tabs).

> See `app/services/reassignments.py` for the state machine and
> `app/slack_app.py:_build_reassign_modal` (`:1115`) for the modal.

### Snooze / hold

| Action | Auto-resume? |
|---|---|
| Snooze 2h | Yes — after 2 hours |
| Snooze tomorrow | Yes — at your start-of-day, your timezone |
| Hold | Indefinite. Resume manually from the dashboard, OR via the deadline-driven auto-resume rule (default: 24 hours before deadline) |

The deadline-driven auto-resume is a per-user setting
(`auto_resume_hours_before_deadline`). Set to **0** to disable; default
**24h**. So your "Hold and forget" never silently misses a deadline.

### Outcome classification

Every commitment that ends up in a terminal state (`COMPLETE`,
`ARCHIVED`, or `DELETED`) gets stamped with an **outcome**:

- **SUCCESS** — you completed it AND it was on time (or there was no
  deadline).
- **FAILED** — anything else (overdue completion, or abandoned without
  completing).

You see this two ways:

1. A small green/red chip in the bottom-right of every terminal
   commitment card.
2. Two cross-cutting tabs — **Success** and **Failed** — that filter
   all terminal commitments by outcome.

> The rule lives in **one place**: `compute_outcome` in
> `app/services/commitments.py:55`. Every transition function
> (`mark_done`, `soft_delete`, `archive`) calls into it.

### Per-user retention

In *Settings → Preferences → Auto-delete completed after X days*:

- **X > 0**: hourly sweep hard-deletes completed commitments older than
  X days (row removed from the database).
- **X = 0**: completed commitments are **archived** instead — moved to
  the *Archived* tab and kept forever. The X=0 case also fires
  **immediately on mark-done**, so the *Complete* tab stays empty for
  users who want that.

### Timezones

Every user has a `tz` field (IANA name like `Asia/Kolkata`). The DB
stores everything in UTC; conversion happens **at every boundary**:

- Dashboard deadline display + input → user's zone.
- Slack App Home + ping DMs → user's zone, label included
  (e.g. "Time (Asia/Kolkata)" on the deadline modal).
- "Snooze to tomorrow at 9am" → 9am *your* zone.

> All of this lives in **one file**: `app/tz.py` (83 lines).

### Sign in with Slack

The dashboard is gated by real Slack OAuth (OpenID Connect — not the
bot-install flow). State param is CSRF-checked with
`secrets.compare_digest`. Open-redirect guard on `next` URL. Sessions
are signed cookies (30-day expiry, `SameSite=Lax`, `Secure` in
production).

> Code: `app/routes/auth.py`. Endpoints: `/auth/slack/login`,
> `/auth/slack/callback`, `/auth/logout`.

### Onboarding gate

`/commit` and the message shortcut refuse to log commitments for users
who haven't signed in to the dashboard at least once. Otherwise we'd
silently collect rows they can't access. They get an ephemeral
"Welcome to CommitBot — sign in once at <url>" instead.

> `User.signed_in_at` is the proof. `_is_onboarded` in
> `app/slack_app.py:147`.

### Per-action ownership

Every Slack button on a commitment (Done, Snooze, Reassign, etc.)
verifies that the clicker owns the commitment before acting. Non-owners
get an ephemeral `:lock:` message.

> `_is_commitment_owner` and `_deny_non_owner` in `app/slack_app.py`.

---

## States — the lifecycle of a commitment

```
                          ┌── put_on_hold ──► ON_HOLD ── resume ──┐
                          │                      ▲                │
                          │                      │ auto-resume    │
                          │                      │ (resume_at OR  │
                          │                      │  deadline ≤ X) │
                          │                                       │
   /commit  →  ACTIVE  ───┤                                       ▼
                          │                                    ACTIVE  (back here)
                          │  request_reassignment              │
                          ├────────────► ON_HOLD (limbo)        │
                          │                  │                  │
                          │                  ├── accept ─► REASSIGNED  (under new owner)
                          │                  │                  │
                          │                  ├── decline ──────┤
                          │                  ├── cancel  ──────┤
                          │                  └── expire 24h ───┘
                          │
                          │  mark_done
                          ├─────────────────────────────► COMPLETE
                          │                                     │
                          │  soft_delete                        ├── archive ──► ARCHIVED
                          ├─────────────────────────────► DELETED          (kept forever)
                          │                                     │
                          │                                     └── 48h ──► (purged)
                          │
                          │  reopen
                          └◄─── from COMPLETE ───────────────────────────────

   Every terminal transition stamps an OUTCOME:
     completed_at AND on time?  →  SUCCESS
     otherwise                  →  FAILED
```

### State meanings in plain language

| State | What it means | Pings? | Editable? |
|---|---|---|---|
| **ACTIVE** | You're working on it. | ✓ | ✓ |
| **ON_HOLD** | Paused. Either you snoozed it, OR it's a reassignment limbo waiting for someone to accept. | ✗ | ✓ (unless in reassignment limbo) |
| **REASSIGNED** | Someone handed this off and you accepted. Functionally like ACTIVE but tagged so you can trace the chain. | ✓ | ✓ |
| **COMPLETE** | You finished it. | ✗ | ✗ |
| **ARCHIVED** | Completed and filed. Stays forever. | ✗ | ✗ |
| **DELETED** | In the bin. Purged 48h after. | ✗ | ✗ |

### The `prior_state` trick

When a commitment goes into `ON_HOLD` (manual snooze or reassignment
limbo), we stash where it came from in `Commitment.prior_state`. On
resume / decline / cancel / expire, we restore to it. So:

- Bob accepts a reassignment → state=`REASSIGNED`.
- Bob snoozes it → state=`ON_HOLD`, `prior_state=REASSIGNED`.
- Auto-resume fires → state=`REASSIGNED` again, not `ACTIVE`.

The "I was a hand-off" tag survives a snooze.

### Outcome (orthogonal to state)

| Outcome | Condition |
|---|---|
| `SUCCESS` | `completed_at` is set AND ≤ deadline (or no deadline) |
| `FAILED` | Otherwise (never completed, or completed late) |
| `NULL` | The commitment isn't in a terminal state yet |

Outcome is **stamped** on `mark_done` / `soft_delete` / `archive`.
**Cleared** on `reopen` / `restore_from_bin → ACTIVE`. **Preserved** on
`restore_from_bin → COMPLETE`.

---

## Flows — annotated user journeys

### A. Creating a commitment via `/commit`

```
You type:  /commit I'll send the report by Friday   in #general

┌─────────────────────────────────────────────────────────────────┐
│ Step 1   Slack POSTs to /slack/events                           │
│ Step 2   slack-bolt routes to handle_commit_slash               │
│ Step 3   handler calls ack() within 3s (Slack's deadline)       │
│ Step 4   onboarding gate: _find_user + signed_in_at check       │
│            └─ if not onboarded: ephemeral nudge, return         │
│ Step 5   handler posts the public message:                      │
│            "@you committed: I'll send the report by Friday"     │
│ Step 6   handler calls commit_svc.create_commitment             │
│            ├─ dedup check (workspace, channel, message_ts)      │
│            ├─ default priority resolution                       │
│            ├─ CommitmentRecipient rows for any @mentions        │
│            └─ INSERT INTO commitments                           │
│ Step 7   ping_svc.schedule_initial_ping                         │
│            └─ INSERT INTO pings (scheduled_for = now + base)    │
│ Step 8   threaded reply with a Done ✓ button                    │
│ Step 9   _refresh_home pushes updated Slack home view           │
└─────────────────────────────────────────────────────────────────┘
```

Total round-trip: ~150ms. The whole motion is "type a sentence, hit
enter, you're tracked."

### B. The reassignment flow (Alice → Bob)

```
 ALICE'S SIDE                                  BOB'S SIDE
 ────────────────                              ────────────────────
 click Reassign on a commitment
  └─ modal opens: users_select + note
                                                                
 commitment → ON_HOLD                                            
  ├─ prior_state = ACTIVE (saved)                                
  ├─ on_hold_resume_at = NULL                                    
  ├─ pending pings deleted                                       
  └─ Reassignment row: status = PENDING,                         
     expires_at = now + 24h                                      
                                                                 
                              ──DM──►   incoming-envelope        
                                        "@alice wants to hand    
                                         off 'send the report'"  
                                        [Accept] [Decline]       
                                                                 
                                                                 
                                                  Bob clicks Accept
                                                  │
                                                                ▼
                              ◄──DM──   Alice notified           
                                                  Reassignment.status = ACCEPTED
                                                  commitment.user_id   = Bob
                                                  commitment.state     = REASSIGNED
                                                  commitment.priority  = Bob's default
                                                  commitment.prior_state = NULL
                                                  fresh ping queued for Bob
                                                                 
 Alice's Reassigned tab            Bob's Active tab:
   shows the row, read-only,         shows the row, full action 
   pill: "→ now with @Bob"           buttons, gets pings on his cadence
                                                                 
 ── alternatively ──                                             
                                                  Bob clicks Decline
                                                  │
                                                                ▼
 commitment → ACTIVE (Alice owns again)           Reassignment.status = DECLINED
   prior_state cleared                            DM rewritten to retire buttons
   ping re-armed                                                
   Alice DM'd                                                   
                                                                 
 ── or, 24h passes silently ──                                   
                                                                 
 scheduler's expire_reassignments job fires every 5m:
   - Reassignment.status = EXPIRED                              
   - commitment → ACTIVE (Alice owns again)                     
   - both parties DM'd, buttons retired                         
                                                                 
 ── or, Alice changes her mind before Bob acts ──                
 click Cancel                                                    
   - Reassignment.status = CANCELLED                            
   - commitment → ACTIVE                                        
   - Bob's DM rewritten to "cancelled by sender"                
```

**Key invariants** enforced in `app/services/reassignments.py`:

- Only the current owner can request or cancel.
- Only the named recipient can accept or decline.
- Target must be in the same workspace AND onboarded (signed in).
- At most one PENDING reassignment per commitment.
- Edits are blocked while a reassignment is pending (else Bob agrees
  to one thing, inherits another).
- All transitions write `CommitmentEdit` audit-log rows.

### C. The ping loop

```
TIME 0
  /commit created the commitment. schedule_initial_ping ran.
  → Ping row created with scheduled_for = now + base_interval (4h)

TIME +60s, +120s, +180s, …
  Scheduler tick. process_due_pings:
    1. SELECT * FROM pings WHERE sent_at IS NULL AND scheduled_for <= now
    2. For each:
       a. If commitment state ∉ (ACTIVE, REASSIGNED): mark consumed, skip
       b. If user.global_pause: mark consumed, queue next ping, skip
       c. deliver_ping → either log (dry run) or call send_ping_dm
       d. db.flush() ← so the next-ping-count query sees the just-sent one
       e. compute_next_ping_at(...) returns the time for the next ping
       f. INSERT INTO pings with scheduled_for = that time

TIME +4h
  First ping fires. Bob (or Alice) gets a DM:
    🔔 send the report to leadership
    📅 Fri Dec 22, 17:00 IST   🔔 every 4h
    [Mark done] [Snooze 2h] [Tomorrow] [Hold] [Stop escalation]

TIME +8h, +12h, +16h, …   (still outside escalation window)
  Pings every 4h, no acceleration.

TIME D-24h
  We crossed the escalation_starts_at threshold.
  Next ping fires at base/rate^stages. With base=240m, rate=2,
  stages_so_far=0: interval = 240m. With stages=1: 120m. Then 60m.
  Then 30m (floor). Stays at 30m.

TIME D (deadline)
  Same — floor cadence indefinitely.

TIME any time later
  User clicks Mark done in the DM → handle_done_action →
  mark_done → state=COMPLETE, outcome=SUCCESS (or FAILED if late).
  The pending Ping row is left as-is and gets consumed silently
  on the next scheduler tick (state != ACTIVE/REASSIGNED → mark sent).
```

The cadence math, in code:

```python
# app/services/pings.py:compute_next_ping_at
if not c.escalation_enabled:    return last + base       # 'Stop' wins
if deadline is None:            return last + base
if deadline < now:              return last + floor     # overdue
if now < escalation_starts_at:  return last + base      # not yet
# inside escalation window:
interval = base / (max(rate, 1.0) ** stages)
return last + max(interval, floor)
```

### D. Sign in with Slack

```
Step 1   User visits /  with no session
Step 2   required_user dependency raises LoginRequired
Step 3   Custom exception handler in app/main.py:
           - For HTMX requests: 401 + HX-Redirect header
           - Otherwise: 303 redirect to /auth/slack/login?next=<original>
Step 4   /auth/slack/login generates a 32-byte state token, stores it
         in the session, redirects to Slack's authorize endpoint with
         scope=openid+profile+email
Step 5   User sees Slack's permission prompt, clicks Approve
Step 6   Slack redirects to /auth/slack/callback?code=...&state=...
Step 7   callback verifies state with secrets.compare_digest
Step 8   POST to openid.connect.token with the code → access_token
Step 9   GET openid.connect.userInfo with the bearer token →
           https://slack.com/user_id, https://slack.com/team_id, email, name
Step 10  Find-or-create User row, stamp signed_in_at = now
Step 11  Save slack_user_id + slack_team_id in the session cookie
Step 12  Push a fresh App Home view via views.publish so the user
         sees their commitments view immediately when they switch back
         to Slack
Step 13  303 redirect to the saved next URL (defaults to /)
```

---

## Architecture

### The three-layer rule

The most important convention in the codebase. Every piece of code
lives in exactly one layer:

```
┌─────────────────────────────────────────────────────┐
│ Entry points — "the doors"                          │
│ ──────────                                          │
│ app/slack_app.py        ← Slack buttons, slash      │
│ app/routes/dashboard.py ← web HTTP routes           │
│ app/routes/auth.py      ← OAuth                     │
│                                                     │
│ Their job: parse the request, call a service,       │
│ render a response. NO business logic here.          │
└─────────────────────────────────────────────────────┘
                       │
                       ▼  (function calls only)
┌─────────────────────────────────────────────────────┐
│ Services — "the rules"                              │
│ ──────────                                          │
│ app/services/commitments.py    ← state transitions  │
│ app/services/pings.py          ← cadence math       │
│ app/services/reassignments.py  ← hand-off flow      │
│                                                     │
│ Their job: enforce every rule about what can        │
│ happen to a commitment. NO HTTP, NO Slack, NO HTML. │
└─────────────────────────────────────────────────────┘
                       │
                       ▼  (SQLAlchemy ORM)
┌─────────────────────────────────────────────────────┐
│ Models — "the shape"                                │
│ ──────────                                          │
│ app/models.py                                       │
│                                                     │
│ Pure SQLAlchemy table declarations. NO logic.       │
└─────────────────────────────────────────────────────┘
```

**Why this matters.** A rule like *"completing a commitment computes
its outcome, bumps a version number, and writes an edit-log entry"*
lives in **one place** (`services/commitments.py:mark_done`). When you
press "Done" in Slack and when you press "Done" on the dashboard, they
call the same function. If I change the rule, both paths get the new
behavior automatically.

The broken alternative — putting business logic in route handlers —
means the rule exists in three places. Three places to keep in sync,
three places that can silently drift apart.

### Data model

```
                         ┌──────────────┐
                         │  Workspace   │      one row per Slack team
                         │  ───────────  │
                         │  slack_team_id│
                         │  bot_token    │
                         └──────┬───────┘
                                │
                                │ 1-to-many
                                ▼
                         ┌──────────────────────┐
                         │      User             │     one row per person,
                         │  ────────────────     │     per workspace
                         │  slack_user_id        │
                         │  email                │
                         │  display_name         │
                         │  tz                   │  ← timezone
                         │  signed_in_at         │  ← onboarding proof
                         │  global_pause         │
                         │  start_of_day         │
                         │  auto_delete_completed│
                         │  auto_resume_hours    │
                         └──────┬───────────────┘
                                │
                ┌───────────────┼─────────────────────┐
                │ owns          │ owns                │ owns
                ▼               ▼                     ▼
       ┌─────────────────┐  PriorityLevel        Notation
       │   Commitment    │  (cadence knobs)      (regex pattern)
       │  ─────────────  │
       │  text           │
       │  state          │  ◄── CommitmentState enum
       │  outcome        │  ◄── CommitmentOutcome enum
       │  prior_state    │  ◄── for resume/decline rollback
       │  deadline       │
       │  completed_at   │
       │  priority_level │
       │  version        │  ◄── conflict resolution
       │  last_writer    │  ('slack' or 'dashboard')
       │  workspace_id   │
       │  user_id        │  ← CURRENT owner
       └─────┬───────────┘
             │
   ┌─────────┼────────────────────┬─────────────────────┐
   │         │                    │                     │
   ▼         ▼                    ▼                     ▼
Recipient  CommitmentEdit       Reassignment         Ping
(one per   (audit log:          (one per             (one row per
 'to' on    every field         hand-off attempt,    scheduled ping,
 the row)   change, who/when)   PENDING/ACCEPTED/    indexed on
                                DECLINED/EXPIRED/    scheduled_for)
                                CANCELLED)
```

A few callouts:

- **`CommitmentRecipient`** is its own row (not a comma-separated list)
  so multi-recipient commitments and individual recipient reassignments
  are possible.
- **`CommitmentEdit`** is the audit log. Every field change writes a
  row. It's also what powers the "I handed this off" perspective —
  see `Reassignment.from_user_id` + `status=ACCEPTED`.
- **`Reassignment.note`**, **`notice_channel_id`**, **`notice_message_ts`** —
  we keep the recipient's DM details so we can `chat.update` the
  message on outcome (retire the buttons, show "you accepted").
- **`Ping`** rows let the scheduler find work in O(log n) instead of
  scanning every commitment.

### Background jobs

Five recurring jobs run in the same Python process via APScheduler:

| Job | Cadence | What it does |
|---|---|---|
| `process_due_pings` | 60s | Deliver pings, schedule the next one for each. Includes both ACTIVE and REASSIGNED states. During `global_pause`, consumes AND queues next (so unpausing doesn't leave an empty queue). |
| `purge_bin` | hourly | Hard-delete commitments that have been DELETED for >48h. |
| `auto_resume_on_hold` | 5min | Two triggers: (a) explicit `on_hold_resume_at` past, or (b) deadline is within the user's `auto_resume_hours_before_deadline` window. Skips reassignment limbo. Restores `prior_state`. |
| `expire_reassignments` | 5min | Flip PENDING reassignments past 24h to EXPIRED. Roll the commitment back to its prior state. DM both parties. |
| `auto_delete_old_completed` | hourly | Per-user retention sweep: hard-delete after X days (X > 0) OR archive (X = 0). |

All five are written to be **idempotent** — running them twice
produces the same result as once. Important for retries.

### Tech stack and why

| Choice | Why |
|---|---|
| **FastAPI** | Async-ready; plays well with both Slack webhooks (fast acks) and HTML routes. Dependency injection eats the auth + DB-session boilerplate. |
| **slack-bolt** | Official SDK. Handles signature verification, the 3-second-ack rule, retries. |
| **SQLAlchemy 2.0 + SQLite** | One-file DB, zero setup. Typed ORM catches bugs in editors. Trivial swap to Postgres later (one env var). |
| **APScheduler** | In-process. No separate worker, no Redis. Fine for low-volume; swap to Celery if you need horizontal scale. |
| **Jinja2 + HTMX** | Server-rendered HTML with surgical updates (the Done button swaps a row without a full reload). No JS build step. |
| **itsdangerous** | Signs the session cookie. Same library Flask uses. |

The connecting theme: **boring tech, picked deliberately**. Every
choice is "the standard thing."

### File map

```
app/
├── main.py            FastAPI bootloader (~95 lines)
├── config.py          Pydantic settings — reads .env
├── db.py              SQLite engine, session helpers, idempotent migrations
├── models.py          Every table
├── tz.py              UTC ↔ local helpers — the *only* file that does this
├── scheduler.py       Five recurring jobs
├── slack_app.py       Every Slack interaction (largest file)
├── init_db.py         CLI tool for fresh-install + demo seed
├── services/
│   ├── commitments.py State transitions, outcome rule, edit log
│   ├── pings.py       Cadence math, escalation curve
│   └── reassignments.py Hand-off flow
├── routes/
│   ├── auth.py        Sign in with Slack
│   └── dashboard.py   Web UI HTTP routes
└── templates/         Jinja HTML (base, dashboard, settings, login)

tests/
├── test_services.py    notation validation, dedup, versioning, cadence
├── test_escalation.py  every branch of compute_next_ping_at + scheduler
├── test_reassignments.py hand-off state machine + edge cases
├── test_outcomes.py    SUCCESS/FAILED rules across every transition
└── test_smoke.py       boot, HTTP routes, end-to-end visibility
```

---

## Running it locally

You need Python 3.13 (3.14 has issues with `pydantic-core`'s Rust
bindings).

```bash
brew install python@3.13
git clone https://github.com/SreenandanMozilor/commitbot
cd commitbot

python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Copy the example env and fill in your Slack credentials when ready.
# For local dev with DRY_RUN_PINGS=true the placeholders are fine.
cp .env.example .env

.venv/bin/uvicorn app.main:app --reload --port 8000
```

Dashboard: <http://localhost:8000>. Expose via ngrok for the Slack
webhook to reach you.

### Slack app config (one-time)

In <https://api.slack.com/apps>:

- **OAuth & Permissions → Redirect URLs** — add
  `https://<your-host>/auth/slack/callback`.
- **OAuth & Permissions → User Token Scopes** — `openid`, `profile`,
  `email` (for Sign in with Slack).
- **OAuth & Permissions → Bot Token Scopes** —
  `app_mentions:read`, `channels:history`, `chat:write`, `commands`,
  `groups:history`, `im:history`, `im:write`, `mpim:history`,
  `reactions:write`, `users:read`.
- **Slash Commands** — add `/commit` → request URL `<host>/slack/events`.
- **Interactivity & Shortcuts** — enable; request URL `<host>/slack/events`;
  add a message shortcut with callback id `mark_as_commitment`.
- **Event Subscriptions** — enable; request URL `<host>/slack/events`;
  subscribe to `app_home_opened`, `message.channels`, `message.im`,
  `message.groups`, `message.mpim`.
- **App Home** — toggle **Home Tab** on.

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

108 tests, ~3 seconds. Coverage:

- **test_services.py** — notation validation, message dedup,
  versioning, on-hold precedence, bin recovery, field validation,
  ping cadence.
- **test_escalation.py** — every branch of the cadence calculator
  (no-deadline, before window, inside window, overdue, paused,
  REASSIGNED, escalation_enabled toggle, floor enforcement, system
  min, max-stages clamp, defensive rate clamp, is_at_floor accuracy,
  `current_interval`/`compute_next` parity, reschedule semantics,
  `format_interval`, end-to-end `process_due_pings`,
  deadline-driven auto-resume).
- **test_reassignments.py** — happy path (accept/decline/cancel/expire),
  validation (self-reassign rejected, double-pending rejected,
  un-onboarded target rejected, cross-workspace rejected,
  only-active-or-reassigned rule), side effects (edit log,
  idempotency, edit-during-limbo block, `prior_state` restoration
  on decline).
- **test_outcomes.py** — every rule for SUCCESS/FAILED across
  `mark_done`, `soft_delete`, `archive`, `reopen`, `restore_from_bin`,
  plus the immediate auto-archive at X=0.
- **test_smoke.py** — end-to-end: app boots, demo seeds, every
  dashboard tab renders, mark-done round-trips through HTMX,
  cross-user mutation is rejected, **sender-side reassignment
  visibility** (the bug fix that lets Alice still see commitments
  she handed off).

---

## Things deliberately out of scope

- **Meeting AI / voice capture** — the spec called for it; MVP
  doesn't include it. Architectural hooks are reserved.
- **Jira / Zendesk integration** — same.
- **`blocked_by` dependency links** — schema-only Phase 2 hook. The
  column exists; no UI or service code reads it.
- **Daily digest** — `PriorityLevel.daily_digest_enabled` field is
  there; no scheduler job assembles digests yet.
- **Native mobile app** — the dashboard is responsive but not
  mobile-optimised.

---

## Acknowledgements

This started as a capstone project. Three interviews shaped it: an
APM who'd already patched the problem with sticky notes, a UX
designer who wanted voice capture, and a support hire who pointed out
that context-rich ticket systems still lose nuance because *every
message looks the same*. Those constraints became the design brief.
