# CommitBot

> *"I said I'd do that and forgot."*

CommitBot is a Slack bot + web dashboard that catches the small promises
you make in chat — *"I'll send the report by Friday"*, *"I'll review
your PR tomorrow"*, *"I'll get you that number"* — and gently reminds
you about them on a cadence that escalates as the deadline approaches.

> **Status:** MVP. The core capture / pinging / reassignment / dashboard
> flows are functional, plus an opt-in **agentic capture** layer that
> watches your Slack messages and auto-logs the ones that look like
> commitments. 137 automated tests guard everything.
> A few things are deliberately deferred (Meeting AI, Jira / Zendesk
> integration, dependency-blocking links, daily digest, native mobile)
> — see [Things deliberately out of scope](#things-deliberately-out-of-scope).

---

## Problem statement

Three interviews shaped this project — an APM, a UX designer, and a
new support hire — and they all surfaced the same gap.

**Workarounds exist, but none of them scale.** Sticky notes work for one
person until you have ten of them on your desk. Personal Slack
channels capture commitments but no one else sees them. Notes apps
don't ping the next person in a dependency chain. Slack stars and
saved-for-later are invisible to teammates. *Every interviewee had a
patched-up system, every one of which broke under more load.*

**The hardest version of the problem isn't personal memory — it's
cross-person visibility.** When you tell a teammate "I'll send the
report by Friday" and forget, the cost isn't just your own
embarrassment; it's that the other person has no record either.
They're stuck remembering what they're waiting on, from whom, and when
to nudge.

**People want capture to be ambient.** Every interviewee said some
version of *"I don't want to open a new app to log this."* The capture
mechanism has to live inside the place where the commitment is made
— overwhelmingly, Slack.

CommitBot is the attempt to make commitment-tracking ambient:
**captured where you already are, surfaced to the right people,
reminded without you having to babysit the reminder system.**

---

## What it does in one paragraph

You type `/commit I'll send the spec by Friday` in any Slack channel.
CommitBot logs it, posts a public confirmation, and starts pinging you
on a cadence that **accelerates as the deadline approaches**. You can
reply *"Done"* from the ping DM, snooze it, put it on hold, or **hand
it off to a teammate** (who has to agree before it's theirs).
Or you can skip `/commit` entirely: opt into the **agent** and CommitBot
will quietly watch your messages, classify the ones that look like
promises (*"I'll review your PR tonight"*, *"remind me to email the
vendor tomorrow"*) with an LLM, and log them with an extracted deadline
and a one-click Undo if it got one wrong.
Everything's also visible in a web dashboard you sign into with your
Slack account.

---

## Architecture at a glance

```
                              ┌──────────────────────────────────┐
                              │             Slack                 │
                              │  slash commands · events ·        │
                              │  interactivity · OAuth            │
                              └────────────────┬─────────────────┘
                                               │ HTTPS
                                               ▼
   ┌──────────────┐               ┌────────────────────────────────────┐
   │   Browser    │ ─── HTTP ───► │   CommitBot  (single Python proc)  │
   │              │     HTMX      │                                    │
   │ Sign in with │               │   ┌──────────────────────────────┐ │
   │ Slack →      │               │   │ FastAPI (uvicorn)            │ │
   │ dashboard    │               │   │  /slack/events  → slack-bolt │ │
   └──────────────┘               │   │  /              → dashboard  │ │
                                  │   │  /auth/slack/*  → OAuth       │ │
                                  │   └──────────────┬───────────────┘ │
                                  │                  │                  │
                                  │                  ▼                  │
                                  │   ┌──────────────────────────────┐ │
                                  │   │ Services (business rules)    │ │
                                  │   │  commitments.py · pings.py · │ │
                                  │   │  reassignments.py · tz.py    │ │
                                  │   └──────────────┬───────────────┘ │
                                  │                  │                  │
                                  │                  ▼                  │
                                  │   ┌──────────────────────────────┐ │
                                  │   │ Models  (SQLAlchemy ORM)     │ │
                                  │   └──────────────┬───────────────┘ │
                                  │                  ▼                  │
                                  │   ┌──────────────────────────────┐ │
                                  │   │ SQLite — single file          │ │
                                  │   └──────────────────────────────┘ │
                                  │                                    │
                                  │   ┌──────────────────────────────┐ │
                                  │   │ APScheduler (7 background   │ │
                                  │   │ jobs, same process)         │ │
                                  │   │  • process_due_pings  (60s) │ │
                                  │   │  • scan_for_commitments(1m) │ │
                                  │   │  • auto_resume_on_hold (5m) │ │
                                  │   │  • expire_reassignments (5m)│ │
                                  │   │  • purge_bin           (1h) │ │
                                  │   │  • auto_delete_completed(1h)│ │
                                  │   │  • prune_agent_buffer  (24h)│ │
                                  │   └──────────────────────────────┘ │
                                  └────────────────────────────────────┘
```

The whole system is **one Python process**. No separate web / bot /
worker services. One `uvicorn` invocation runs FastAPI, slack-bolt, and
APScheduler together. Trivial to deploy; trivial to reason about.

The code follows a **three-layer rule**:

1. **Entry points** (`slack_app.py`, `routes/*`) parse requests, call
   services, render responses. **No business logic.**
2. **Services** (`services/*`) enforce every rule about what can
   happen to a commitment. **No HTTP, no Slack.**
3. **Models** (`models.py`) are pure SQLAlchemy table declarations.
   **No logic.**

So a rule like *"completing a commitment computes its outcome, bumps a
version, and writes an audit-log entry"* lives in **one function**
(`commit_svc.mark_done`). The Slack "Done" button and the dashboard
"Done" button both call that function — there's no duplication, no
drift.

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Web framework | **FastAPI** (async, Python 3.13) | Handles Slack webhooks (fast acks) and HTML routes in one app. Dependency injection eats auth + DB-session boilerplate. |
| Slack SDK | **slack-bolt** | Official SDK. Handles signature verification, the 3-second-ack rule, retries. |
| ORM | **SQLAlchemy 2.0** | Typed ORM; editors autocomplete fields and catch type mistakes before runtime. |
| Database | **SQLite** (single file) | Zero setup. Swap to Postgres later by changing one env var. |
| Scheduler | **APScheduler** (in-process) | No separate worker, no Redis, no message broker. Fine for low-medium volume. |
| HTML templates | **Jinja2 + HTMX** | Server-rendered HTML with surgical row swaps (no SPA, no JS build). |
| Sessions | **itsdangerous** | Signed-cookie sessions; same library Flask uses. |
| OAuth | **Sign in with Slack** (OpenID Connect) | Real auth, not a `?user_id=` query param. |
| LLM client | **`anthropic` SDK** (Claude Haiku 4.5) | Powers the agentic capture classifier. Falls back to a deterministic regex stub when no API key is configured, so dev + tests cost nothing. |

The connecting theme: **boring tech, picked deliberately**. Every
choice is the conventional one for its job. There's nothing here that
will surprise a future contributor.

---

## Table of contents

1. [Capture — how a commitment gets into the system](#capture--how-a-commitment-gets-into-the-system)
2. [Agentic capture — opt-in LLM auto-logging](#agentic-capture--opt-in-llm-auto-logging)
3. [Recipients — the rule about `@mentions`](#recipients--the-rule-about-mentions)
4. [Pinging — the cadence calculator](#pinging--the-cadence-calculator)
5. [States — the lifecycle of a commitment](#states--the-lifecycle-of-a-commitment)
6. [Outcomes — success vs failed](#outcomes--success-vs-failed)
7. [Reassignment — handing a commitment off](#reassignment--handing-a-commitment-off)
8. [Timezones](#timezones)
9. [Sign in with Slack + onboarding gate](#sign-in-with-slack--onboarding-gate)
10. [Permissions — who can do what](#permissions--who-can-do-what)
11. [Retention — auto-delete or auto-archive](#retention--auto-delete-or-auto-archive)
12. [The web dashboard](#the-web-dashboard)
13. [The Slack App Home tab](#the-slack-app-home-tab)
14. [End-to-end flows](#end-to-end-flows)
15. [Data model](#data-model)
16. [Background jobs](#background-jobs)
17. [Running it locally](#running-it-locally)
18. [Tests](#tests)
19. [Things deliberately out of scope](#things-deliberately-out-of-scope)

---

## Capture — how a commitment gets into the system

There are **five ways** to log a commitment. Four are deliberate user
actions; the fifth is the **agent**, which watches messages and decides
on its own. The first four are covered here; the agent has its own
[Agentic capture](#agentic-capture--opt-in-llm-auto-logging) section.

### 1. `/commit <text>` slash command

Type `/commit I'll send the report by Friday` in any Slack channel
where CommitBot is a member. The bot:

- Posts a public message: *"@you committed: I'll send the report by
  Friday"* — your teammates see what you said.
- Logs the commitment in the database, linked to that exact Slack
  message (so it can't be double-captured).
- Posts a threaded "Logged" reply with a Done button.

> Code: `handle_commit_slash` in `app/slack_app.py`.

### 2. Custom regex notation

Set up to 5 patterns in *Settings → Custom notations*. When a message
you send matches any of them, it's logged silently.

The default suggestion is `\[\[commit.*\]\]`, so you write:

```
[[commit @priya I'll send the spec tomorrow]]
```

…and the bot quietly logs it. By default no public reply (you can turn
on threaded confirmation in *Settings → Preferences*).

> Code: `handle_message_for_notation` in `app/slack_app.py`.

### 3. Message shortcut (right-click → Mark as commitment)

Any Slack message → right-click → **Mark as commitment**. Captures the
message as **your** commitment. Whether the message was yours or
someone else's doesn't change the ownership — you become the owner
either way. (To track *"someone owes me X"*, see the workaround in
[Reassignment](#reassignment--handing-a-commitment-off).)

> Code: `handle_message_shortcut` in `app/slack_app.py`.

### 4. Dashboard *New commitment* form

For commitments not made in Slack at all. Plain HTML form on the
dashboard.

> Code: `app/routes/dashboard.py:action_new_commitment`.

### Common behaviours across all four paths

- **Onboarding gate.** A user who hasn't completed [Sign in with Slack](#sign-in-with-slack--onboarding-gate)
  yet can't capture commitments from the Slack paths — they get a
  friendly ephemeral nudge directing them to the dashboard. Otherwise
  we'd silently collect data they have no way to access.
- **Deduplication.** Capturing the same Slack message twice creates one
  row, keyed on `(workspace_id, channel_id, message_ts)`.
- **Default priority.** If you don't specify one, the user's default
  level is used (auto-created on first sign-in with sensible defaults:
  4h base, 24h escalation window, 30m floor, ×2 rate).
- **Visible feedback toggles** (per-user, in *Settings → Preferences*):
  - **Threaded confirmation** (default ON) — bot replies in-thread with
    *":white_check_mark: Logged as a commitment."* and a Done button on
    every successful capture.
  - **Reaction signal** (default OFF) — bot adds a `:bookmark_tabs:`
    reaction to the captured message. Useful as a silent confirmation
    when you don't want a thread reply.
  Both flags are read in the message-event handler and the slash-command
  handler; they apply uniformly across all capture paths.

---

## Agentic capture — opt-in LLM auto-logging

The agent is a fifth capture path that doesn't require any deliberate
user action per message. Once turned on (Home tab → *Turn agent on*),
CommitBot quietly watches messages the user posts in channels it's in,
classifies the ones that look like personal commitments with an LLM,
and logs them — with an extracted deadline and a one-click Undo on the
Home tab if it got something wrong.

### Two-stage pipeline

```
   user posts a message
            │
            ▼
   ┌───────────────────────────┐    cheap, sync, regex-only
   │ buffer_message            │    — stub patterns:
   │   INSERT INTO              │      "I'll …", "I will …",
   │   agent_message_buffer     │      "I can pick up …",
   └────────────┬──────────────┘      "remind me to …",
                │                     "don't let me forget …"
                ▼
   ┌───────────────────────────┐
   │ stub pre-filter           │     If the stub *might* be a
   │  (StubProvider regexes)   │     commitment …
   └────────────┬──────────────┘
                │   yes
                ▼
   ┌───────────────────────────┐    background thread, deduped per user
   │ scan_user                 │     — drains the buffer through the
   │   provider.classify(...)  │       Anthropic API in one batched call
   │   per-row SAVEPOINT       │     — system prompt is anchored to the
   │   ┌─ create_commitment    │       user's `Current time` + `Sender
   │   │  source=AGENT          │       timezone` so "tomorrow" / "by
   │   │  agent_confidence,    │       Friday" resolve to real dates
   │   │  agent_rationale       │
   │   └─ schedule_initial_ping│
   └───────────────────────────┘

   PLUS:  scan_for_commitments  every 1 minute (scheduler tick)
          → per-user due-check: only users whose
            agent_scan_interval_minutes has elapsed
            since their last scan are actually classified.
          (Backstop sweep for anything the stub missed.)
```

The stub is the cost gate: only messages it flags get an LLM call, so a
chatty channel doesn't run up an Anthropic bill on chit-chat. The LLM
is the final arbiter — false positives in the stub just mean one extra
batched classify call, not a bogus capture.

### What counts as a commitment

The classifier's system prompt teaches three shapes, all positive:

| Shape | Examples |
|---|---|
| **First-person promise** | *"I'll send the spec by Friday"*, *"I can pick up that bug"*, *"I'll review your PR tonight"* |
| **Self-directed reminder** | *"Remind me to email the vendor tomorrow"*, *"Don't let me forget to call Priya"*, *"I should remember to file the expense report"* |
| **Future-tense uptake** | *"Will get back to you with the report tomorrow"*, *"getting that doc to you by EOD"* |

And the anti-patterns it's taught to reject:

- Vague intent (*"I'll think about it"*)
- Low-conviction qualifiers (*"maybe I'll grab lunch"*)
- Third-party action (*"John should fix that"*)
- Hypothetical conditionals (*"I'll buy you lunch if X"*)
- Past tense (*"I was going to send it yesterday"*)
- Group intent (*"we need to ship this"*)

### Deadline extraction

The user prompt header includes the sender's current local time and IANA
timezone, so the model can resolve relative phrases:

- *"tomorrow"* → next calendar day at 09:00 in the user's zone
- *"tonight"* → today at ~21:00
- *"by Friday"* → next upcoming Friday at 17:00
- *"EOD"* → today at 17:00

The returned ISO string is validated by `_parse_deadline_hint` —
anything in the past or more than a year out is rejected (the user can
set one manually from Home if the guess was useless).

### Per-user controls (Home tab)

```
Agent: ON | model: claude-haiku-4-5 | scan: every 15m | floor: ≥75% | buffered: 0
[ Turn agent off ] [ Scan recent messages ] [ Scan interval ▾ ]
```

| Control | Effect |
|---|---|
| **Turn agent on/off** | Toggles `User.agent_enabled`. When off, no buffering, no classification. |
| **Scan recent messages** | Synchronously drains the user's buffer through the LLM. Useful for "I just turned it on, classify what I've said." |
| **Scan interval** | `static_select` with 1 / 5 / 15 / 30 / 60-minute options. Backstop sweep cadence — independent of the instant-trigger path, which fires on every likely-candidate message regardless. |

The status line also surfaces (read-only) the model name, the **effective
confidence floor** (verdicts below it are silently dropped), buffer
count, and a `:construction:` dry-run badge when `AGENT_DRY_RUN=true`.

The confidence floor is set via the `AGENT_CONFIDENCE_FLOOR` env var
(default `0.75`). `User.agent_confidence_floor_pct` exists as a per-user
override column, but there's no UI binding yet — change it via DB or
plumb a form when you need it.

### The Undo affordance

Every fresh agent capture shows up in a *Recently auto-captured* strip
on the Home tab for `AGENT_UNDO_WINDOW_MINUTES` (default 60). Each row
shows the captured text, the model's rationale (truncated to ~240
chars), and the confidence percentage, plus three buttons:

- **Undo (delete)** — **hard-deletes** the commitment. A false positive
  shouldn't pollute the user's failed-commitments history, so this
  bypasses the 48h Bin entirely. Behind a Slack confirmation modal so
  it's not a single-click footgun. Refused for non-agent captures and
  for agent captures past their window.
- **Set deadline** — opens the same modal as the rest of Home, so the
  user can correct or set a deadline the model missed.
- **Mark done** — for the case where the agent caught the commitment
  but you'd already finished it; one click to terminal.

### Safety rails

- **Opt-in.** `User.agent_enabled` is False by default. The bot never
  classifies messages from users who haven't turned it on.
- **Buffer retention.** Raw message text in `AgentMessageBuffer` is
  pruned after `AGENT_BUFFER_RETENTION_DAYS` (default 7), classified or
  not. We don't keep arbitrary Slack content indefinitely.
- **Prompt-injection escape.** Message text is wrapped in `json.dumps`
  before going into the user prompt, so a message containing fake
  verdict JSON can't smuggle a captured commitment with a fabricated
  confidence.
- **Per-row SAVEPOINTs.** A failure on row N (DB hiccup, dedup race
  with the notation path) doesn't roll back rows 0..N-1.
- **Dry-run mode.** `AGENT_DRY_RUN=true` runs the full classify loop
  but skips the writes — useful for prompt tuning. Default `false`.
- **Dedup with the notation path.** If a message matches both a custom
  notation and the agent's verdict, the `(workspace, channel, ts)`
  unique constraint on `Commitment` ensures one row.

> Code: `app/services/agent.py` (orchestration), `app/services/llm.py`
> (provider abstraction + Anthropic + stub), `AgentMessageBuffer` in
> `app/models.py`, agent UI in `_build_home_agent_section` of
> `app/slack_app.py`.

---

## Recipients — the rule about `@mentions`

When a commitment is captured, *who* is it owed to? CommitBot extracts
recipients from `@mentions` in the message — but with different rules
for each capture path:

| Capture path | Recipients extracted? |
|---|---|
| `/commit` slash command | **Always** — any `@mention` in the text becomes a recipient. |
| Custom notation | **Only if the notation pattern itself contains `@`.** A pattern like `\[\[commit.*@.*\]\]` opts into mention extraction; a pattern like `\[\[note.*\]\]` deliberately ignores `@`s in matching messages. |
| Right-click → Mark as commitment | **From `@mentions` in the message text, if any.** If the message has no `@`, no recipients are stored. |
| Dashboard *New commitment* form | From the explicit comma-separated *Who's this to?* field. |
| Agentic capture | **Always** — `@mention`s in the buffered message text become recipients. The LLM also returns plain-text `recipient_hints` (free-form names), which are currently logged but not persisted. |

**Two forms of mentions are recognised** in the Slack paths:

- Tab-completed Slack mentions (`<@U12345>`, what Slack auto-substitutes
  when you tab-complete a name). Stored as Slack user IDs; rendered as
  blue clickable pills in Slack and as display names in the dashboard.
- Plain text mentions (`@priya`, what you type when you don't tab-complete).
  Stored as the literal name; rendered as `@priya`.

Emails (`name@host.com`) are deliberately skipped via a negative
lookbehind in the parser, so they don't get accidentally picked up.

> Parser: `_extract_mentions` in `app/slack_app.py`.
> Per-notation opt-in: see `handle_message_for_notation` in
> `app/slack_app.py` — looks for `"@" in matched_pattern`.

---

## Pinging — the cadence calculator

You define **priority levels** — as many as you want — in *Settings →
Priority levels*. Each level has four numbers:

| Knob | What it controls |
|---|---|
| **Base interval** | Minutes between pings before escalation kicks in. |
| **Escalation window** | Hours before the deadline at which the cadence starts speeding up. |
| **Escalation rate** | Each ping inside the window is this much faster than the previous. `2.0` doubles each step. |
| **Floor** | The fastest the cadence can ever get. Hard cap. |

There's also a **system-wide minimum** (`SYSTEM_MIN_PING_INTERVAL_MINUTES = 1`)
so even a misconfigured level can never ping more often than once a
minute. Defense in depth.

### The actual math

```python
# pseudocode of compute_next_ping_at()
if not commitment.escalation_enabled:    return last + base  # "Stop" wins
if commitment.deadline is None:          return last + base
if commitment.deadline < now:            return last + floor  # overdue
if now < escalation_starts_at:           return last + base
# inside escalation window:
interval = base / (max(rate, 1.0) ** stages_so_far)
return last + max(interval, floor)
```

With `base=240m, rate=2, floor=30m, window=24h`:

| Ping # in window | Interval until next |
|---|---|
| 0 (window just opened) | 240 m (= base) |
| 1 | 120 m |
| 2 | 60 m |
| 3 | 30 m (= floor) |
| 4+ | 30 m (floor binds) |

Press *Stop escalation* and the cadence stays at `base` regardless of
deadline — even when overdue.

### Ping DM buttons

Every ping is a Slack DM with:

- **Mark done** — completes the commitment.
- **Snooze 2h** — puts it on hold; auto-resumes in 2 hours.
- **Tomorrow** — auto-resumes at your start-of-day, your timezone.
- **Hold** — indefinite hold. No auto-resume unless your
  *Auto-resume hours-before-deadline* setting triggers (see below).
- **Stop / Resume escalation** (context-aware) — hidden when the
  cadence is already at the floor (nothing left to slow down).

> Code: `app/services/pings.py` for cadence math; `send_ping_dm`
> in `app/slack_app.py` for the DM block kit.

### Auto-resume hours-before-deadline

A per-user setting in *Settings → Preferences* (default **24 hours**).
When you put a commitment on Hold, the scheduler watches for its
deadline approaching: when there are X hours or fewer until the
deadline, the commitment auto-resumes back into the same state it was
in before the hold (ACTIVE or REASSIGNED). Set to **0** to disable.

This means *"hold and forget"* doesn't accidentally let you miss a
deadline. The held commitment will surface itself when it matters.

---

## States — the lifecycle of a commitment

```
                          ┌── put_on_hold ──► ON_HOLD ── resume ──┐
                          │                      ▲                │
                          │                      │ auto-resume    │
                          │                      │  (resume_at OR │
                          │                      │   deadline ≤ X)│
                          │                                       │
   created ──►   ACTIVE  ─┤                                       ▼
                          │                                  prior_state
                          │  request_reassignment                  │
                          ├──► ON_HOLD (reassignment limbo) ──┐    │
                          │                                   │    │
                          │       ┌── accept ──► REASSIGNED ◄─┤    │
                          │       ├── decline ─────────────────┤    │
                          │       ├── cancel  ─────────────────┤    │
                          │       └── expire 24h ──────────────┘    │
                          │                                         │
                          │  mark_done                              │
                          ├─────────────────────────────► COMPLETE  │
                          │                                  │      │
                          │  soft_delete                     │      │
                          ├─────────────────────────────► DELETED   │
                          │                                  │      │
                          │                                  └─48h─►(purged)
                          │
                          │  archive (from COMPLETE or DELETED)
                          └────────────────────────────► ARCHIVED

   Every terminal transition (COMPLETE / ARCHIVED / DELETED) stamps
   an OUTCOME:
      completed_at AND on time?  →  SUCCESS
      otherwise                  →  FAILED
```

### What each state means

| State | Plain English | Pings? | Editable? |
|---|---|---|---|
| **ACTIVE** | You're working on it. | Yes | Yes |
| **ON_HOLD** | Paused — either you snoozed it, OR a reassignment is awaiting response. | No | Yes (unless awaiting reassignment) |
| **REASSIGNED** | Handed to you by a teammate; you accepted. Functionally like ACTIVE but tagged so the chain is traceable. | Yes | Yes |
| **COMPLETE** | You finished it. | No | No |
| **ARCHIVED** | Completed and filed for keeps. | No | No |
| **DELETED** | In the 48h bin. Will be purged. | No | No |

### The `prior_state` mechanism

When a commitment moves into `ON_HOLD` (manual snooze or reassignment
limbo), we stash where it came from in `Commitment.prior_state`. On
resume / decline / cancel / expire, we restore to that state. So:

```
ACTIVE  →  ON_HOLD (snooze)  → resume  →  ACTIVE      ✓
REASSIGNED  →  ON_HOLD (snooze)  → resume  →  REASSIGNED ✓
ACTIVE  →  ON_HOLD (reassign limbo)  → decline  →  ACTIVE  ✓
REASSIGNED  →  ON_HOLD (reassign limbo)  → decline  →  REASSIGNED  ✓
```

The "I was a hand-off" tag survives a snooze. Bob → Carol declined?
Goes back to Bob as REASSIGNED, not silently demoted to ACTIVE.

---

## Outcomes — success vs failed

Every commitment that ends up in a terminal state gets stamped with an
**outcome**:

- **SUCCESS** — `completed_at` is set AND it's at-or-before the deadline
  (or there was no deadline).
- **FAILED** — anything else: you finished late, or you gave up without
  completing.

The rule lives in **one place** — `compute_outcome` in
`app/services/commitments.py` — and is called by every terminal
transition (`mark_done`, `soft_delete`, `archive`). Reopening or
restoring-to-ACTIVE clears the outcome; restoring-to-COMPLETE preserves
it.

The dashboard surfaces outcomes two ways:

1. A small green/red chip in the bottom-right corner of every terminal
   commitment card.
2. Two cross-cutting **Success** and **Failed** tabs that filter all
   terminal commitments by outcome regardless of state.

---

## Reassignment — handing a commitment off

Click **Reassign** on any of your active commitments. Pick a teammate
from a dropdown that lists **everyone in the workspace** (not just
people you've already worked with through CommitBot — we fetch the
list via Slack's `users.list` API and cache it for 5 minutes). Add an
optional note explaining why you're handing it off.

The recipient gets a DM with **Accept** and **Decline** buttons. They
have **24 hours** to respond.

### The four resolution paths

| Outcome | What happens to the commitment |
|---|---|
| **Accept** | Owner changes to them. State → REASSIGNED. Priority remapped to *their* default. A fresh ping is queued under their cadence. You still see the row in your *Reassigned* tab as **read-only** with `→ now with @them`. |
| **Decline** | Stays with you, state → ACTIVE (or back to your prior state). They get no more reminders. You're DM'd. |
| **Cancel** (you change your mind) | Same as decline. Their DM is rewritten to "cancelled by sender". |
| **Expire** (24h with no response) | Same as decline. Both parties are DM'd by the scheduler job. |

### Re-reassignment works naturally

Alice → Bob (accepted) → Bob → Carol (accepted): both Alice and Bob
see the row in their *Reassigned* tabs (read-only); Carol sees it in
her *Active* tab. The Reassignment table records every hop as a
separate row, so chains of any length work without schema gymnastics.

### Invariants enforced in the service layer

- Only the current owner can request or cancel.
- Only the named recipient can accept or decline.
- Target must be **onboarded** (signed in via OAuth at least once). The
  dropdown shows un-onboarded teammates with a "— not signed in"
  label and disables them.
- Target must be in the **same Slack workspace**.
- At most **one PENDING** reassignment per commitment at a time.
- Field edits are **blocked** while a reassignment is pending — Bob
  agreed to one thing, Alice can't change it under him.
- All transitions write `CommitmentEdit` rows.

> Code: `app/services/reassignments.py`. Slack modal in
> `_build_reassign_modal` in `app/slack_app.py`.

### Workaround: "they owe me X"

CommitBot doesn't have a built-in "owed to me" view (that's a
future-phase feature). If Bob promises you something in Slack and you
want it tracked as *his* commitment, do this:

1. Right-click his message → Mark as commitment. Logs the text as
   yours (temporarily).
2. Hit **Reassign**, pick Bob, send.
3. Bob gets the DM with Accept/Decline. If he accepts, the commitment
   is now his — you keep read-only visibility in your *Reassigned* tab.

Same end state as a built-in feature, with one extra click. (The trade-off
is that briefly between steps 1 and 2 the commitment lives under your
ownership.)

---

## Timezones

Every user has a `tz` field (an IANA name like `Asia/Kolkata`). The DB
stores **everything in UTC**. Conversion happens at every boundary
where data crosses into or out of user view:

- Dashboard deadline pills — rendered in your zone.
- Dashboard deadline inputs (datetime-local) — pre-filled in your zone
  and parsed back in your zone on submit.
- Slack App Home and ping DMs — rendered in your zone.
- Slack's deadline-set modal — the **label** changes (e.g. "Time
  (Asia/Kolkata)"), so you know what zone the picker is in.
- *Snooze to tomorrow at 9am* — means 9am in *your* zone.

All conversion lives in **one file**: `app/tz.py` (83 lines). The rest
of the codebase stays UTC-only.

---

## Sign in with Slack + onboarding gate

The dashboard is gated by real Slack OAuth — OpenID Connect, not the
bot-install flow.

### The flow

1. Visit `/` with no session. The route's `required_user` dependency
   raises `LoginRequired`.
2. A custom exception handler in `app/main.py` redirects to
   `/auth/slack/login?next=<original>`.
3. We generate a 32-byte state token (`secrets.token_urlsafe(32)`),
   stash it in the session, and redirect to Slack's authorize URL with
   scope `openid profile email`.
4. User approves on Slack's page. Slack redirects to
   `/auth/slack/callback?code=...&state=...`.
5. We verify the state with `secrets.compare_digest` (timing-safe).
6. Exchange the code at `openid.connect.token` for an access token.
7. Call `openid.connect.userInfo` with the access token to get
   `slack_user_id`, `slack_team_id`, email, and display name.
8. Find or create the `User` row. Stamp `signed_in_at = now`.
9. Sign a session cookie with `itsdangerous`. 30-day expiry,
   `SameSite=Lax`, `Secure` in production.
10. Push a fresh App Home view via `views.publish` so the user sees
    their commitments view immediately when they switch back to Slack.

> Code: `app/routes/auth.py`. Session middleware in `app/main.py`.

### Onboarding gate

The Slack capture paths (`/commit`, message shortcut) **refuse to log
commitments** for users who haven't completed Sign in with Slack at
least once. Otherwise we'd silently collect rows they have no way to
access. They get an ephemeral *"Welcome to CommitBot — sign in once at
<dashboard URL>"* message instead.

The custom-notation capture path skips silently for un-onboarded users
(notations are passive, we don't want to surface nudges on every
matching message).

`User.signed_in_at` is the proof of onboarding. Helper: `_is_onboarded`
in `app/slack_app.py`.

---

## Permissions — who can do what

Every Slack button on a commitment row checks that the clicker is the
current owner before acting. Non-owners get an ephemeral
`:lock: Only @owner can act on this commitment` message.

This is **non-trivial** because Slack lets anyone in a channel click a
button rendered into the channel. Without this check, the threaded
confirmation message after `/commit` would let any channel member press
"Done" on someone else's commitment.

> Helpers: `_is_commitment_owner` and `_deny_non_owner` in
> `app/slack_app.py`. Applied to every action handler — Done, Snooze
> (2h, Tomorrow, Hold), Set/Clear deadline, Stop/Resume escalation,
> Reassign, Cancel reassignment.

For reassignments specifically: accept/decline can only be done by the
named target (different check, see `_check_actor_is_target`).

---

## Retention — auto-delete or auto-archive

A per-user setting in *Settings → Preferences*: *Auto-delete completed
after N days*.

- **N > 0** — hourly sweep **hard-deletes** completed commitments older
  than N days. The row is removed from the database entirely; no trip
  through the 48h bin. The original "save space" intent.
- **N = 0** — completed commitments are **archived** instead. Moved to
  the *Archived* tab and kept forever. The "safe default" for users
  who never want their history destroyed.

The N = 0 case also fires **immediately on mark-done** (not just via
the hourly sweep), so the *Complete* tab stays empty for users who want
a clean Complete view.

> Sweep: `auto_delete_old_completed` in `app/scheduler.py`.
> Inline at completion: `mark_done` in `app/services/commitments.py`.

---

## The web dashboard

Server-rendered HTML with HTMX for surgical updates. No JS build, no
SPA. Theme: light / dark / auto (follows system).

### Tabs

| Tab | What it shows |
|---|---|
| **Active** | Live commitments you own (state IN ACTIVE, REASSIGNED). |
| **On hold** | Snoozed manually OR awaiting a reassignment response. |
| **Reassigned** | Commitments you **handed off** (someone else accepted). Read-only view; pill shows current owner. |
| **Complete** | Done, not yet archived/deleted. |
| **Archived** | Done and filed. |
| **Deleted** | In the 48h bin. Each row has **Restore** (rolls it back to its `prior_state` — ACTIVE, ON_HOLD, REASSIGNED, or COMPLETE) and **Purge** (immediate hard-delete, skips the 48h timer) buttons. |
| **Success** | Cross-cutting filter — all terminal commitments with `outcome=SUCCESS`. |
| **Failed** | Same, `outcome=FAILED`. |

### Per-row UI

- A **"Reassign to a teammate"** collapsible form (active commitments
  only) — dropdown of every workspace member; un-onboarded ones shown
  but disabled with a `— not signed in` label.
- An **"Edit details"** panel for text / deadline / priority /
  recipients.
- An **"Unsaved changes"** badge next to the panel's summary, so if you
  collapse the panel mid-edit you don't lose track of the dirty state.
- A **green/red outcome chip** in the bottom-right corner of terminal
  commitments.
- Quick-action buttons appropriate to the state (Done / Hold / Delete
  for active; Resume / Delete for on-hold; Reopen / Archive / Delete
  for complete; etc.).

### Top-of-page banner: incoming reassignment requests

When someone has reassigned a commitment to you, an accent-colored
banner appears at the top of the dashboard showing all pending
requests with **Accept** and **Decline** buttons inline. So you don't
have to dig through Slack DMs.

### Exports

JSON and CSV exports of all your commitments at `/export/json` and
`/export/csv`.

---

## The Slack App Home tab

Open CommitBot in your Slack sidebar — the Home tab is your in-Slack
dashboard. Sections, in priority order:

1. **Awaiting your response** — pending incoming reassignment requests,
   with Accept / Decline buttons.
2. **Awaiting their response** — your outgoing pending reassignments,
   with a Cancel button.
3. **Your active commitments** — every ACTIVE and REASSIGNED commitment
   you own. Each shows the deadline, current cadence (e.g. *"🔔 every
   30m"*), recipients, and a row of buttons:
   - **Edit deadline** / **Set deadline** (label depends on whether one
     is set) — opens a modal with a date+time picker labelled in the
     user's timezone.
   - **Hold** — indefinite pause; the commitment moves to ON_HOLD,
     `prior_state` is stashed, and auto-resume kicks in only via the
     deadline-window trigger (no `on_hold_resume_at` set).
   - **Reassign** — opens the modal with the workspace-wide member
     dropdown (un-onboarded teammates shown but disabled).
   - **Mark done** — terminal transition; outcome stamp written here.
   - **Stop escalation** / **Resume escalation** (context-aware) —
     hidden when the cadence is already at the floor.
4. **Agent strip** — one-line status (ON/off, model, scan interval,
   confidence floor, buffered count, dry-run badge) and three controls:
   *Turn agent on/off*, *Scan recent messages*, and a *Scan interval*
   dropdown (1 / 5 / 15 / 30 / 60 min). When the agent has captured
   anything in the last `AGENT_UNDO_WINDOW_MINUTES`, those captures
   render below with confidence %, rationale, and an **Undo (delete)**
   button.
5. **Footer** — *Clear all CommitBot DMs* button that bulk-deletes the
   bot's old pings and notification DMs from your DMs view (runs in a
   background thread so the click acks instantly).

The home view auto-refreshes after every action via `views.publish`,
so what you see in Slack always reflects the latest state.

> Code: `_build_home_view` and `_build_home_agent_section` in
> `app/slack_app.py`.

---

## End-to-end flows

### A. Creating a commitment via `/commit`

```
You type:  /commit I'll send the report by Friday   in #general

  1. Slack POSTs to /slack/events
  2. slack-bolt routes to handle_commit_slash
  3. handler calls ack() within 3s (Slack's deadline)
  4. onboarding gate: _find_user + signed_in_at check
        └─ if not onboarded: ephemeral nudge, return
  5. handler posts the public channel message
  6. handler calls commit_svc.create_commitment
        ├─ dedup check (workspace, channel, message_ts)
        ├─ default-priority resolution
        ├─ CommitmentRecipient rows for any @mentions
        └─ INSERT INTO commitments
        ↑ if anything fails, the channel post is deleted to
          avoid an orphan claim with no commitment row backing it
  7. ping_svc.schedule_initial_ping
        └─ INSERT INTO pings (scheduled_for = now + base)
  8. threaded reply with a Done ✓ button (owner-gated)
  9. _refresh_home pushes updated Slack home view
```

Total round-trip: ~150 ms.

### B. The reassignment flow (Alice → Bob)

```
 ALICE                                          BOB
 ─────                                          ────
 clicks Reassign → modal: pick Bob + note
   │
   ▼
 commitment → ON_HOLD                                            
   ├─ prior_state = ACTIVE (stashed)                              
   ├─ on_hold_resume_at = NULL                                    
   ├─ pending pings deleted                                       
   └─ Reassignment row: PENDING, expires_at = now + 24h           
                                                                  
                              ──DM──►   "@alice wants to hand off
                                         'send the report' to you"
                                        [Accept] [Decline]        
                                                                  
                                            Bob clicks Accept ────┘
                                            │
                                                                  ▼
                              ◄──DM──   Alice notified            Reassignment.status=ACCEPTED
                                                                  Commitment.user_id=Bob
                                                                  state=REASSIGNED
                                                                  priority=Bob's default
                                                                  fresh ping queued for Bob

 Alice's Reassigned tab shows row,         Bob's Active tab shows row,
 read-only, "→ now with @Bob"              full action buttons, gets pings

 ── alternatives ──

  Bob clicks Decline → Reassignment.status=DECLINED, commitment → ACTIVE (Alice)
  Alice clicks Cancel → status=CANCELLED, commitment → ACTIVE (Alice), Bob's DM retired
  24h with no response → scheduler's expire_reassignments job: status=EXPIRED, both DM'd
```

### C. The ping loop

```
TIME 0
  /commit created the commitment. schedule_initial_ping ran.
  → Ping row created with scheduled_for = now + base_interval (e.g. 4h)

EVERY 60 SECONDS
  Scheduler tick — process_due_pings:
    1. SELECT * FROM pings WHERE sent_at IS NULL AND scheduled_for <= now
    2. For each:
       a. If commitment state ∉ (ACTIVE, REASSIGNED): mark consumed, skip
       b. If user.global_pause: mark consumed, queue next ping (keeps the
          queue primed), skip
       c. deliver_ping → either log (dry run) or send_ping_dm
       d. db.flush() so the next-ping-count query sees the just-sent one
       e. compute_next_ping_at(...) — returns the time for the next ping
       f. INSERT INTO pings with scheduled_for = that time

TIME D - escalation_window
  Inside the escalation window. Each ping's interval shrinks:
     interval = base / rate^stages_so_far,  floored.

TIME D (deadline)  and beyond
  Overdue mode — pings keep firing at floor cadence indefinitely.
```

### D. Sign in with Slack

Already detailed in the [Sign in with Slack section](#sign-in-with-slack--onboarding-gate)
above.

---

## Data model

```
                         ┌──────────────┐
                         │  Workspace   │      one row per Slack team
                         │  ───────────  │
                         │  slack_team_id│
                         │  bot_token    │
                         └──────┬───────┘
                                │ 1-to-many
                                ▼
              ┌──────────────────────────────────┐
              │             User                  │     one row per person,
              │  ─────────────────────────────────│     per workspace
              │  slack_user_id   email            │
              │  display_name    tz               │ ← timezone
              │  signed_in_at                     │ ← onboarding proof
              │  global_pause    start_of_day     │
              │  auto_delete_completed_after_days │
              │  auto_resume_hours_before_deadline│
              │  agent_enabled                    │ ← opt-in to LLM capture
              │  agent_confidence_floor_pct       │ ← per-user override
              │  agent_scan_interval_minutes      │ ← backstop sweep cadence
              │  last_agent_scan_at               │ ← for due-check
              └──────┬────────────────────────────┘
                     │
              ┌──────┼──────────────────┐
              │      │ owns             │ owns
              ▼      ▼                  ▼
   ┌─────────────────┐  PriorityLevel    Notation
   │   Commitment    │  (cadence knobs)  (regex pattern)
   │  ─────────────  │
   │  text           │
   │  state          │ ◄── CommitmentState enum
   │  outcome        │ ◄── CommitmentOutcome enum
   │  prior_state    │ ◄── stashes pre-hold state
   │  deadline       │
   │  completed_at   │
   │  priority_level │
   │  source         │ ◄── CaptureSource enum (incl. AGENT)
   │  agent_confidence, agent_rationale  │ ← set on AGENT captures only
   │  version        │ ◄── for conflict resolution
   │  last_writer    │     ('slack' or 'dashboard')
   │  workspace_id   │
   │  user_id        │ ← CURRENT owner
   └─────┬───────────┘
         │
  ┌──────┼─────────────────┬──────────────────────┐
  │      │                 │                      │
  ▼      ▼                 ▼                      ▼
Recipient CommitmentEdit   Reassignment           Ping
(one per  (audit log: who  (one row per hand-off  (one row per scheduled
 'to' on   changed what,    attempt;  PENDING /    ping, indexed on
 the row)  when, where)     ACCEPTED / DECLINED /  scheduled_for for fast
                            EXPIRED / CANCELLED)   sweep queries)

   ┌────────────────────────────────────────┐
   │  AgentMessageBuffer                     │  (used by the agent only)
   │  ────────────────────────────────────── │
   │  user_id, slack_channel_id,             │  unique together — Slack
   │  slack_message_ts                       │  event retries don't double-buffer
   │  text  (capped at 2000 chars)           │
   │  created_at, processed_at               │  processed_at = "already classified"
   └────────────────────────────────────────┘
```

A few callouts:

- **`CommitmentRecipient`** is its own row (not a comma-separated list)
  so multi-recipient commitments work and individual recipients can be
  changed independently.
- **`CommitmentEdit`** is the audit log. Every field change writes a
  row. It also powers the *"I handed this off"* perspective — the
  Reassigned tab's query joins on it.
- **`Reassignment.note`, `notice_channel_id`, `notice_message_ts`** —
  we persist the recipient's DM coordinates so we can `chat.update`
  the message on outcome (retire the buttons, show "you accepted").
- **`Ping`** rows let the scheduler find work in `O(log n)` instead of
  scanning every commitment.
- **`AgentMessageBuffer`** is the only table that stores raw Slack
  message text. The prune job clears it after `AGENT_BUFFER_RETENTION_DAYS`
  so we don't keep arbitrary chat content beyond what the classifier
  needs.

---

## Background jobs

Seven recurring jobs run in the same Python process via APScheduler:

| Job | Cadence | What it does |
|---|---|---|
| `process_due_pings` | 60s | Deliver pings whose `scheduled_for` is now or earlier; schedule the next ping for each. Includes both ACTIVE and REASSIGNED states. During `global_pause`, consumes AND queues next (so unpausing doesn't leave an empty queue). |
| `scan_for_commitments` | 60s | Backstop sweep for the agent: iterate users with `agent_enabled=True` and run `scan_user` for each one whose per-user `agent_scan_interval_minutes` has elapsed since `last_agent_scan_at`. Instant-trigger threads handle latency-sensitive captures; this job catches the long tail the stub pre-filter didn't flag. |
| `purge_bin` | 1h | Hard-delete commitments that have been DELETED for >48h. |
| `auto_resume_on_hold` | 5m | Two triggers: (a) explicit `on_hold_resume_at` past, or (b) deadline within the user's `auto_resume_hours_before_deadline` window. Skips reassignment limbo. Restores `prior_state`. |
| `expire_reassignments` | 5m | Flip PENDING reassignments past 24h to EXPIRED. Roll the commitment back. DM both parties. |
| `auto_delete_old_completed` | 1h | Per-user retention: hard-delete after X days (X > 0) OR archive (X = 0). |
| `prune_agent_buffer` | 24h | Delete `AgentMessageBuffer` rows older than `AGENT_BUFFER_RETENTION_DAYS` (default 7). Independent of whether they were classified — raw message text doesn't live indefinitely. |

All seven are written to be **idempotent** — running them twice
produces the same result as once. Important for retries.

---

## Running it locally

You need **Python 3.13** (3.14 has issues with `pydantic-core`'s Rust
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

Dashboard: <http://localhost:8000>. Expose via ngrok / Cloudflare Tunnel
for the Slack webhook to reach you.

### Optional: enable the agent

Set `ANTHROPIC_API_KEY` in `.env` to wire up real LLM classification.
Without it, the agent falls back to the deterministic stub classifier —
fine for development, useless for production (regex only, no real
language understanding). Other agent knobs (`AGENT_MODEL`, `AGENT_DRY_RUN`,
`AGENT_CONFIDENCE_FLOOR`, `AGENT_UNDO_WINDOW_MINUTES`,
`AGENT_BUFFER_RETENTION_DAYS`, `AGENT_SCAN_INTERVAL_MINUTES`) are
documented in `.env.example`. Per-user toggles live on the Slack Home
tab and the dashboard settings page.

### Slack app config (one-time)

In <https://api.slack.com/apps>:

- **OAuth & Permissions → Redirect URLs** — add
  `https://<your-host>/auth/slack/callback`.
- **OAuth & Permissions → User Token Scopes** — `openid`, `profile`,
  `email` (Sign in with Slack only needs these three).
- **OAuth & Permissions → Bot Token Scopes** —
  `app_mentions:read`, `channels:history`, `chat:write`,
  `chat:write.public`, `commands`, `groups:history`, `im:history`,
  `im:write`, `mpim:history`, `reactions:write`, `users:read`.
- **Slash Commands** — add `/commit` → request URL
  `https://<your-host>/slack/events`.
- **Interactivity & Shortcuts** — enable; request URL `…/slack/events`;
  add a message shortcut with callback id `mark_as_commitment`.
- **Event Subscriptions** — enable; request URL `…/slack/events`;
  subscribe to `app_home_opened`, `message.channels`, `message.im`,
  `message.groups`, `message.mpim`.
- **App Home** — toggle the **Home Tab** on.

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

**137 tests, ~7 seconds.** Organised by surface:

- **`test_services.py`** — notation validation, message dedup,
  versioning, on-hold precedence, bin recovery, field validation,
  basic ping cadence.
- **`test_escalation.py`** — every branch of the cadence calculator
  (no-deadline, before window, inside window, overdue, paused,
  REASSIGNED, escalation_enabled toggle, floor enforcement, system
  min, max-stages clamp, defensive rate clamp, `is_at_floor`
  accuracy, `current_interval`/`compute_next` parity, reschedule
  semantics, `format_interval`, end-to-end `process_due_pings`,
  deadline-driven auto-resume).
- **`test_reassignments.py`** — happy path (accept/decline/cancel/expire),
  validation (self-reassign rejected, double-pending rejected,
  un-onboarded target rejected, cross-workspace rejected, only
  active-or-reassigned), side effects (edit log, idempotency,
  edit-during-limbo block, `prior_state` restoration on decline,
  re-reassignment chains).
- **`test_outcomes.py`** — every rule for SUCCESS/FAILED across
  `mark_done`, `soft_delete`, `archive`, `reopen`, `restore_from_bin`,
  plus the immediate auto-archive at X=0.
- **`test_agent.py`** — StubProvider classification (first-person
  promise, vague intent, hypothetical, third-party, self-directed
  reminders); buffer idempotency + agent-disabled refusal; scan
  persistence above floor, drop below floor, dry-run, dedup;
  `is_likely_candidate` pre-filter; per-user scan interval +
  due-check (`is_scan_due` true on never-scanned, false within
  window, true after window); `last_agent_scan_at` stamping; Undo
  hard-deletes within the window and refuses past it / for non-agent
  captures; buffer prune.
- **`test_smoke.py`** — end-to-end: app boots, demo data seeds, every
  dashboard tab renders, mark-done round-trips through HTMX,
  cross-user mutation is rejected, **sender-side reassignment
  visibility** (regression guard for the "my dashboard went blank
  after Bob accepted" bug).

---

## Things deliberately out of scope

This is an MVP. The list of things that *aren't* here, and *aren't*
defects:

- **Meeting AI / voice capture** — was in the original spec. Hooks are
  reserved but not implemented.
- **Jira / Zendesk integration** — same.
- **Dependency-blocking links (`blocked_by`)** — schema-only Phase 2
  hook. The column exists in `Commitment`; nothing reads or writes it.
- **Daily digest** — `PriorityLevel.daily_digest_enabled` field is
  there; no scheduler job assembles digests.
- **"Owed to me" inbound view** — there's no Tab 2 yet. Use the
  reassignment workaround documented above.
- **Native mobile app** — dashboard is responsive but not
  mobile-optimised.
- **Multi-workspace install flow** — Sign in with Slack works for users
  in the workspace where CommitBot is installed; installing CommitBot
  into a *new* workspace still requires manual config (the bot-install
  OAuth flow isn't wired up).

---

## Acknowledgements

CommitBot started as a capstone project. Three interviews shaped it —
an APM who'd already patched the problem with sticky notes, a UX
designer who wanted voice capture, and a support hire who pointed out
that context-rich ticket systems still lose nuance because *every
message looks the same.* Those constraints became the design brief.
